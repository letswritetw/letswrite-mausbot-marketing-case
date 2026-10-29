# 功能事實與出刊流程

本文件整理網站的實際行為與限制。網站本身不會產生 AI 團隊，那需要另外在 MausBot 設定。

## 閱讀與找文章

| 能力 | 實際行為／限制 | 對應檔案與函式 |
| --- | --- | --- |
| 清單與全文 | API 讀 articles/YYYY/MM/DD.md，日期倒序；匿名可讀 | server.py: list_cases、get_case、Handler.do_GET |
| 搜尋 | 前端下載文章並搜尋日期、主題、標題、全文；空白分詞且全部詞都需符合；非伺服器搜尋引擎 | web/index.html: terms、haystack、visibleCases、preloadAll |
| 主題 | 卡片標示主題、側欄統計篇數；可輸入主題文字搜尋。沒有獨立主題篩選按鈕 | web/index.html: renderList、renderSide |
| 術語 | 固定 GLOSSARY 字典；tier 2 命中後標示並解釋，tier 1 不標示；無命中會顯示空狀態。不是 AI 自動解釋新詞 | web/index.html: GLOSSARY、pickGlossTerms、glossaryHtml |
| 來源、摘要、目錄 | 特定「來源」段落搬到閱讀側欄；依標題／獨立粗體段落建立最多 6 項目錄；擷取特定資料段落最多 4 點摘要；捲動更新目前目錄位置 | web/index.html: renderBody、articleParts、summaryData、summaryHtml、initToc |
| 閱讀狀態 | 有上一篇／下一篇、回列表與捲動位置恢復；「閱讀進度」是目前目錄項目高亮，沒有跨裝置百分比／已讀同步 | web/index.html: pnNavHtml、renderRoute、setTocActive |
| 外觀 | 亮暗模式、響應式排版、鍵盤操作與原生 dialog | web/index.html: CSS、theme 與事件處理 |
| 新刊偵測 | 清單每 60 秒比對檔名；新路徑會刷新，同路徑改稿不觸發且全文有快取，需整頁重整 | web/index.html: refresh、ensureText、setInterval |

## 個人資料與統計

| 能力 | 實際行為／限制 | 對應檔案與函式 |
| --- | --- | --- |
| 會員 | Email＋密碼就地註冊登入；密碼以 scrypt 或 PBKDF2＋salt 儲存；session cookie，30 天有效期；收藏與通知需登入 | server.py: hash_password、check_password、create_session、Handler |
| 收藏註記 | 每位會員獨立收藏；同文章重存更新註記，不增加同一人的收藏數；註記最多 2000 字；SQLite 永久儲存 | server.py: list_favorites、Handler.do_PUT/do_DELETE；web/index.html: saveFavorite、removeFavorite |
| 匯出 | 下載自己的全部收藏成 Markdown：標題、日期、主題、文章連結、註記；不是匯出全站文章或其他會員資料，也不是資料庫備份 | web/index.html: favoritesMarkdown、exportFavorites |
| 瀏覽數 | POST /api/view；同讀者同文章每日一次，日期依 UTC。匿名以 cookie 雜湊辨識；登入以會員識別；讀取全文 GET 本身不計數 | server.py: record_view、Handler._view_key；web/index.html: recordView |
| 收藏數 | API 回傳每篇所有會員收藏總數 fav_count，新增／取消後更新前端；不同人可以各計一次 | server.py: case_stats、attach_stats、Handler.do_PUT/do_DELETE；web/index.html: updateStatsDom |

瀏覽統計是近似使用訊號：清 cookie、換瀏覽器、登入前後切換身分可能各計一次；前端同頁生命週期也會限制重送。沒有即時推播給其他讀者，清單定時偵測主要看檔名，統計可手動重新整理。

## 訂閱信

server.py 的 case_email_message 建立 multipart/alternative，含 UTF-8 純文字與 HTML 全文。build_case_email_parts 使用簡化 Markdown 排版、HTML escape、600px 內容欄、頁尾儀表板連結；「可以怎麼落地應用」標題有獨立樣式（server.py 靠這個標題辨識）。它不是完整 CommonMark 引擎，並非所有 Markdown 寫法皆有完整轉換。

通知啟動與防重：notify_loop → 啟動時 latest_case_file 作為起點 → 每 NOTIFY_POLL_SECONDS 秒 notify_scan → pending_notify_users → send_case_email → 成功後 mark_notify_sent。只處理比起點更新的檔名，啟動前／較早補刊不補寄。SMTP 失敗不寫成功紀錄，下輪重試；成功寫入 notify_sent 後跳過。若服務重啟，起點重設到最新文章，重啟前未完成的寄送不保證補寄；SMTP 已收信但寫 DB 失敗可能重寄，並非嚴格 exactly-once。通知設定在掃描當下判定，沒有每位會員的個別訂閱時間游標。

本次只驗證 MIME 與模擬傳送／失敗重試，沒有寄真信、沒有 Gmail 或外部 SMTP 整合驗證。

## 出刊流程

MausBot 選題／查證／定稿（另需設定）→ 儲存 articles/YYYY/MM/DD.md → 網站讀取檔案，清單約每 60 秒偵測新檔名。本專案沒有發布腳本，不會自動 commit 或 push。

單日唯一出刊、避免覆寫、來源查證與選題編輯／總編輯分工，要由 MausBot 的角色與排程流程另外執行。網站 API 沒有寫入文章的功能。
