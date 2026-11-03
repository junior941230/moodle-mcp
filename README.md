# moodle-mcp

本機執行、**唯讀**的 [MCP](https://modelcontextprotocol.io/) server，讓 Claude Code / Claude Desktop
可以查詢你自己的 Moodle：課程、教材、作業與截止日、公告、成績，並把 PDF / DOCX / PPTX
教材轉成文字閱讀。

- 全部走官方 Moodle Web Service API，不爬 HTML。
- **沒有任何寫入功能**：不能交作業、發文、標記已讀或改設定。
- Token 只存在本機 `.env`，不會出現在 log、工具輸出或 git。

> 目標站台：`https://moodle.example.edu`（需已開啟 Mobile Web Service）

---

## 需求

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)
- 有效的 Moodle Web Service token

## 安裝

```bash
git clone <this-repo> moodle-mcp
cd moodle-mcp
uv sync
cp .env.example .env        # Windows: copy .env.example .env
```

### 取得 token

> **SSO 站台注意**：若你的 Moodle 使用 OIDC / SSO 登入，Moodle 沒有本地密碼，
> `login/token.php` 的帳密流程一定回傳 `invalidlogin`；而且若「偏好設定」裡
> **沒有「安全金鑰」**（未開放 `moodle/webservice:createtoken`），網頁建金鑰也行不通。
> 請用下面方法 A（Moodle App 的 launch 流程，不需管理員權限）。

**方法 A（SSO 站台推薦）：用 Moodle App 的 launch 連結取 token**

1. 瀏覽器先登入 <https://moodle.example.edu>
2. 開這個網址（`passport` 隨便填）：

   ```text
   https://moodle.example.edu/admin/tool/mobile/launch.php?service=moodle_mobile_app&passport=mcp&urlscheme=moodlemobile&confirmed=1
   ```

3. 頁面會出現「click here to launch the app」連結，內容是
   `moodlemobile://token=....`；在連結上按右鍵 →「複製連結網址」
4. 執行腳本並貼上：

   ```bash
   uv run python scripts/get_token.py --from-launch
   ```

   出現提示後再貼上連結（輸入不會顯示）。不要把連結直接寫在指令參數裡，
   否則 token 會留在 shell 歷史紀錄中。

   腳本會解出 token、用 `core_webservice_get_site_info` 驗證，成功才寫入 `.env`。
   連結約 15 分鐘內有效，過期就回到步驟 2 重新產生。

**方法 B：網頁「安全金鑰」（站台有開放時才看得到）**

1. 登入 Moodle → 右上角頭像 →「偏好設定」
2. 「安全金鑰」(Security keys) → 建立 / 查看 **Mobile Web Service** 金鑰
3. 把金鑰填入 `.env`：

```dotenv
MOODLE_URL=https://moodle.example.edu
MOODLE_TOKEN=你的token
```

**方法 C：帳密換 token（僅限有本地密碼的站台）**

```bash
uv run python scripts/get_token.py
```

腳本會用 `getpass` 讀密碼（不回顯、不寫檔），成功後只把 `token` 寫進 `.env`。
SSO 站台會得到 `invalidlogin`，腳本會自動改為引導方法 A。

> 不要把 token 寫進 Claude 的設定檔，也不要 commit `.env`（已在 `.gitignore`）。

### 驗證

```bash
uv run python scripts/smoke.py
```

會依序呼叫每個 tool 並印出前 300 字；第一個 `whoami` 應該顯示你的姓名。

---

## 註冊到 Claude

### Claude Code

```bash
claude mcp add moodle --scope user -- uv --directory /absolute/path/to/moodle-mcp run moodle-mcp
```

Windows 範例：

```powershell
claude mcp add moodle --scope user -- uv --directory C:\Users\you\Desktop\moodleMCP run moodle-mcp
```

### Claude Desktop（`claude_desktop_config.json`）

```json
{
  "mcpServers": {
    "moodle": {
      "command": "uv",
      "args": ["--directory", "C:\\path\\to\\moodle-mcp", "run", "moodle-mcp"]
    }
  }
}
```

Token 由 server 從專案目錄的 `.env` 讀取，**不要**寫在上面的 config 裡。

---

## 提供的 tools

| Tool | 用途 |
|---|---|
| `whoami()` | 確認 token、姓名、userid、可用功能數 |
| `list_courses(filter)` | 課程清單（inprogress / past / future / all） |
| `get_course_contents(course_id, section?)` | 章節與教材 / 活動，檔案附 `file_id` |
| `search_course(course_id, query)` | 用快取搜尋章節、模組名稱與描述 |
| `list_assignments(course_id?, include_past?)` | 作業、截止日、繳交狀態 |
| `get_assignment(assign_id)` | 作業說明、附件、成績與評語 |
| `upcoming_deadlines(days=14)` | 未來 N 天的截止日 / 事件 |
| `list_announcements(course_id?, limit=10)` | 課程公告（討論區 type=news） |
| `get_discussion(discussion_id)` | 整串討論 / 公告內容與附件 |
| `get_grades(course_id?)` | 各課總成績或單課評分項目 |
| `read_file(file_id, offset?, max_chars?)` | 讀取教材檔案並轉文字（可分頁） |
| `notifications(limit=20)` | Moodle 通知（唯讀，不標記已讀） |
| `clear_cache()` | 清除本機快取，強制更新 |

另有 resources：`moodle://courses`、`moodle://course/{course_id}`，
以及 prompt：`weekly_briefing`（本週待辦整理）。

### 檔案讀取流程

`get_course_contents` / `get_assignment` / `get_discussion` 不會把原始檔案 URL 給 LLM，
而是給一個短 `file_id`。`read_file(file_id)` 只接受這個 ID，並驗證 URL 與 `MOODLE_URL` 同站，
避免被誘導去抓其他網站。

支援的文字抽取：

- `.pdf` → `pypdf`，附頁碼
- `.docx` → `python-docx`（段落 + 表格）
- `.pptx` → `python-pptx`（含備忘稿）
- `.txt` / `.md` / `.csv` / 程式碼 → UTF-8，失敗退 big5 / cp950
- `.html` → markdown
- 影片 / 壓縮檔 / 圖片 → 只回傳檔名、大小、類型

---

## 設定

| 變數 | 必填 | 預設 | 說明 |
|---|---|---|---|
| `MOODLE_URL` | 是 | — | 站台網址（結尾不要 `/`） |
| `MOODLE_TOKEN` | 是 | — | Web Service token |
| `MOODLE_TZ` | 否 | `Asia/Taipei` | 時間顯示時區 |
| `MOODLE_CACHE_TTL` | 否 | `600` | 一般 API 快取秒數 |
| `MOODLE_MAX_FILE_MB` | 否 | `30` | 下載檔案大小上限 |
| `MOODLE_CONCURRENCY` | 否 | `3` | 同時請求數上限 |
| `MOODLE_CACHE_DIR` | 否 | `user_cache_dir("moodle-mcp")` | 快取目錄 |
| `MOODLE_TIMEOUT` | 否 | `30` | HTTP 逾時秒數 |
| `MOODLE_LOG_LEVEL` | 否 | `INFO` | log 等級（一律寫 stderr） |
| `MOODLE_TLS_VERIFY` | 否 | `true` | TLS 驗證；關閉僅供最後手段 |
| `MOODLE_CA_BUNDLE` | 否 | — | 自訂 CA bundle 路徑（TLS 攔截環境用） |

> TLS 預設使用作業系統憑證庫（`truststore`）。某些學校憑證在 Python 內建 certifi
> 下會出現 `CERTIFICATE_VERIFY_FAILED: Missing Subject Key Identifier`，改用
> Windows 憑證庫即可正常連線。

### 快取

| 資料 | TTL |
|---|---|
| site_info | process 生命週期 |
| 課程清單 | 1 小時 |
| 課程內容、作業清單 | 10 分鐘 |
| 行事曆、公告 | 5 分鐘 |
| 繳交狀態、成績 | 2 分鐘 |
| 下載檔案 / 抽取文字 | 依 `timemodified`，有變才重抓 |

快取 key 為 `wsfunction + 參數`，**不含 token**。

---

## 安全設計

1. `MoodleClient.call()` 只允許白名單內的 18 個唯讀 wsfunction，其他一律拒絕（有單元測試）。
2. Token 不出現在 stdout、log、例外訊息、tool 輸出或快取 key；例外與 log 都經過 `redact()`。
3. `.env` 在 `.gitignore` 中，token 欄位 `repr=False`。
4. `read_file` 只接受 `file_id`，且對應 URL 必須與 `MOODLE_URL` 同 host。
5. 下載有大小上限；快取檔名只用 `file_id` + 安全副檔名，不用伺服器提供的檔名。
6. log 一律寫 stderr（stdout 是 MCP stdio 通道）。
7. 網路錯誤最多重試 3 次（0.5s / 1s / 2s），4xx 不重試；並以 semaphore 限流。

---

## 開發

```bash
uv sync
uv run pytest
```

測試用 `respx` mock HTTP，不需要真 token；fixtures 在 `tests/fixtures/`（已去識別化）。

專案結構：

```
src/moodle_mcp/
├── server.py        # FastMCP、tools/resources/prompts 註冊、main()
├── config.py        # pydantic-settings
├── client.py        # 唯一打 HTTP 的地方（白名單、重試、限流、快取）
├── errors.py        # 例外、錯誤碼對應、redact、tool_guard
├── cache.py         # diskcache TTL + file_id 對應表
├── formatting.py    # HTML→markdown、時間、表格、截斷
├── files.py         # 下載與 PDF/DOCX/PPTX 文字抽取
└── tools/           # 每個 MCP tool 的實作
```

## 疑難排解

- **`本站未開放此功能`**：此 token 的 service 沒有該 wsfunction。請確認使用
  `moodle_mobile_app` service，或請管理者開啟對應功能。工具仍會註冊，只回傳訊息、不噴錯。
- **`Token 無效或已過期`**：重新執行 `scripts/get_token.py`。
- **`ConnectError` / `CERTIFICATE_VERIFY_FAILED`**：先確認網路可通
  （`Test-NetConnection moodle.example.edu -Port 443`）。若是憑證錯誤，
  本專案已預設用 `truststore` 讀 Windows 憑證庫；仍失敗時可用
  `MOODLE_CA_BUNDLE` 指定校園網路的根憑證。關閉驗證（`MOODLE_TLS_VERIFY=false`）
  只建議在完全信任網路環境下短期使用。
- **無法下載檔案 / 403**：確認 token 有效，且檔案屬於你有權限的課程。
- **Claude 看不到 server**：先在本機跑 `uv run moodle-mcp`，確認 stderr 沒有設定錯誤。
  MCP 的 stdout 只放協定訊息，除錯請看 stderr。
