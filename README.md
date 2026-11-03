# moodle-mcp

本機執行、**預設唯讀**的 [MCP](https://modelcontextprotocol.io/) server，讓 Claude Code / Claude Desktop
可以查詢你自己的 Moodle：課程、教材、作業與截止日、公告、成績，並把 PDF / DOCX / PPTX
教材轉成文字閱讀。

- 全部走官方 Moodle Web Service API，不爬 HTML。
- **預設不能寫入**：標記已讀、存作業草稿、送出評分、回覆、發新討論，
  每一項都要在 `.env` 單獨開啟，而且動手前會先請你確認（見[寫入功能](#寫入功能預設全部關閉)）。
- Token 只存在本機 `.env`，不會出現在 log、工具輸出或 git。

> 目標站台：`https://moodle.example.edu`（需已開啟 Mobile Web Service）

> ⚠️ 這是**非官方**的個人專案，與 Moodle 及任何學校無關。AI 的回答可能有誤，
> 截止日與成績請以 Moodle 網站為準。使用前請先看[免責聲明](#免責聲明)。

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
| `get_discussion(discussion_id)` | 整串討論 / 公告內容、附件與每則的 `post_id` |
| `list_forums(course_id)` | 某門課的所有討論區與 `forum_id` |
| `get_grades(course_id?)` | 各課總成績或單課評分項目 |
| `read_file(file_id, offset?, max_chars?)` | 讀取教材檔案並轉文字（可分頁） |
| `notifications(limit=20)` | Moodle 通知與 `notification_id`（不會標記已讀） |
| `clear_cache()` | 清除本機快取，強制更新 |

另有 resources：`moodle://courses`、`moodle://course/{course_id}`，
以及 prompt：`weekly_briefing`（本週待辦整理）。

### 寫入功能（預設全部關閉）

每一項都有自己的開關，沒開的工具**不會註冊**，Claude 完全看不到。

| Tool | 開啟設定 | 動手前確認 |
|---|---|---|
| `mark_notifications_read(notification_id?, mark_all?)` | `MOODLE_ALLOW_MARK_READ=true` | 不需要（低風險） |
| `save_assignment_draft(assign_id, text?, files?)` | `MOODLE_ALLOW_SAVE_SUBMISSION=true` | 需要 |
| `submit_assignment_for_grading(assign_id, accept_statement?)` | `MOODLE_ALLOW_SUBMIT_FOR_GRADING=true` | 需要 |
| `reply_to_discussion(discussion_id, message, post_id?, subject?)` | `MOODLE_ALLOW_FORUM_REPLY=true` | 需要 |
| `create_discussion(forum_id, subject, message)` | `MOODLE_ALLOW_FORUM_POST=true` | 需要 |

改完 `.env` 要重新啟動 server（重開 Claude Code / Claude Desktop）才會生效。

**確認流程**

- 用戶端支援 MCP elicitation（例如 Claude Code）時，server 會直接跳出確認視窗，
  顯示要做的事（作業名稱、要上傳的檔案、要發的內容…），你勾選同意才會執行。
  這個回答不經過 Claude，所以就算 Claude 被惡意內容誘導，也沒辦法替你按同意。
- 不支援時，第一次呼叫只會回傳預覽和一組確認碼；Claude 必須把預覽給你看，
  等你同意後再用**完全相同的參數**加上確認碼呼叫一次。確認碼 10 分鐘內有效、只能用一次，
  參數有任何不同就會失效。這種模式下 Claude 技術上仍可能自己帶確認碼呼叫，
  所以**請保留 Claude 的工具權限詢問，不要對寫入工具選「永遠允許」**。

**作業相關注意事項**

- `save_assignment_draft` 上傳的檔案會**取代**草稿中原本的所有檔案；線上文字也會被取代。
- 有些作業沒有「送出評分」步驟，儲存就等同正式繳交。遇到這種作業時，
  `save_assignment_draft` 還需要同時開啟 `MOODLE_ALLOW_SUBMIT_FOR_GRADING` 才能用，預覽也會特別標示。
- 要求同意「繳交聲明」的作業，Claude 不能替你同意，必須由你本人看過聲明後同意。
- 上傳檔案必須先設定 `MOODLE_UPLOAD_DIR`，只能上傳這個資料夾（含子資料夾）裡的檔案；
  隱藏檔（例如 `.env`）和指向資料夾外的捷徑一律拒絕，避免 Claude 被誘導把私密檔案上傳出去。
  建議專門建一個「要交的作業」資料夾，不要設成家目錄。

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
| `MOODLE_ALLOW_MARK_READ` | 否 | `false` | 允許標記通知為已讀 |
| `MOODLE_ALLOW_SAVE_SUBMISSION` | 否 | `false` | 允許儲存作業草稿（線上文字 / 上傳檔案） |
| `MOODLE_ALLOW_SUBMIT_FOR_GRADING` | 否 | `false` | 允許送出作業評分 |
| `MOODLE_ALLOW_FORUM_REPLY` | 否 | `false` | 允許回覆討論串 |
| `MOODLE_ALLOW_FORUM_POST` | 否 | `false` | 允許在討論區發新討論 |
| `MOODLE_UPLOAD_DIR` | 否 | — | 上傳作業檔案時唯一允許的資料夾；不設定就不能上傳檔案 |

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
| 教材抽取出的文字 | 依 `timemodified`，有變才重抓（檔案本體抽完就刪除） |

快取 key 為 `wsfunction + 參數`，**不含 token**。任何寫入成功後都會清掉 API 快取，避免讀到舊狀態。

---

## 安全設計

1. `MoodleClient.call()` 只允許白名單內的 18 個唯讀 wsfunction，其他一律拒絕（有單元測試）。
   寫入只能走 `MoodleClient.write()`，只允許 6 個寫入 wsfunction，且對應的開關必須開啟；
   沒開啟的寫入工具不會註冊，就算被呼叫也會在執行期再擋一次。
2. Token 不出現在 stdout、log、例外訊息、tool 輸出或快取 key；例外與 log 都經過 `redact()`。
3. `.env` 在 `.gitignore` 中，token 欄位 `repr=False`。
4. `read_file` 只接受 `file_id`，且對應 URL 必須與 `MOODLE_URL` 同 host。
5. 下載有大小上限；快取檔名只用 `file_id` + 安全副檔名，不用伺服器提供的檔名。
6. log 一律寫 stderr（stdout 是 MCP stdio 通道）。
7. 網路錯誤最多重試 3 次（0.5s / 1s / 2s），4xx 不重試；並以 semaphore 限流。
   **寫入一律不重試**，避免網路不穩時重複發文或重複繳交。
8. 除了標記已讀，所有寫入都要使用者確認（優先用 elicitation 直接問使用者），
   寫入前會重新向 Moodle 取得最新狀態，不用快取。
9. 上傳檔案限定在 `MOODLE_UPLOAD_DIR` 內，拒絕隱藏檔、跳出資料夾的捷徑與超過大小上限的檔案。

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

---

## 免責聲明

- **非官方專案**：本專案與 Moodle HQ（Moodle Pty Ltd）及任何學校、機構都沒有關係，
  也未經其認可或背書。「Moodle」是 Moodle Pty Ltd 的商標。
- **請遵守學校的使用規範**：使用前請確認你的學校 / 機構允許用 Web Service token
  搭配第三方工具存取 Moodle。token 等同你的登入身分，請勿分享給他人。
  是否符合規範由使用者自行負責。
- **只能看到你本來就看得到的資料**：本工具只用你自己的 token 呼叫 Moodle API，
  不會也無法繞過 Moodle 的任何權限。
- **寫入動作由你負責**：開啟寫入功能後，Claude 透過本工具做的每個動作
  （交作業、發文、回覆、標記已讀）都是以你的名義完成，和你自己操作沒有差別。
  是否開啟、是否同意每一次的確認，都由你自己決定並承擔後果；
  讓 AI 代寫或代交作業可能違反學校或課程的學術誠信規範，請自行確認。
- **AI 的輸出可能出錯**：截止日、成績、公告等內容請以 Moodle 網站為準。
  因依賴本工具或 AI 的摘要而錯過截止日、誤解內容或造成其他後果，作者不負任何責任。
- **資料會送到你使用的 AI 服務**：透過 Claude Code / Claude Desktop 使用時，
  工具回傳的課程內容、公告、成績等資料會交給該 AI 服務處理，
  適用該服務的使用條款與隱私政策。
- **本機快取含個人資料**：API 回應（包含成績等）會快取在本機的快取目錄。
  在共用電腦上使用時，請在用完後執行 `clear_cache()` 或刪除快取目錄。
- **教材著作權屬於原作者**：讀取到的課程教材僅供你個人學習使用，請勿散布。
- **不提供任何保證**：本軟體依 MIT 授權「按現狀」提供，不附任何明示或默示的保證；
  對於使用本軟體所造成的任何損失，作者不負任何責任。
