#!/usr/bin/env python3
"""從 Moodle App 的 launch 連結取出 Web Service token，並寫入 .env。

背景：若 Moodle 用 OIDC / SSO 登入，「偏好設定 → 安全金鑰」沒有開放，
所以無法用 `login/token.php`（帳密換 token）或網頁建立金鑰。

替代做法（Moodle App 的登入流程，不需要管理員權限）：
  1. 瀏覽器先登入 https://moodle.example.edu
  2. 開這個網址（passport 可隨便填）：
     https://moodle.example.edu/admin/tool/mobile/launch.php?service=moodle_mobile_app&passport=mcp&urlscheme=moodlemobile&confirmed=1
  3. 頁面會有「click here to launch the app」連結，內容長這樣：
     moodlemobile://token=TWQ1Y2...（一整串 base64）
     在連結上按右鍵 →「複製連結網址」
  4. 把整串貼給本腳本，token 會自動取出、驗證、寫入 .env

用法：
    uv run python scripts/token_from_launch.py
    （執行後再貼上連結；輸入不會顯示，也不會留在 shell 歷史紀錄）

安全：token 不會被印出（只顯示前後 4 碼），也不會出現在任何 log。
"""

from __future__ import annotations

import base64
import binascii
import getpass
import sys
from pathlib import Path

import httpx

# Windows 主控台可能是 cp950；統一用 UTF-8 輸出
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent))

from get_token import DEFAULT_URL, ENV_PATH, mask, read_env, save_token  # noqa: E402

from moodle_mcp.config import get_settings  # noqa: E402

SERVICE = "moodle_mobile_app"
REST_PATH = "/webservice/rest/server.php"


def extract_token(raw: str) -> str | None:
    """從 `moodlemobile://token=<base64>`、純 base64 或已解碼字串取出 token。

    連結格式：base64(md5(wwwroot + passport) ::: token [::: privatetoken])
    """
    text = raw.strip().strip("'\"")
    if not text:
        return None

    payload = text
    if "token=" in text:
        payload = text.split("token=", 1)[1]
        payload = payload.split("&", 1)[0]

    payload = payload.strip()
    if not payload:
        return None

    # 已經被解碼過（含 ::: 分隔）就直接取第二段
    if ":::" in payload:
        parts = payload.split(":::")
        if len(parts) >= 2 and parts[1].strip():
            return parts[1].strip()

    padded = payload + "=" * (-len(payload) % 4)
    try:
        decoded = base64.urlsafe_b64decode(padded).decode("utf-8", "replace")
    except (binascii.Error, ValueError):
        return None

    parts = decoded.split(":::")
    if len(parts) < 2:
        return None
    token = parts[1].strip()
    return token or None


def verify_token(url: str, token: str) -> dict | None:
    """用 core_webservice_get_site_info 驗證 token；成功回傳 site info。"""
    try:
        response = httpx.post(
            url + REST_PATH,
            data={
                "wstoken": token,
                "wsfunction": "core_webservice_get_site_info",
                "moodlewsrestformat": "json",
            },
            timeout=30.0,
            follow_redirects=True,
            verify=get_settings().ssl_verify,
        )
    except httpx.ConnectError as exc:
        message = str(exc)
        print(f"錯誤：無法連線 {url}（{exc.__class__.__name__}）", file=sys.stderr)
        if "CERTIFICATE_VERIFY_FAILED" in message or "SSL" in message:
            print(
                "TLS 憑證驗證失敗，請確認已 `uv sync`（會安裝 truststore），"
                "或設定 .env 的 MOODLE_CA_BUNDLE。",
                file=sys.stderr,
            )
        return None
    except httpx.HTTPError as exc:
        print(f"錯誤：連線失敗（{exc.__class__.__name__}）", file=sys.stderr)
        return None

    try:
        payload = response.json()
    except ValueError:
        print(f"錯誤：回應不是 JSON（HTTP {response.status_code}）", file=sys.stderr)
        return None

    if isinstance(payload, dict) and payload.get("errorcode"):
        print(
            f"token 驗證失敗：{payload.get('message') or payload.get('errorcode')}"
            f"（{payload.get('errorcode')}）",
            file=sys.stderr,
        )
        if payload.get("errorcode") in {"invalidtoken", "accessexception"}:
            print(
                "可能原因：連結過期（launch 連結約 15 分鐘內有效）、"
                "已取用過，或 token 貼錯。請重新產生 launch 連結再試一次。",
                file=sys.stderr,
            )
        return None

    return payload if isinstance(payload, dict) else None


def link_from_args(argv: list[str]) -> str:
    """命令列上的連結；忽略 get_token.py 轉交過來的 --from-launch 旗標。"""
    return " ".join(arg for arg in argv if arg != "--from-launch").strip()


def main() -> int:
    print("=== 從 Moodle App launch 連結取出 token ===\n")

    existing = read_env(ENV_PATH)
    url = existing.get("MOODLE_URL", "").strip().rstrip("/") or DEFAULT_URL

    if existing.get("MOODLE_TOKEN"):
        answer = input(".env 已有 MOODLE_TOKEN，要覆蓋嗎？[y/N]: ").strip().lower()
        if answer not in {"y", "yes"}:
            print("已取消，未修改 .env。")
            return 0

    raw = link_from_args(sys.argv[1:])
    if raw:
        print(
            "警告：連結直接寫在指令參數裡，token 可能留在 shell 歷史紀錄 / 行程清單中。\n"
            "建議下次不帶參數執行，等提示出現再貼上。",
            file=sys.stderr,
        )
    else:
        print(
            "請貼上 launch 連結（moodlemobile://token=... 整串，或只要那段 base64）。\n"
            f"取得方式：登入 {url} 後開啟\n"
            f"  {url}/admin/tool/mobile/launch.php"
            "?service=moodle_mobile_app&passport=mcp&urlscheme=moodlemobile&confirmed=1\n"
            "頁面上的「click here to launch the app」連結按右鍵 → 複製連結網址。\n",
            file=sys.stderr,
        )
        raw = getpass.getpass("貼上連結（輸入時不顯示，直接 Enter 取消）: ").strip()
        if not raw:
            print("已取消，未修改 .env。")
            return 0

    token = extract_token(raw)
    if not token:
        print(
            "錯誤：無法從這段內容取出 token。\n"
            "請確認貼上的連結包含 `token=` 後面那串 base64，且沒有被截斷。",
            file=sys.stderr,
        )
        return 1

    print(f"已取出 token（{mask(token)}），正在向 {url} 驗證…")
    info = verify_token(url, token)
    if not info:
        return 1

    print(
        "驗證成功："
        f"{info.get('fullname', '?')}（{info.get('username', '?')}）@ "
        f"{info.get('sitename', '?')}，Moodle {info.get('release', '?')}"
    )
    functions = info.get("functions") or []
    if isinstance(functions, list):
        print(f"此 token 可用的 Web Service 函式：{len(functions)} 個")

    return save_token(url, token)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        raise SystemExit(130) from None
