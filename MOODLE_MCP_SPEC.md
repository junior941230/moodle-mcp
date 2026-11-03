# moodle-mcp 實作規格書

> 給實作 agent：本文件是完整需求與架構。請依「里程碑」順序實作，每個里程碑完成後跑測試再進下一個。
> 目標站台：`https://moodle.example.edu`（已確認 Mobile Web Service 有開）

---

## 1. 目標

做一個**本機執行、唯讀**的 MCP server，讓 LLM（Claude Code / Claude Desktop）可以查詢使用者自己的 Moodle：課程、教材、作業與截止日、公告、成績，並能把教材檔案（PDF / DOCX / PPTX）轉成文字讀取。

### 非目標（第一版不做）
- 任何寫入動作：交作業、發文、標記已讀、改設定
- 多使用者 / 雲端部署
- 爬 HTML（全部走官方 Web Service API）

---

## 2. 技術選型

| 項目 | 選擇 |
|---|---|
| 語言 | Python 3.11+ |
| 套件管理 | `uv` |
| MCP | 官方 `mcp` SDK（`mcp.server.fastmcp.FastMCP`），transport = `stdio` |
| HTTP | `httpx.AsyncClient` |
| 設定 | `pydantic-settings` + `.env` |
| HTML → 文字 | `markdownify`（Moodle 的描述欄位都是 HTML） |
| 文件轉文字 | `pypdf`、`python-docx`、`python-pptx` |
| 快取 | `diskcache`（或 sqlite），存在 `platformdirs.user_cache_dir("moodle-mcp")` |
| 測試 | `pytest`、`pytest-asyncio`、`respx`（mock httpx） |

---

## 3. 專案結構

```
moodle-mcp/
├── pyproject.toml              # entry point: moodle-mcp = "moodle_mcp.server:main"
├── .env.example                # MOODLE_URL=, MOODLE_TOKEN=
├── .gitignore                  # 必須包含 .env
├── README.md                   # 安裝、取得 token、註冊到 Claude 的步驟
├── scripts/
│   ├── get_token.py            # 互動式取得 token（getpass，不回顯密碼）
│   └── smoke.py                # 用真 token 打每個 tool 一次，印摘要
├── src/moodle_mcp/
│   ├── __init__.py
│   ├── server.py               # FastMCP 實例、註冊 tools、main()
│   ├── config.py               # Settings（pydantic-settings）
│   ├── client.py               # MoodleClient：唯一會打 HTTP 的地方
│   ├── errors.py               # MoodleError 系列 + 錯誤碼對應
│   ├── cache.py                # TTL 快取包裝
│   ├── formatting.py           # HTML→markdown、時間戳→Asia/Taipei、截斷
│   ├── files.py                # 下載 + 文字抽取
│   └── tools/
│       ├── courses.py
│       ├── contents.py
│       ├── assignments.py
│       ├── calendar.py
│       ├── forums.py
│       ├── grades.py
│       └── files.py
└── tests/
    ├── fixtures/               # 各 wsfunction 的範例 JSON 回應（去識別化）
    ├── test_client.py
    ├── test_formatting.py
    └── test_tools_*.py
```

---

## 4. 設定與 Token

### 4.1 環境變數
| 變數 | 必填 | 預設 | 說明 |
|---|---|---|---|
| `MOODLE_URL` | 是 | — | `https://moodle.example.edu`（結尾不要 `/`） |
| `MOODLE_TOKEN` | 是 | — | Web service token |
| `MOODLE_TZ` | 否 | `Asia/Taipei` | 時間顯示時區 |
| `MOODLE_CACHE_TTL` | 否 | `600` | 一般 API 快取秒數 |
| `MOODLE_MAX_FILE_MB` | 否 | `30` | 下載檔案大小上限 |
| `MOODLE_CONCURRENCY` | 否 | `3` | 同時請求數上限 |

### 4.2 `scripts/get_token.py`
- 互動輸入帳號，密碼用 `getpass.getpass()`
- `POST {MOODLE_URL}/login/token.php`，form data：`username`、`password`、`service=moodle_mobile_app`
- 成功回傳 `{"token": "...", "privatetoken": "..."}` → 只把 `token` 寫進 `.env`（若已存在則詢問是否覆蓋）
- 失敗回傳 `{"error": "...", "errorcode": "..."}` → 印出 errorcode。如果是 `enablewsdescription` 或 SSO 相關，提示使用者改用手動取得（網頁「偏好設定 → 安全金鑰」）
- **絕對不可**把密碼寫到任何檔案或 log

---

## 5. MoodleClient（`client.py`）

### 5.1 呼叫格式
```
POST {MOODLE_URL}/webservice/rest/server.php
Content-Type: application/x-www-form-urlencoded

wstoken=<token>&wsfunction=<name>&moodlewsrestformat=json&<params>
```

### 5.2 參數扁平化（重要）
Moodle REST 不吃 JSON body，巢狀參數要攤平：
```python
{"courseids": [3, 5]}                  -> courseids[0]=3&courseids[1]=5
{"options": [{"name": "x", "value": 1}]} -> options[0][name]=x&options[0][value]=1
bool                                    -> 1 / 0
```
請實作 `flatten_params(d: dict) -> list[tuple[str, str]]` 並寫單元測試。

### 5.3 錯誤處理
- Moodle 出錯時**HTTP 仍是 200**，body 是 `{"exception": "...", "errorcode": "...", "message": "..."}`，要靠有沒有 `exception` 欄位判斷
- 對應規則：

| errorcode | 例外類別 | 給 LLM 的訊息 |
|---|---|---|
| `invalidtoken` | `AuthError` | Token 無效或過期，請重新執行 `scripts/get_token.py` |
| `accessexception`、`nopermissions`、`requireloginerror` | `PermissionError_` | 沒有權限存取這個資源 |
| `invalidrecord`、`invalidparameter` | `NotFoundError` | 找不到，或參數錯誤 |
| 函式不在 service 內（`servicenotavailable` / `accessexception` 且訊息含 function 名） | `FunctionUnavailable` | 本站未開放此功能 |
| 其他 | `MoodleError` | 原樣附上 message |

- 網路錯誤 / 5xx：指數退避重試 3 次（0.5s、1s、2s），4xx 不重試
- 用 `asyncio.Semaphore(MOODLE_CONCURRENCY)` 限流

### 5.4 白名單（安全關鍵）
`client.call()` 只允許呼叫下列 wsfunction，其他一律拋例外。這是確保唯讀的唯一防線：
```python
ALLOWED_FUNCTIONS = {
    "core_webservice_get_site_info",
    "core_enrol_get_users_courses",
    "core_course_get_enrolled_courses_by_timeline_classification",
    "core_course_get_contents",
    "mod_assign_get_assignments",
    "mod_assign_get_submission_status",
    "core_calendar_get_action_events_by_timesort",
    "core_calendar_get_calendar_upcoming_view",
    "mod_forum_get_forums_by_courses",
    "mod_forum_get_forum_discussions",
    "mod_forum_get_discussion_posts",
    "gradereport_user_get_grade_items",
    "gradereport_overview_get_course_grades",
    "mod_resource_get_resources_by_courses",
    "mod_page_get_pages_by_courses",
    "mod_url_get_urls_by_courses",
    "mod_quiz_get_quizzes_by_courses",
    "message_popup_get_popup_notifications",
}
```

### 5.5 啟動時
- 呼叫 `core_webservice_get_site_info` 一次，快取 `userid`、`fullname`、`sitename`、`functions[]`
- 用 `functions[]` 判斷哪些 tool 可用；不可用的 tool 仍註冊，但呼叫時回傳「本站未開放」而不是噴 stack trace

### 5.6 Token 遮罩
- 任何 log、例外訊息、回給 LLM 的 URL，都要把 `token=xxxx` / `wstoken=xxxx` 換成 `token=***`
- 寫一個 `redact(s: str) -> str` 並在 logging filter 裡套用
- log 一律寫到 **stderr**（stdout 是 MCP stdio 通道，寫進去會讓協定壞掉）

---

## 6. MCP Tools 規格

通用規則：
- 所有 tool 都是 `async`，回傳 **markdown 字串**（LLM 讀起來最省 token），不回傳原始 JSON
- 時間戳（Unix 秒）一律轉成 `YYYY-MM-DD HH:mm (週X)`，時區 `MOODLE_TZ`，並附相對時間，例如「（3 天後）」
- HTML 欄位用 `markdownify` 轉換，並移除 `<img>`、多餘空行
- 每個 tool 輸出超過 `max_chars`（預設 12000）時截斷，結尾附 `…（已截斷，用 offset=N 繼續）`
- tool 的 docstring 要寫清楚用途和參數，LLM 會靠這個判斷要用哪個 tool

### 6.1 `whoami()`
- API：`core_webservice_get_site_info`
- 輸出：姓名、userid、站台名稱、可用功能數量

### 6.2 `list_courses(filter: Literal["inprogress","past","future","all"] = "inprogress")`
- API：`core_course_get_enrolled_courses_by_timeline_classification`（classification=filter）；若不可用，退回 `core_enrol_get_users_courses(userid)`
- 輸出表格：`id | 課程簡稱 | 課程全名 | 進度% | 最後存取`

### 6.3 `get_course_contents(course_id: int, section: int | None = None)`
- API：`core_course_get_contents(courseid)`
- 輸出：依週次 / 章節列出模組，每個模組顯示 `[modname] 名稱 (cmid=…)`；檔案類資源列出檔名、大小、`file_url`（**遮罩 token 前的原始 URL 只存在快取，不顯示給 LLM**，改顯示 `file_id`，見 6.10）
- `section` 有給時只輸出該章節
- 章節 summary 轉 markdown

### 6.4 `list_assignments(course_id: int | None = None, include_past: bool = False)`
- API：`mod_assign_get_assignments(courseids[])`；`course_id` 為 None 時用所有 inprogress 課程
- 每份作業再並行呼叫 `mod_assign_get_submission_status(assignid)` 取得繳交狀態
- 輸出依截止日排序：`截止日 | 課程 | 作業名稱 | 狀態(未繳/已繳/已評分) | assign_id`
- 預設隱藏已過期且已繳交的作業

### 6.5 `get_assignment(assign_id: int)`
- 作業說明（intro 轉 markdown）、附件清單（給 `file_id`）、截止日、遲交截止、繳交狀態、成績與評語（若有）

### 6.6 `upcoming_deadlines(days: int = 14)`
- API：`core_calendar_get_action_events_by_timesort(timesortfrom=now, timesortto=now+days, limitnum=50)`
- 輸出：時間 | 課程 | 事件名稱 | 類型（作業/測驗/其他）
- 這是最常用的 tool，請確保效能（快取 TTL 可短一點，例如 300 秒）

### 6.7 `list_announcements(course_id: int | None = None, limit: int = 10)`
- API：`mod_forum_get_forums_by_courses` → 篩 `type == "news"` → `mod_forum_get_forum_discussions(forumid, sortorder=1, perpage=limit)`
- `course_id` 為 None 時跨所有 inprogress 課程，合併後依時間排序
- 輸出：時間 | 課程 | 標題 | 作者 | discussion_id，加上前 200 字摘要

### 6.8 `get_discussion(discussion_id: int)`
- API：`mod_forum_get_discussion_posts`
- 輸出整串討論，依時間排序，每則含作者、時間、內文（markdown）、附件 `file_id`

### 6.9 `get_grades(course_id: int | None = None)`
- `course_id` 為 None：`gradereport_overview_get_course_grades` → 各課程總成績
- 有 `course_id`：`gradereport_user_get_grade_items(courseid, userid)` → 各評分項目、分數、範圍、百分比、評語

### 6.10 `read_file(file_id: str, offset: int = 0, max_chars: int = 12000)`
- `file_id`：由其他 tool 產生的短 ID（例如原始 fileurl 的 sha1 前 12 碼），對應表存在快取裡。**不要讓 LLM 直接傳任意 URL**，避免被誘導去抓其他網站
- 下載：
  - URL 若是 `/pluginfile.php/...`，改寫成 `/webservice/pluginfile.php/...`
  - 附加 `?token=<token>`（或 `&token=`）
  - 先 HEAD 或看 `filesize`，超過 `MOODLE_MAX_FILE_MB` 就拒絕
  - 存到快取目錄，檔名用 `file_id + 副檔名`（**不要用原始檔名**，防路徑穿越）
- 文字抽取依 MIME / 副檔名：
  - `.pdf` → `pypdf`，每頁前加 `--- 第 N 頁 ---`
  - `.docx` → `python-docx`（段落 + 表格）
  - `.pptx` → `python-pptx`，每張投影片前加 `--- 投影片 N ---`，含備忘稿
  - `.txt` / `.md` / `.csv` / 程式碼 → 直接讀（UTF-8，失敗退 big5 / cp950）
  - `.html` → markdownify
  - 其他（影片、壓縮檔、圖片）→ 回傳檔名、大小、類型，說明無法轉文字
- 抽取結果也快取（key = file_id + timemodified）
- 輸出用 `offset` / `max_chars` 分頁，並標示總字數

### 6.11 `search_course(course_id: int, query: str)`
- 不打新 API：從 `get_course_contents` 的快取結果中，對章節名稱、模組名稱、描述做不分大小寫的子字串搜尋
- 輸出命中的模組清單和所在章節

### 6.12 `notifications(limit: int = 20)`（選做）
- API：`message_popup_get_popup_notifications(useridto=userid, limit)`
- 輸出時間、主旨、是否已讀（**只讀取，不呼叫 mark read**）

---

## 7. MCP Resources / Prompts（選做，M4 再做）
- Resource `moodle://courses` → 同 `list_courses()`
- Resource `moodle://course/{id}` → 同 `get_course_contents(id)`
- Prompt `weekly_briefing` → 指示 LLM 依序呼叫 `upcoming_deadlines(7)`、`list_announcements()`，整理成本週待辦

---

## 8. 快取策略（`cache.py`）

| 資料 | TTL |
|---|---|
| site_info | 整個 process 生命週期 |
| 課程清單 | 1 小時 |
| 課程內容、作業清單 | 10 分鐘 |
| 行事曆、公告 | 5 分鐘 |
| 繳交狀態、成績 | 2 分鐘 |
| 下載的檔案與抽取文字 | 以 `timemodified` 判斷，有變才重抓 |

- 快取 key = `wsfunction + sorted(params)`，**不能包含 token**
- 提供 tool `clear_cache()` 讓使用者強制更新

---

## 9. 安全要求（驗收必查）

1. wsfunction 白名單強制執行，有單元測試證明非白名單函式會被拒絕
2. Token 不出現在：stdout、log、例外訊息、tool 輸出、快取 key、git 追蹤的檔案
3. `.env` 在 `.gitignore` 裡
4. `read_file` 只接受 `file_id`，且 file_id 對應的 URL 必須是 `MOODLE_URL` 同 host
5. 下載檔案大小有上限；快取檔名不使用伺服器提供的字串
6. 不實作任何寫入類 API
7. 限流與重試有上限，不能無限重試打學校伺服器

---

## 10. 測試

- `tests/fixtures/` 放每個 wsfunction 的範例回應（可先用真 token 打一次存下來，**去除姓名、學號、email 後**再提交）
- 必測項目：
  - `flatten_params` 巢狀 list / dict / bool
  - 錯誤 JSON（HTTP 200 + exception）會轉成正確例外類別
  - 白名單拒絕
  - `redact()` 能遮罩 `token=`、`wstoken=`
  - 時間格式化（含跨日、週幾）
  - 每個 tool 用 `respx` mock 後的輸出 snapshot
  - `read_file` 的 PDF / DOCX / PPTX 抽取（fixtures 放小型測試檔）
- `scripts/smoke.py`：用真 token 實際打每個 tool，印出前 300 字，用來手動驗收

---

## 11. 註冊方式（寫進 README）

### Claude Code
```bash
claude mcp add moodle --scope user -- uv --directory /absolute/path/to/moodle-mcp run moodle-mcp
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
Token 由 server 從專案目錄的 `.env` 讀取，不要寫在上面的 config 裡。

---

## 12. 里程碑

| # | 內容 | 完成條件 |
|---|---|---|
| M1 | 專案骨架、config、`MoodleClient`（扁平化、錯誤、白名單、遮罩、重試）、`get_token.py`、`whoami` | 單元測試通過；`smoke.py` 印出自己姓名 |
| M2 | `list_courses`、`get_course_contents`、`upcoming_deadlines`、`list_assignments`、`get_assignment` | 在 Claude Code 問「這週有哪些作業要交」能正確回答 |
| M3 | `read_file`（PDF/DOCX/PPTX）、`list_announcements`、`get_discussion`、`get_grades` | 能讓 LLM 讀一份講義 PDF 並摘要 |
| M4 | 快取完善、`search_course`、`clear_cache`、`notifications`、resources / prompts、README | 第二次呼叫明顯變快；全部測試通過 |

---

## 13. 驗收情境（端到端）

用 Claude Code 連上 server 後，下面這些問題都要能正確回答：
1. 「我這學期修了哪些課？」
2. 「未來兩週有哪些截止日？哪些還沒交？」
3. 「〇〇課最新的公告說了什麼？」
4. 「幫我讀〇〇課第 5 週的講義，整理重點。」
5. 「我〇〇課目前成績怎麼樣？」
6. 嘗試讓 LLM 交作業或發文 → 沒有對應的 tool，做不到
