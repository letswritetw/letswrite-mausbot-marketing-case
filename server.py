#!/usr/bin/env python3
"""Case dashboard server.

Serves the runtime dashboard, a small read-only JSON API over the
marketing-cases/articles folder, and optional per-user favorites backed by
SQLite. Only files named YYYY/MM/DD.md are exposed.

Endpoints:
    GET    /                          -> web/index.html
    GET    /api/cases                 -> {"generated": "...", "cases": [...]}
    GET    /api/case?name=YYYY/MM/DD.md -> {"file": ..., "content": ...}
    POST   /api/view                  {"case_file"} -> {"views", "counted"}
    GET    /healthz                   -> ok
    POST   /api/register              {"email", "password"} -> 201 {"email"}
    POST   /api/login                 {"email", "password"} -> 200 {"email"}
    POST   /api/logout                -> 200 {"ok": true}
    GET    /api/me                    -> {"email"} or 401
    GET    /api/favorites             -> {"favorites": [...]}
    PUT    /api/favorite              {"case_file", "note"}
    DELETE /api/favorite?case_file=YYYY/MM/DD.md
    GET    /api/notify                -> {"enabled": 0|1, "smtp_ready": true|false}
    PUT    /api/notify                {"enabled": true|false}

Browsing stays anonymous; favorites and notifications require a session
cookie. Article views are counted once per viewer and UTC day in case_view_log,
with the aggregate kept in case_stats; favorite counts are read live from
favorites. The database lives at $DB_PATH (default $DATA_DIR/appdata/favorites.db).
A daemon thread emails each new article to users who enabled notifications.
"""

import hashlib
import hmac
import html
import json
import os
import re
import secrets
import smtplib
import sqlite3
import ssl
import threading
import time
import traceback
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formatdate
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

DATA_DIR = os.environ.get("DATA_DIR", "/data")
ARTICLE_DIR = os.path.join(DATA_DIR, "articles")
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
PORT = int(os.environ.get("PORT", "80"))
DB_PATH = os.environ.get("DB_PATH") or os.path.join(DATA_DIR, "appdata", "favorites.db")

CASE_RE = re.compile(r"^[0-9]{4}/[0-9]{2}/[0-9]{2}\.md$")
DATE_RE = re.compile(r"([0-9]{4}-[0-9]{2}-[0-9]{2})")
PATH_DATE_RE = re.compile(r"^([0-9]{4})/([0-9]{2})/([0-9]{2})\.md$")
SPLIT_RE = re.compile(r"[\uff5c|]")
THEME_PREFIX_RE = re.compile(r"^[^:\uff1a]{1,6}[\uff1a:]\s*(.+)$")
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[^@\s]{2,63}$")

SESSION_COOKIE = "mc_session"
VIEW_COOKIE = "mc_vid"
VIEW_COOKIE_TTL = 365 * 24 * 3600
SESSION_TTL = 30 * 24 * 3600
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "").lower() in ("1", "true", "yes")
MAX_BODY = 64 * 1024
MAX_NOTE = 2000
SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
PBKDF2_ITER = 200000


def _env_int(name, default):
    try:
        value = int(os.environ.get(name, ""))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def load_smtp_config():
    host = (os.environ.get("SMTP_HOST") or "").strip()
    user = (os.environ.get("SMTP_USER") or "").strip()
    password = os.environ.get("SMTP_PASS") or ""
    sender = (os.environ.get("SMTP_FROM") or "").strip() or user
    sender = sender.replace("\r", " ").replace("\n", " ").strip()
    tls = (os.environ.get("SMTP_TLS") or "starttls").strip().lower()
    if tls not in ("ssl", "starttls", "none"):
        tls = "invalid"
    try:
        port = int((os.environ.get("SMTP_PORT") or "").strip())
    except (TypeError, ValueError):
        port = 0
    ready = bool(host and 0 < port < 65536 and user and password and sender and tls != "invalid")
    return {
        "host": host,
        "port": port,
        "user": user,
        "password": password,
        "sender": sender,
        "tls": tls,
        "ready": ready,
    }


SMTP_CONFIG = load_smtp_config()
NOTIFY_POLL_SECONDS = _env_int("NOTIFY_POLL_SECONDS", 300)
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "http://localhost:8801/")
EMAIL_ACCENT = "#9C4A1B"
EMAIL_ACCENT_SOFT = "#C05621"

_FAIL_LOCK = threading.Lock()
_FAILS = {}


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_text(path):
    with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
        return fh.read().replace("\r\n", "\n").replace("\r", "\n")


def date_from_path(relpath):
    m = PATH_DATE_RE.match(relpath or "")
    if not m:
        return ""
    return "%s-%s-%s" % (m.group(1), m.group(2), m.group(3))


def parse_case(relpath, text):
    title = None
    meta_line = None
    for line in text.split("\n"):
        t = line.strip()
        if title is None and t.startswith("# "):
            title = t[2:].strip()
        if meta_line is None and t.startswith(">"):
            meta_line = t[1:].strip()
        if title and meta_line:
            break

    path_date = date_from_path(relpath)
    if not title:
        title = path_date or os.path.basename(relpath)[:-3]

    date = ""
    if meta_line:
        m = DATE_RE.search(meta_line)
        if m:
            date = m.group(1)
    if not date:
        date = path_date
    if not date:
        date = relpath

    theme = ""
    if meta_line:
        parts = SPLIT_RE.split(meta_line)
        if len(parts) > 1:
            theme = parts[-1].strip()
        m = THEME_PREFIX_RE.match(theme)
        if m:
            theme = m.group(1).strip()

    return {"file": relpath, "date": date, "theme": theme, "title": title}


def list_cases():
    cases = []
    if not os.path.isdir(ARTICLE_DIR):
        return cases
    for root, dirs, files in os.walk(ARTICLE_DIR):
        dirs[:] = [d for d in dirs if d.isdigit()]
        for name in files:
            full = os.path.join(root, name)
            relpath = os.path.relpath(full, ARTICLE_DIR).replace(os.sep, "/")
            if not CASE_RE.fullmatch(relpath) or not os.path.isfile(full):
                continue
            try:
                text = read_text(full)
            except OSError:
                continue
            item = parse_case(relpath, text)
            item["size"] = len(text.encode("utf-8"))
            cases.append(item)
    cases.sort(key=lambda c: (c["date"], c["file"]), reverse=True)
    return cases


def get_case(name):
    relpath = (name or "").replace("\\", "/").strip()
    if not CASE_RE.fullmatch(relpath):
        return None
    base = os.path.realpath(ARTICLE_DIR)
    path = os.path.realpath(os.path.join(base, *relpath.split("/")))
    if os.path.commonpath([base, path]) != base or not os.path.isfile(path):
        return None
    text = read_text(path)
    item = parse_case(relpath, text)
    item["content"] = text.strip()
    return item


def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    parent = os.path.dirname(DB_PATH)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = db()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                notify_enabled INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);
            CREATE TABLE IF NOT EXISTS favorites (
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                case_file TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(user_id, case_file)
            );
            CREATE INDEX IF NOT EXISTS idx_favorites_user ON favorites(user_id);
            CREATE TABLE IF NOT EXISTS notify_sent (
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                case_file TEXT NOT NULL,
                sent_at TEXT NOT NULL,
                UNIQUE(user_id, case_file)
            );
            CREATE INDEX IF NOT EXISTS idx_notify_sent_user ON notify_sent(user_id);
            CREATE TABLE IF NOT EXISTS case_stats (
                case_file TEXT PRIMARY KEY,
                views INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS case_view_log (
                case_file TEXT NOT NULL,
                viewer_key TEXT NOT NULL,
                day TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (case_file, viewer_key, day)
            );
            CREATE INDEX IF NOT EXISTS idx_case_view_log_day ON case_view_log(day);
            """
        )
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
        if "notify_enabled" not in columns:
            conn.execute(
                "ALTER TABLE users ADD COLUMN notify_enabled INTEGER NOT NULL DEFAULT 0"
            )
        cutoff = datetime.fromtimestamp(
            time.time() - 90 * 24 * 3600, timezone.utc
        ).strftime("%Y-%m-%d")
        conn.execute("DELETE FROM case_view_log WHERE day < ?", (cutoff,))
        conn.commit()
    finally:
        conn.close()


def case_stats():
    """Return (views, favorites) maps keyed by case_file; empty on DB error."""
    views = {}
    favorites = {}
    try:
        conn = db()
        try:
            for row in conn.execute("SELECT case_file, views FROM case_stats"):
                views[row["case_file"]] = int(row["views"] or 0)
            for row in conn.execute(
                "SELECT case_file, COUNT(*) AS n FROM favorites GROUP BY case_file"
            ):
                favorites[row["case_file"]] = int(row["n"] or 0)
        finally:
            conn.close()
    except sqlite3.Error:
        return {}, {}
    return views, favorites


def attach_stats(cases):
    views, favorites = case_stats()
    for item in cases:
        item["views"] = views.get(item["file"], 0)
        item["fav_count"] = favorites.get(item["file"], 0)
    return cases


def record_view(case_file, viewer_key):
    """Count one view per case_file, viewer and UTC day; return {counted, views}."""
    stamp = now_iso()
    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "INSERT OR IGNORE INTO case_view_log"
            " (case_file, viewer_key, day, created_at) VALUES (?, ?, ?, ?)",
            (case_file, viewer_key, stamp[:10], stamp),
        )
        counted = cur.rowcount == 1
        if counted:
            conn.execute(
                "INSERT INTO case_stats (case_file, views, updated_at) VALUES (?, 1, ?)"
                " ON CONFLICT(case_file)"
                " DO UPDATE SET views = views + 1, updated_at = excluded.updated_at",
                (case_file, stamp),
            )
        row = conn.execute(
            "SELECT views FROM case_stats WHERE case_file = ?", (case_file,)
        ).fetchone()
        conn.commit()
        return {"counted": counted, "views": int(row["views"]) if row else 0}
    except sqlite3.Error:
        conn.rollback()
        raise
    finally:
        conn.close()


def hash_password(password):
    salt = secrets.token_bytes(16)
    try:
        digest = hashlib.scrypt(
            password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32
        )
        return "scrypt$%d$%d$%d$%s$%s" % (
            SCRYPT_N, SCRYPT_R, SCRYPT_P, salt.hex(), digest.hex()
        )
    except (ValueError, AttributeError, MemoryError):
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITER)
        return "pbkdf2$%d$%s$%s" % (PBKDF2_ITER, salt.hex(), digest.hex())


def check_password(password, stored):
    try:
        parts = (stored or "").split("$")
        if parts[0] == "scrypt" and len(parts) == 6:
            n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
            salt = bytes.fromhex(parts[4])
            expected = bytes.fromhex(parts[5])
            digest = hashlib.scrypt(
                password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=len(expected)
            )
        elif parts[0] == "pbkdf2" and len(parts) == 4:
            iterations = int(parts[1])
            salt = bytes.fromhex(parts[2])
            expected = bytes.fromhex(parts[3])
            digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
        else:
            return False
    except (ValueError, AttributeError):
        return False
    return hmac.compare_digest(digest, expected)


def dummy_password_check(password):
    hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), b"missing-user", PBKDF2_ITER)


def too_many_failures(key):
    now = time.time()
    with _FAIL_LOCK:
        bucket = [t for t in _FAILS.get(key, []) if now - t < 900]
        _FAILS[key] = bucket
        return len(bucket) >= 10


def note_failure(key):
    now = time.time()
    with _FAIL_LOCK:
        if len(_FAILS) > 5000:
            for k in [k for k, v in _FAILS.items() if not v or now - v[-1] >= 900]:
                _FAILS.pop(k, None)
        _FAILS.setdefault(key, []).append(now)


def clear_failures(key):
    with _FAIL_LOCK:
        _FAILS.pop(key, None)


def create_session(user_id):
    token = secrets.token_hex(32)
    created = now_iso()
    expires = datetime.fromtimestamp(time.time() + SESSION_TTL, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    conn = db()
    try:
        conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (created,))
        conn.execute(
            "INSERT INTO sessions (token, user_id, expires_at, created_at) VALUES (?, ?, ?, ?)",
            (token, user_id, expires, created),
        )
        conn.commit()
    finally:
        conn.close()
    return token


def favorite_entry(row):
    item = get_case(row["case_file"])
    return {
        "case_file": row["case_file"],
        "note": row["note"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "exists": item is not None,
        "title": item["title"] if item else row["case_file"],
        "date": item["date"] if item else date_from_path(row["case_file"]),
        "theme": item["theme"] if item else "",
    }


def list_favorites(user_id):
    conn = db()
    try:
        rows = conn.execute(
            "SELECT case_file, note, created_at, updated_at FROM favorites"
            " WHERE user_id = ? ORDER BY updated_at DESC, case_file DESC",
            (user_id,),
        ).fetchall()
    finally:
        conn.close()
    return [favorite_entry(row) for row in rows]


def notify_state(user_id):
    conn = db()
    try:
        row = conn.execute(
            "SELECT notify_enabled FROM users WHERE id = ?", (user_id,)
        ).fetchone()
    finally:
        conn.close()
    enabled = 1 if row is not None and row["notify_enabled"] else 0
    return {"enabled": enabled, "smtp_ready": bool(SMTP_CONFIG["ready"])}


def pending_notify_users(case_file):
    conn = db()
    try:
        return conn.execute(
            "SELECT u.id, u.email FROM users u"
            " WHERE u.notify_enabled = 1"
            " AND NOT EXISTS ("
            "   SELECT 1 FROM notify_sent n"
            "   WHERE n.user_id = u.id AND n.case_file = ?"
            " ) ORDER BY u.id",
            (case_file,),
        ).fetchall()
    finally:
        conn.close()


def mark_notify_sent(user_id, case_file):
    conn = db()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO notify_sent (user_id, case_file, sent_at)"
            " VALUES (?, ?, ?)",
            (user_id, case_file, now_iso()),
        )
        conn.commit()
    finally:
        conn.close()


def _plain_inline(text):
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    return text.replace("**", "")


def _html_inline(text):
    escaped = html.escape(text, quote=False)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
    return escaped.replace("**", "")


def markdown_to_plain(text):
    lines = (text or "").split("\n")
    out = []
    paragraph = []

    def flush_paragraph():
        if paragraph:
            out.append("\n".join(_plain_inline(line.strip()) for line in paragraph))
            paragraph.clear()

    for line in lines:
        stripped = line.strip()
        if not stripped:
            flush_paragraph()
            if out and out[-1] != "":
                out.append("")
            continue
        if re.fullmatch(r"-{3,}", stripped):
            flush_paragraph()
            out.append("────────")
            continue
        match = re.match(r"^(#{1,3})\s+(.*)$", stripped)
        if match:
            flush_paragraph()
            out.append(_plain_inline(match.group(2).strip()))
            continue
        match = re.match(r"^\s*[-*]\s+(.*)$", line)
        if match:
            flush_paragraph()
            out.append("• " + _plain_inline(match.group(1).strip()))
            continue
        match = re.match(r"^\s*(\d+)\.\s+(.*)$", line)
        if match:
            flush_paragraph()
            out.append("%s. %s" % (match.group(1), _plain_inline(match.group(2).strip())))
            continue
        match = re.match(r"^\s*>\s?(.*)$", line)
        if match:
            flush_paragraph()
            out.append(_plain_inline(match.group(1).strip()))
            continue
        paragraph.append(line)
    flush_paragraph()
    result = "\n".join(out)
    return re.sub(r"\n{3,}", "\n\n", result).strip()


def markdown_to_html(text):
    lines = (text or "").split("\n")
    out = []
    paragraph = []
    list_type = None
    list_items = []
    quotes = []

    def flush_paragraph():
        if paragraph:
            out.append(
                '<p style="margin:0 0 18px 0;font-size:16px;line-height:1.85;color:#374151;">'
                + "<br>".join(_html_inline(line.strip()) for line in paragraph)
                + "</p>"
            )
            paragraph.clear()

    def flush_list():
        nonlocal list_type
        if list_type and list_items:
            tag = list_type
            out.append(
                '<%s style="margin:0 0 18px 0;padding-left:22px;color:#374151;font-size:16px;line-height:1.85;">'
                % tag
                + "".join(
                    '<li style="margin:0 0 8px 0;">%s</li>' % item
                    for item in list_items
                )
                + "</%s>" % tag
            )
        list_items.clear()
        list_type = None

    def flush_quote():
        if quotes:
            out.append(
                '<blockquote style="margin:18px 0;padding:12px 16px;border-left:3px solid #C05621;background:#FAFAF7;color:#5C6470;font-size:15px;line-height:1.8;">'
                + "<br>".join(_html_inline(line.strip()) for line in quotes)
                + "</blockquote>"
            )
            quotes.clear()

    def flush_all():
        flush_paragraph()
        flush_list()
        flush_quote()

    for line in lines:
        stripped = line.strip()
        if not stripped:
            flush_all()
            continue
        if re.fullmatch(r"-{3,}", stripped):
            flush_all()
            out.append('<hr style="border:0;border-top:1px solid #E5E7EB;margin:24px 0;">')
            continue
        match = re.match(r"^(#{1,3})\s+(.*)$", stripped)
        if match:
            flush_all()
            level = min(3, len(match.group(1)))
            if level == 1:
                style = "margin:28px 0 12px 0;font-size:24px;line-height:1.4;color:#1F2430;font-weight:700;"
            elif level == 2:
                style = "margin:28px 0 10px 0;font-size:20px;line-height:1.45;color:#9C4A1B;font-weight:700;"
            else:
                style = "margin:22px 0 8px 0;font-size:17px;line-height:1.5;color:#1F2430;font-weight:700;"
            tag = "h%d" % level
            out.append(
                '<%s style="%s">%s</%s>'
                % (tag, style, _html_inline(match.group(2).strip()), tag)
            )
            continue
        match = re.match(r"^\s*[-*]\s+(.*)$", line)
        if match:
            flush_paragraph()
            flush_quote()
            if list_type != "ul":
                flush_list()
                list_type = "ul"
            list_items.append(_html_inline(match.group(1).strip()))
            continue
        match = re.match(r"^\s*(\d+)\.\s+(.*)$", line)
        if match:
            flush_paragraph()
            flush_quote()
            if list_type != "ol":
                flush_list()
                list_type = "ol"
            list_items.append(_html_inline(match.group(2).strip()))
            continue
        match = re.match(r"^\s*>\s?(.*)$", line)
        if match:
            flush_paragraph()
            flush_list()
            quotes.append(match.group(1).strip())
            continue
        flush_list()
        flush_quote()
        paragraph.append(line)
    flush_all()
    return "\n".join(out)


def _strip_email_lead(lines):
    removed_title = False
    removed_meta = False
    out = []
    for line in lines:
        stripped = line.strip()
        if not removed_title and stripped.startswith("# "):
            removed_title = True
            continue
        if not removed_meta and stripped.startswith(">"):
            removed_meta = True
            continue
        out.append(line)
    return out


def _split_application(lines):
    for index, line in enumerate(lines):
        if re.fullmatch(r"##\s*可以怎麼落地應用\s*", line.strip()):
            return lines[:index], lines[index:]
    return lines, []


def _email_issue(title, date):
    match = re.search(r"#\s*([^｜|]+)", title or "")
    return match.group(1).strip() if match else (date or "")


def _email_display_title(title):
    parts = [part.strip() for part in re.split(r"[｜|]", title or "") if part.strip()]
    return parts[-1] if len(parts) >= 2 else (title or "").strip()


def build_case_email_parts(item):
    content = (item.get("content") or "").strip()
    body_lines = _strip_email_lead(content.split("\n"))
    main_lines, application_lines = _split_application(body_lines)
    plain_body = markdown_to_plain("\n".join(body_lines))
    main_html = markdown_to_html("\n".join(main_lines))
    application_html = markdown_to_html("\n".join(application_lines)) if application_lines else ""

    title = _email_display_title(item.get("title") or "")
    date = item.get("date") or ""
    theme = item.get("theme") or ""
    issue = _email_issue(item.get("title") or "", date)
    issue_line = "第 %s 期" % issue if issue else ""
    meta = [part for part in (issue_line, date) if part]
    if theme:
        meta.append("主題：" + theme)

    plain_lines = ["經典案例日刊"]
    if meta:
        plain_lines.append("｜".join(meta))
    if title:
        plain_lines.extend(["", title])
    plain = "\n".join(plain_lines).strip()
    if plain_body:
        plain += "\n\n" + plain_body
    plain += "\n\n────────\n經典案例日刊\n" + DASHBOARD_URL + "\n"

    title_html = html.escape(title)
    date_html = html.escape(date)
    theme_html = html.escape(theme)
    issue_html = html.escape(issue_line)
    url_html = html.escape(DASHBOARD_URL, quote=True)
    meta_html = ""
    if issue_line:
        meta_html += (
            '<span style="display:inline-block;padding:5px 10px;border-radius:999px;'
            'background:#FDF0E7;color:#9C4A1B;font-size:12px;font-weight:700;">'
            + issue_html
            + "</span>"
        )
    if date:
        meta_html += (
            '<span style="display:inline-block;margin-left:10px;color:#5C6470;'
            'font-size:13px;line-height:1.6;">' + date_html + "</span>"
        )
    theme_html_block = ""
    if theme:
        theme_html_block = (
            '<p style="margin:8px 0 0 0;color:#5C6470;font-size:14px;line-height:1.6;">'
            "主題：" + theme_html + "</p>"
        )
    application_block = ""
    if application_html:
        application_block = (
            '<tr><td style="padding:8px 32px 24px 32px;">'
            '<div style="background:#FAFAF7;border:1px solid #E8E2DA;border-radius:12px;'
            'padding:20px 22px;color:#374151;font-size:16px;line-height:1.85;">'
            + application_html
            + "</div></td></tr>"
        )

    html_body = (
        '<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1"></head>'
        '<body style="margin:0;padding:0;background:#FAFAF7;">'
        '<div style="display:none;max-height:0;max-width:0;overflow:hidden;opacity:0;'
        'color:#FAFAF7;font-size:1px;line-height:1px;">'
        + title_html
        + "｜"
        + date_html
        + "</div>"
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        'style="width:100%;background:#FAFAF7;padding:28px 12px;">'
        '<tr><td align="center">'
        '<table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" '
        'style="width:600px;max-width:600px;background:#FFFFFF;border:1px solid #E5E7EB;'
        'border-radius:16px;border-collapse:collapse;font-family:-apple-system,BlinkMacSystemFont,'
        "'Segoe UI','Noto Sans TC','Microsoft JhengHei',sans-serif;color:#1F2430;\">"
        '<tr><td style="padding:28px 32px 0 32px;">'
        '<div style="font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',\'Noto Sans TC\','
        "'Microsoft JhengHei',sans-serif;font-size:12px;letter-spacing:0.18em;color:#9C4A1B;"
        'font-weight:700;">經典案例日刊</div>'
        '<div style="margin-top:14px;">'
        + meta_html
        + "</div>"
        '<h1 style="margin:18px 0 0 0;font-size:26px;line-height:1.35;color:#1F2430;'
        'font-weight:700;">'
        + title_html
        + "</h1>"
        + theme_html_block
        + "</td></tr>"
        '<tr><td style="padding:24px 32px 8px 32px;color:#374151;font-size:16px;'
        'line-height:1.85;">'
        + main_html
        + "</td></tr>"
        + application_block
        + '<tr><td style="padding:20px 32px 30px 32px;border-top:1px solid #E5E7EB;">'
        '<p style="margin:0 0 6px 0;color:#5C6470;font-size:13px;line-height:1.6;">'
        "經典案例日刊</p>"
        '<a href="'
        + url_html
        + '" style="color:#9C4A1B;font-size:14px;font-weight:600;text-decoration:none;">'
        "前往儀表板 →</a>"
        '<p style="margin:8px 0 0 0;color:#8A94A6;font-size:12px;line-height:1.6;'
        'word-break:break-all;">'
        + url_html
        + "</p></td></tr></table></td></tr></table></body></html>"
    )
    return plain, html_body


def case_email_message(item, recipient):
    message = EmailMessage()
    message["Subject"] = item["title"]
    message["From"] = SMTP_CONFIG["sender"]
    message["To"] = recipient
    message["Date"] = formatdate(localtime=True)
    plain, html_body = build_case_email_parts(item)
    message.set_content(plain, subtype="plain", charset="utf-8")
    message.add_alternative(html_body, subtype="html", charset="utf-8")
    return message


def send_case_email(recipient, item):
    config = SMTP_CONFIG
    client = None
    context = ssl.create_default_context()
    try:
        if config["tls"] == "ssl":
            client = smtplib.SMTP_SSL(
                config["host"], config["port"], timeout=20, context=context
            )
        else:
            client = smtplib.SMTP(config["host"], config["port"], timeout=20)
        client.ehlo()
        if config["tls"] == "starttls":
            client.starttls(context=context)
            client.ehlo()
        client.login(config["user"], config["password"])
        client.send_message(case_email_message(item, recipient))
    finally:
        if client is not None:
            try:
                client.quit()
            except Exception:
                try:
                    client.close()
                except Exception:
                    pass


def latest_case_file():
    cases = list_cases()
    return cases[0]["file"] if cases else ""


def log_notify(message):
    print("notify: %s" % message, flush=True)


def notify_scan(last_file):
    cases = list_cases()
    if not cases:
        return last_file
    new_cases = [case for case in cases if case["file"] > last_file]
    if not new_cases:
        return last_file
    new_cases.sort(key=lambda case: case["file"])
    progress = last_file
    for index, meta in enumerate(new_cases):
        item = get_case(meta["file"])
        if item is None:
            progress = meta["file"]
            continue
        users = pending_notify_users(meta["file"])
        if not users:
            progress = meta["file"]
            continue
        if not SMTP_CONFIG["ready"]:
            log_notify(
                "SMTP 未設定，暫停寄送（待寄 %d 篇，從 %s 開始，下輪重試）"
                % (len(new_cases) - index, meta["file"])
            )
            break
        failed = 0
        for user in users:
            try:
                send_case_email(user["email"], item)
            except Exception as exc:
                failed += 1
                log_notify(
                    "寄送失敗 case=%s user=%s: %s: %s"
                    % (meta["file"], user["email"], type(exc).__name__, exc)
                )
                continue
            try:
                mark_notify_sent(user["id"], meta["file"])
            except sqlite3.Error as exc:
                failed += 1
                log_notify(
                    "記錄寄送失敗 case=%s user=%s: %s"
                    % (meta["file"], user["email"], exc)
                )
            else:
                log_notify("已寄送 case=%s to=%s" % (meta["file"], user["email"]))
        if failed:
            log_notify(
                "case=%s 尚有 %d 位寄送失敗，下輪重試" % (meta["file"], failed)
            )
            break
        progress = meta["file"]
    return progress


def notify_loop():
    last_file = latest_case_file()
    log_notify(
        "啟動（每 %d 秒掃描，基準 %s，SMTP %s）"
        % (
            NOTIFY_POLL_SECONDS,
            last_file or "(尚無案例)",
            "已設定" if SMTP_CONFIG["ready"] else "未設定",
        )
    )
    while True:
        time.sleep(NOTIFY_POLL_SECONDS)
        try:
            last_file = notify_scan(last_file)
        except Exception:
            log_notify("掃描失敗，下輪重試")
            traceback.print_exc()


class Handler(BaseHTTPRequestHandler):
    server_version = "CaseDashboard/1.2"

    def _send(self, status, body, content_type, extra_headers=None):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        for name, value in (extra_headers or []):
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def _json(self, payload, status=200, extra_headers=None):
        self._send(
            status,
            json.dumps(payload, ensure_ascii=False),
            "application/json; charset=utf-8",
            extra_headers,
        )

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            return None
        if length <= 0 or length > MAX_BODY:
            return None
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            return None
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def _cookie_token(self):
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        jar = SimpleCookie()
        try:
            jar.load(raw)
        except CookieError:
            return None
        morsel = jar.get(SESSION_COOKIE)
        if morsel is None or not morsel.value or len(morsel.value) > 128:
            return None
        return morsel.value

    def _user(self):
        token = self._cookie_token()
        if not token:
            return None
        conn = db()
        try:
            row = conn.execute(
                "SELECT u.id, u.email, s.expires_at FROM sessions s"
                " JOIN users u ON u.id = s.user_id WHERE s.token = ?",
                (token,),
            ).fetchone()
            if row is None:
                return None
            if row["expires_at"] <= now_iso():
                conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
                conn.commit()
                return None
            return {"id": row["id"], "email": row["email"], "token": token}
        finally:
            conn.close()

    def _require_user(self):
        user = self._user()
        if user is None:
            self._json({"error": "請先登入"}, 401)
            return None
        return user

    def _session_headers(self, token=None):
        if token:
            parts = [
                "%s=%s" % (SESSION_COOKIE, token),
                "Path=/",
                "HttpOnly",
                "SameSite=Lax",
                "Max-Age=%d" % SESSION_TTL,
            ]
        else:
            parts = [
                "%s=" % SESSION_COOKIE,
                "Path=/",
                "HttpOnly",
                "SameSite=Lax",
                "Max-Age=0",
            ]
        if COOKIE_SECURE:
            parts.append("Secure")
        return [("Set-Cookie", "; ".join(parts))]

    def _view_key(self):
        """Identify the viewer without storing raw IPs or cookie tokens."""
        user = self._user()
        if user is not None:
            return "u:%d" % user["id"], None
        token = None
        raw = self.headers.get("Cookie")
        if raw:
            jar = SimpleCookie()
            try:
                jar.load(raw)
            except CookieError:
                jar = SimpleCookie()
            morsel = jar.get(VIEW_COOKIE)
            if morsel is not None and re.fullmatch(r"[0-9a-f]{32}", morsel.value or ""):
                token = morsel.value
        extra = None
        if token is None:
            token = secrets.token_hex(16)
            parts = [
                "%s=%s" % (VIEW_COOKIE, token),
                "Path=/",
                "HttpOnly",
                "SameSite=Lax",
                "Max-Age=%d" % VIEW_COOKIE_TTL,
            ]
            if COOKIE_SECURE:
                parts.append("Secure")
            extra = [("Set-Cookie", "; ".join(parts))]
        digest = hashlib.sha256(token.encode("ascii")).hexdigest()[:32]
        return "a:" + digest, extra

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path in ("/", "/index.html"):
            try:
                with open(os.path.join(WEB_DIR, "index.html"), "r", encoding="utf-8") as fh:
                    html = fh.read()
            except OSError:
                self._json({"error": "dashboard ui is missing"}, 500)
                return
            self._send(200, html, "text/html; charset=utf-8")
            return

        if path == "/api/cases":
            self._json(
                {
                    "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
                    "cases": attach_stats(list_cases()),
                }
            )
            return

        if path == "/api/case":
            name = (parse_qs(parsed.query).get("name") or [""])[0]
            item = get_case(name)
            if item is None:
                self._json({"error": "case not found"}, 404)
                return
            self._json(item)
            return

        if path == "/api/me":
            user = self._user()
            if user is None:
                self._json({"error": "未登入"}, 401)
                return
            self._json({"email": user["email"]})
            return

        if path == "/api/favorites":
            user = self._require_user()
            if user is None:
                return
            self._json({"favorites": list_favorites(user["id"])})
            return

        if path == "/api/notify":
            user = self._require_user()
            if user is None:
                return
            self._json(notify_state(user["id"]))
            return

        if path == "/healthz":
            self._send(200, "ok", "text/plain; charset=utf-8")
            return

        self._json({"error": "not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path

        if path == "/api/register":
            data = self._read_json()
            if data is None:
                self._json({"error": "請以 JSON 送出 email 與密碼"}, 400)
                return
            email = str(data.get("email") or "").strip().lower()
            password = data.get("password")
            if not EMAIL_RE.fullmatch(email) or len(email) > 254:
                self._json({"error": "Email 格式不正確"}, 400)
                return
            if not isinstance(password, str) or not 8 <= len(password) <= 256:
                self._json({"error": "密碼長度需為 8 到 256 碼"}, 400)
                return
            if too_many_failures("register|" + self.client_address[0]):
                self._json({"error": "嘗試次數過多，請稍後再試"}, 429)
                return
            conn = db()
            try:
                cur = conn.execute(
                    "INSERT INTO users (email, password_hash, created_at) VALUES (?, ?, ?)",
                    (email, hash_password(password), now_iso()),
                )
                user_id = cur.lastrowid
                conn.commit()
            except sqlite3.IntegrityError:
                note_failure("register|" + self.client_address[0])
                self._json({"error": "這個 email 已經註冊過了，請直接登入"}, 409)
                return
            finally:
                conn.close()
            clear_failures("register|" + self.client_address[0])
            token = create_session(user_id)
            self._json({"email": email}, 201, self._session_headers(token))
            return

        if path == "/api/login":
            data = self._read_json()
            if data is None:
                self._json({"error": "請以 JSON 送出 email 與密碼"}, 400)
                return
            email = str(data.get("email") or "").strip().lower()
            password = data.get("password")
            if not isinstance(password, str) or not password or len(password) > 256:
                self._json({"error": "Email 或密碼錯誤"}, 401)
                return
            key = "login|%s|%s" % (self.client_address[0], email)
            if too_many_failures(key):
                self._json({"error": "嘗試次數過多，請 15 分鐘後再試"}, 429)
                return
            conn = db()
            try:
                row = conn.execute(
                    "SELECT id, email, password_hash FROM users WHERE email = ?", (email,)
                ).fetchone()
            finally:
                conn.close()
            if row is None:
                dummy_password_check(password)
                note_failure(key)
                self._json({"error": "Email 或密碼錯誤"}, 401)
                return
            if not check_password(password, row["password_hash"]):
                note_failure(key)
                self._json({"error": "Email 或密碼錯誤"}, 401)
                return
            clear_failures(key)
            token = create_session(row["id"])
            self._json({"email": row["email"]}, 200, self._session_headers(token))
            return

        if path == "/api/logout":
            token = self._cookie_token()
            if token:
                conn = db()
                try:
                    conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
                    conn.commit()
                finally:
                    conn.close()
            self._json({"ok": True}, 200, self._session_headers(None))
            return

        if path == "/api/view":
            data = self._read_json()
            if data is None:
                self._json({"error": "請以 JSON 送出 case_file"}, 400)
                return
            case_file = str(data.get("case_file") or "").replace("\\", "/").strip()
            if not CASE_RE.fullmatch(case_file) or get_case(case_file) is None:
                self._json({"error": "case not found"}, 404)
                return
            viewer_key, extra_headers = self._view_key()
            try:
                result = record_view(case_file, viewer_key)
            except sqlite3.Error:
                self._json({"error": "統計暫時無法寫入"}, 503)
                return
            self._json(
                {
                    "case_file": case_file,
                    "views": result["views"],
                    "counted": result["counted"],
                },
                200,
                extra_headers,
            )
            return

        self._json({"error": "not found"}, 404)

    def do_PUT(self):
        path = urlparse(self.path).path

        if path == "/api/notify":
            user = self._require_user()
            if user is None:
                return
            data = self._read_json()
            if data is None or not isinstance(data.get("enabled"), bool):
                self._json({"error": "請以 JSON 送出 enabled（true 或 false）"}, 400)
                return
            enabled = 1 if data["enabled"] else 0
            if enabled and not SMTP_CONFIG["ready"]:
                self._json(
                    {"error": "email 通知尚未配置，請管理員設定 SMTP 環境變數"}, 409
                )
                return
            conn = db()
            try:
                conn.execute(
                    "UPDATE users SET notify_enabled = ? WHERE id = ?",
                    (enabled, user["id"]),
                )
                conn.commit()
            finally:
                conn.close()
            self._json(notify_state(user["id"]))
            return

        if path != "/api/favorite":
            self._json({"error": "not found"}, 404)
            return
        user = self._require_user()
        if user is None:
            return
        data = self._read_json()
        if data is None:
            self._json({"error": "請以 JSON 送出 case_file 與 note"}, 400)
            return
        case_file = str(data.get("case_file") or "").replace("\\", "/").strip()
        if not CASE_RE.fullmatch(case_file):
            self._json({"error": "case_file 格式不正確"}, 400)
            return
        note = data.get("note", "")
        if note is None:
            note = ""
        if not isinstance(note, str):
            self._json({"error": "note 必須是文字"}, 400)
            return
        note = note.strip()
        if len(note) > MAX_NOTE:
            self._json({"error": "註記最多 %d 字" % MAX_NOTE}, 400)
            return
        if get_case(case_file) is None:
            self._json({"error": "找不到這篇案例"}, 404)
            return
        stamp = now_iso()
        conn = db()
        try:
            conn.execute(
                "INSERT INTO favorites (user_id, case_file, note, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(user_id, case_file)"
                " DO UPDATE SET note = excluded.note, updated_at = excluded.updated_at",
                (user["id"], case_file, note, stamp, stamp),
            )
            conn.commit()
            row = conn.execute(
                "SELECT case_file, note, created_at, updated_at FROM favorites"
                " WHERE user_id = ? AND case_file = ?",
                (user["id"], case_file),
            ).fetchone()
            fav_count = conn.execute(
                "SELECT COUNT(*) FROM favorites WHERE case_file = ?", (case_file,)
            ).fetchone()[0]
        finally:
            conn.close()
        self._json({"favorite": favorite_entry(row), "fav_count": fav_count})

    def do_DELETE(self):
        parsed = urlparse(self.path)
        if parsed.path != "/api/favorite":
            self._json({"error": "not found"}, 404)
            return
        user = self._require_user()
        if user is None:
            return
        case_file = ((parse_qs(parsed.query).get("case_file") or [""])[0] or "").replace(
            "\\", "/"
        ).strip()
        if not CASE_RE.fullmatch(case_file):
            self._json({"error": "case_file 格式不正確"}, 400)
            return
        conn = db()
        try:
            cur = conn.execute(
                "DELETE FROM favorites WHERE user_id = ? AND case_file = ?",
                (user["id"], case_file),
            )
            conn.commit()
            deleted = cur.rowcount
            fav_count = conn.execute(
                "SELECT COUNT(*) FROM favorites WHERE case_file = ?", (case_file,)
            ).fetchone()[0]
        finally:
            conn.close()
        self._json({"ok": True, "deleted": deleted, "fav_count": fav_count})

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args), flush=True)


def main():
    init_db()
    if SMTP_CONFIG["ready"]:
        print(
            "notify smtp=%s:%d tls=%s from=%s"
            % (
                SMTP_CONFIG["host"],
                SMTP_CONFIG["port"],
                SMTP_CONFIG["tls"],
                SMTP_CONFIG["sender"],
            ),
            flush=True,
        )
    else:
        print(
            "notify smtp=disabled (SMTP_HOST/SMTP_PORT/SMTP_USER/SMTP_PASS are required)",
            flush=True,
        )
    print(
        "serving %s on 0.0.0.0:%d (data=%s, db=%s)" % (WEB_DIR, PORT, DATA_DIR, DB_PATH),
        flush=True,
    )
    threading.Thread(target=notify_loop, name="notify", daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
