# 經典案例日刊：公開示範包

[MausBot 教學：用 AI Agent 組行銷編輯部，自動撰寫與發布案例](https://www.letswrite.tw/mausbot-marketing-case/)

一個讀取 Markdown 案例的網站，含閱讀、搜尋、術語、會員收藏與可選的 email 通知。Python 標準庫＋SQLite，前端無需 npm 建置。

本包只含兩篇**虛構示範文章**，沒有會員資料、SMTP 憑證或 AI 團隊設定。

## 1. 用 Docker 啟動（Windows／macOS／Linux）

先決條件：Docker Engine 與 Compose v2；Windows／macOS 可用 Docker Desktop，Windows 採 Linux 容器。Linux 需有啟動 Docker 的權限。首次建置需網路下載 Python 基底映像。

解壓後進入本資料夾。預設網站在 **http://localhost:8801/**，僅綁定本機；請勿與其他服務共用同一個埠。沒有設定 SMTP 時不會寄信。

```sh
docker compose up -d --build
docker compose ps
```

Windows PowerShell 驗證：

```powershell
(Invoke-WebRequest http://localhost:8801/healthz -UseBasicParsing).StatusCode
(Invoke-WebRequest http://localhost:8801/api/cases -UseBasicParsing).Content
```

macOS／Linux 驗證：

```sh
curl -f http://localhost:8801/healthz
curl -f http://localhost:8801/api/cases
```

應為 200，清單應有 2026/01/02.md 與 2026/01/05.md。開網頁可匿名閱讀；收藏時自行註冊示範帳號，不需預設管理員密碼。

```sh
docker compose logs --tail 30
docker compose down
```

down 只停止這個 Compose 專案，會員與收藏仍保留在本目錄 db-data/。備份資料前先停止服務，再備份 db-data/。不要把它或 .env 上傳 GitHub。此包沒有附這些檔案。

## 2. 設定埠與 SMTP（可選）

Windows 複製設定範本：

```powershell
Copy-Item .env.example .env
```

macOS／Linux：

```sh
cp .env.example .env
```

編輯 .env，再執行 `docker compose up -d --build`。改埠時請一併改 DASHBOARD_URL，讓信件連結正確。

| 設定                                       | 用途                                                                        |
| ------------------------------------------ | --------------------------------------------------------------------------- |
| APP_PORT                                   | 本機連接埠，預設 8801；容器內固定 80                                        |
| DASHBOARD_URL                              | 信件頁尾連結，預設 http://localhost:8801/；遠端收件人需使用他們可開啟的網址 |
| SMTP_HOST、SMTP_PORT、SMTP_USER、SMTP_PASS | 全部填妥才可開啟通知；預設留白，不寄信                                      |
| SMTP_FROM、SMTP_TLS                        | 寄件人預設等於帳號；TLS 可用 starttls、ssl，none 僅供可信本機測試           |
| COOKIE_SECURE、NOTIFY_POLL_SECONDS         | HTTPS 反向代理時設 COOKIE_SECURE=1；掃描間隔預設 300 秒                     |

通知需登入後由會員自行開啟。SMTP 未備妥時回 409。程式要求 SMTP 帳密，不支援無驗證 relay。信件含全文 HTML 與純文字兩種格式。啟動時以目前最新文章建立監看起點；只處理執行期間新增、路徑較新的文章，不補寄啟動前既有文章或較早日期補刊。成功寄送後記錄去重，同一輪失敗下輪重試；重啟後不保證補寄重啟前失敗的待寄文章。寄送成功但去重紀錄寫入失敗時仍可能重寄。

## 3. 不用 Docker：Python 直接啟動

先決條件：Python 3.10 以上；本次以 3.14 驗證。只用標準庫，不需 pip install。.env 不會由 Python 自動讀取；請在同一個終端機設定環境變數。

Windows PowerShell：

```powershell
$env:DATA_DIR=(Get-Location).Path
$env:PORT='8801'
$env:DASHBOARD_URL='http://localhost:8801/'
python -B server.py
```

macOS／Linux：

```sh
DATA_DIR="$PWD" PORT=8801 DASHBOARD_URL=http://localhost:8801/ python3 -B server.py
```

直接啟動會監聽所有網路介面；需要僅供本機時，使用上面的 Docker 設定。Ctrl+C 停止。若終端機原本已有 SMTP\_\*，請先清空才能確保測試不寄信。DB_PATH 可另外指定資料庫位置，預設為 DATA_DIR/appdata/favorites.db；Docker 透過 volume 將它放在本機 db-data/。

## 4. 新增文章

在 articles/YYYY/MM/DD.md 建立新檔，參照兩篇示範。路徑年月日固定補零；日期為出刊日，一期一檔。程式只依檔名格式辨識，作者需自行確保日期真實存在、期號連續。

```markdown
# 經典案例日刊｜#003｜你的案例標題

> 2026-01-06｜主題：留存

## 案例拆解

寫下已查證的內容。

## 來源

- https://example.com/
```

只新增文章不必重建容器。網頁手動重新整理，或等 60 秒清單偵測；同一路徑文章改稿需重整整個網頁，因前端會快取已讀取全文。修改 server.py 或 web/ 才需重建。

## 5. MausBot 團隊與檔案

**這個專案是網站程式。** 解壓不會產生 AI 團隊、選題能力、模型憑證、每日排程或發文帳號。請在 MausBot 自行建立總編輯／選題編輯／工程師、授權檔案操作，再安排出刊。可複用 [角色與出刊步驟範本](docs/mausbot-team-template.md)；它是自行撰寫的範例，沒有附外部 skills。

2026-10-28 查核的官方資源：

- [MausBot 官方介紹](https://mausbot.com/)（安裝程式仍可能使用 OpenMausBot 名稱）
- [官方下載](https://mausbot.com/download)（macOS／Windows／Linux；模型服務費用另計）
- [原始碼與版本](https://github.com/milind-soni/OpenMausBot)

| 檔案                                                        | 用途                                     |
| ----------------------------------------------------------- | ---------------------------------------- |
| server.py、web/                                             | HTTP API、SQLite、會員、通知；執行期前端 |
| Dockerfile、docker-compose.yml、.dockerignore、.env.example | 啟動與設定，不含真實憑證                 |
| articles/                                                   | 示範文章                                 |
| docs/                                                       | 角色範本與功能說明                       |

功能與限制見 [docs/feature-facts.md](docs/feature-facts.md)。macOS／Linux 主機與真實 SMTP 尚未實測。這是小型閱讀工具，對外服務前仍需自行安排 HTTPS、備份、資源限制及帳號維運；目前沒有信箱驗證、忘記密碼或管理後台。
