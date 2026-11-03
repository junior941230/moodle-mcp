#!/usr/bin/env python3
"""互動式取得 Moodle Mobile Web Service token 並寫入 .env。

用法：
    uv run python scripts/get_token.py

若 Moodle 使用 OIDC / SSO 登入，`login/token.php` 的帳密換 token 通常會
回傳 invalidlogin。本腳本會自動偵測 SSO，並引導你改用「安全金鑰」手動取得。

安全：密碼只存在記憶體，用 getpass 輸入（不回顯），不會寫入檔案或 log。
"""

from __future__ import annotations

import getpass
import os
import sys
from pathlib import Path

import httpx

from moodle_mcp.config import get_settings

# Windows 主控台可能是 cp950；統一用 UTF-8 輸出，避免 emoji / 中文炸掉
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = PROJECT_ROOT / ".env"
DEFAULT_URL = "https://moodle.example.edu"
SERVICE = "moodle_mobile_app"


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values


def upsert_env(path: Path, updates: dict[str, str]) -> None:
    """更新 .env 的指定 key，保留其他設定與註解。"""
    lines: list[str] = []
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()

    pending = dict(updates)
    output: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.partition("=")[0].strip()
            if key in pending:
                output.append(f"{key}={pending.pop(key)}")
                continue
        output.append(line)

    if pending:
        if output and output[-1].strip():
            output.append("")
        for key, value in pending.items():
            output.append(f"{key}={value}")

    path.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")
    restrict_permissions(path)


def restrict_permissions(path: Path) -> None:
    """把 .env 權限設為只有自己可讀寫（0600）。

    POSIX 預設 umask 下新檔案是 0644，同機其他使用者讀得到 token。
    Windows 的 chmod 只影響唯讀旗標，這裡設 0600 等於「可讀寫」，不會有副作用。
    """
    try:
        os.chmod(path, 0o600)
    except OSError as exc:
        print(f"警告：無法限制 {path.name} 的檔案權限（{exc.__class__.__name__}）", file=sys.stderr)


def mask(token: str) -> str:
    if len(token) <= 8:
        return "***"
    return f"{token[:4]}…{token[-4:]}"


def detect_sso(url: str) -> bool:
    """看登入頁是否導向 OIDC / SSO（例如 auth/oidc）。"""
    try:
        response = httpx.get(
            f"{url}/login/index.php",
            timeout=15.0,
            follow_redirects=True,
            verify=get_settings().ssl_verify,
        )
    except httpx.HTTPError:
        return False
    page = response.text.lower()
    return any(
        marker in page
        for marker in ("auth/oidc", "connect/authorize", "ssoam", "openid-connect")
    )


def print_manual_help(url: str) -> None:
    print(
        f"""
此站台使用 SSO / OIDC 登入（偵測到 auth/oidc），Moodle 通常沒有本地密碼，
所以「帳密換 token」會得到 invalidlogin，這是正常的。

請改用 Moodle App 的 launch 流程取得 token（不需要管理員權限）：
  1. 用瀏覽器開啟 {url}，完成 SSO 登入
  2. 開這個網址（passport 隨便填）：
     {url}/admin/tool/mobile/launch.php?service=moodle_mobile_app&passport=mcp&urlscheme=moodlemobile&confirmed=1
  3. 頁面上的「click here to launch the app」是 moodlemobile://token=... 連結，
     在上面按右鍵 →「複製連結網址」
  4. 執行：uv run python scripts/token_from_launch.py
     （或 uv run python scripts/get_token.py --from-launch）
     把連結貼進去，token 會自動驗證並寫入 .env

若偏好設定裡有「安全金鑰」，也可以直接在那裡建立金鑰後貼上。
若 launch 頁面顯示 servicenotavailable / pluginnotenabledorconfigured，
代表站台關掉了行動版 Web Service，請聯絡學校計中 / Moodle 管理者
（關鍵字：enablemobilewebservice）。
""",
        file=sys.stderr,
    )


def prompt_manual_token() -> str | None:
    """讓使用者貼上網頁產生的 token（不回顯）。"""
    token = getpass.getpass("貼上 token（輸入時不顯示，直接 Enter 取消）: ").strip()
    return token or None


def save_token(url: str, token: str) -> int:
    token = token.strip().strip("'\"")
    upsert_env(ENV_PATH, {"MOODLE_URL": url, "MOODLE_TOKEN": token})
    print(f"\n✅ 成功！token（{mask(token)}）已寫入 {ENV_PATH}")
    print("接下來可以執行：uv run python scripts/smoke.py")
    return 0


def try_password_flow(url: str) -> tuple[str | None, str | None]:
    """回傳 (token, errorcode)；連線失敗回傳 (None, None)。"""
    username = input("Moodle 帳號（學號 / 帳號）: ").strip()
    if not username:
        print("錯誤：帳號不可為空。", file=sys.stderr)
        return None, None

    password = getpass.getpass("Moodle 密碼（輸入時不會顯示）: ")
    if not password:
        print("錯誤：密碼不可為空。", file=sys.stderr)
        return None, None

    endpoint = f"{url}/login/token.php"
    print(f"\n正在向 {url} 取得 token…")
    try:
        response = httpx.post(
            endpoint,
            data={
                "username": username,
                "password": password,
                "service": SERVICE,
            },
            timeout=30.0,
            follow_redirects=True,
            verify=get_settings().ssl_verify,
        )
    except httpx.ConnectError as exc:
        message = str(exc)
        print(f"錯誤：無法連線 {url}（ConnectError）", file=sys.stderr)
        if "CERTIFICATE_VERIFY_FAILED" in message or "SSL" in message:
            print(
                "\nTLS 憑證驗證失敗。可能原因與解法：\n"
                "  1. 學校憑證需要 Windows 憑證庫：本專案預設會用 truststore，"
                "請確認已執行 `uv sync`。\n"
                "  2. 公司 / 校園網路有 TLS 攔截：把它的根憑證路徑設到 .env 的 "
                "MOODLE_CA_BUNDLE=...。\n"
                "  3. 最後手段（不建議）：MOODLE_TLS_VERIFY=false。\n"
                f"\n原始錯誤：{message}",
                file=sys.stderr,
            )
        return None, None
    except httpx.HTTPError as exc:
        print(f"錯誤：無法連線 {url}（{exc.__class__.__name__}）", file=sys.stderr)
        return None, None
    finally:
        # 不保留密碼參照
        password = ""

    try:
        payload = response.json()
    except ValueError:
        print(
            f"錯誤：{endpoint} 回應不是 JSON（HTTP {response.status_code}）。\n"
            "請確認 MOODLE_URL 是否正確，以及網站是否開放 Mobile Web Service。",
            file=sys.stderr,
        )
        return None, None

    token = payload.get("token")
    if token:
        return str(token), None

    errorcode = str(payload.get("errorcode") or "")
    error = str(payload.get("error") or "未知錯誤")
    print(f"取得 token 失敗：{error}", file=sys.stderr)
    if errorcode:
        print(f"errorcode: {errorcode}", file=sys.stderr)
    return None, errorcode


def main() -> int:
    if "--from-launch" in sys.argv[1:]:
        # 延後匯入：token_from_launch 會反過來匯入本模組的共用工具
        from token_from_launch import main as launch_main

        return launch_main()

    print("=== Moodle Web Service token 取得工具 ===")
    print("（密碼 / token 都不會被顯示，密碼也不會被儲存）\n")

    existing = read_env(ENV_PATH)
    url = existing.get("MOODLE_URL", "").strip().rstrip("/")
    if not url:
        entered = input(f"Moodle 網址 [{DEFAULT_URL}]: ").strip().rstrip("/")
        url = entered or DEFAULT_URL
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    if existing.get("MOODLE_TOKEN"):
        answer = input(".env 已有 MOODLE_TOKEN，要覆蓋嗎？[y/N]: ").strip().lower()
        if answer not in {"y", "yes"}:
            print("已取消，未修改 .env。")
            return 0

    sso = detect_sso(url)
    if sso:
        print_manual_help(url)
        answer = input("仍要嘗試用帳密換 token 嗎？[y/N]: ").strip().lower()
        if answer not in {"y", "yes"}:
            token = prompt_manual_token()
            if not token:
                print("已取消，未修改 .env。")
                return 0
            return save_token(url, token)

    token, errorcode = try_password_flow(url)
    if token:
        return save_token(url, token)

    if errorcode == "invalidlogin":
        print(
            "\n帳號或密碼不正確；若此站是 SSO 登入，Moodle 就沒有本地密碼，"
            "帳密換 token 不會成功。",
            file=sys.stderr,
        )
        print_manual_help(url)
        answer = input("要改用手動貼上 token 嗎？[y/N]: ").strip().lower()
        if answer in {"y", "yes"}:
            manual = prompt_manual_token()
            if manual:
                return save_token(url, manual)
    elif errorcode in {"enablewsdescription", "servicenotavailable"}:
        print_manual_help(url)

    return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        raise SystemExit(130) from None
