"""設定管理：讀取 .env / 環境變數。"""

from __future__ import annotations

import ssl
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from platformdirs import user_cache_dir
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# src/moodle_mcp/config.py -> src/moodle_mcp -> src -> 專案根目錄
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# 寫入功能：每一項都要在 .env 用獨立的 MOODLE_ALLOW_<名稱>=true 開啟，預設全部關閉。
WRITE_FEATURES: dict[str, str] = {
    "mark_read": "標記通知為已讀",
    "save_submission": "儲存作業草稿（線上文字 / 上傳檔案）",
    "submit_for_grading": "送出作業評分",
    "forum_reply": "回覆討論串",
    "forum_post": "在討論區發新討論",
}


def write_env_name(feature: str) -> str:
    """寫入功能對應的環境變數名稱，例如 MOODLE_ALLOW_FORUM_POST。"""
    return f"MOODLE_ALLOW_{feature.upper()}"


@lru_cache(maxsize=1)
def os_ssl_context() -> ssl.SSLContext | None:
    """使用作業系統憑證庫（Windows 憑證存放區 / macOS Keychain）。

    學校網站的憑證鏈在 certifi 下可能出現
    `Missing Subject Key Identifier`，但 Windows 憑證庫可正常驗證。
    沒有 truststore 時回傳 None，由 httpx 使用預設（certifi）。
    """
    try:
        import truststore

        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:
        return None


class Settings(BaseSettings):
    """moodle-mcp 的所有設定。"""

    model_config = SettingsConfigDict(
        # 後面的檔案優先；因此專案目錄的 .env 會蓋過工作目錄的 .env
        env_file=(".env", str(PROJECT_ROOT / ".env")),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    moodle_url: str = "https://moodle.example.edu"
    # repr=False：避免 token 意外被印到 log / 例外訊息
    moodle_token: str = Field(default="", repr=False)
    moodle_tz: str = "Asia/Taipei"
    moodle_cache_ttl: int = 600
    moodle_max_file_mb: float = 30
    moodle_concurrency: int = 3
    moodle_cache_dir: str | None = None
    moodle_timeout: float = 30.0
    moodle_log_level: str = "INFO"
    # TLS：預設使用作業系統憑證庫；可指定自訂 CA，或在明確了解風險時關閉驗證
    moodle_tls_verify: bool = True
    moodle_ca_bundle: str | None = None

    # 寫入功能（預設全部關閉；名稱需與 WRITE_FEATURES 一致）
    moodle_allow_mark_read: bool = False
    moodle_allow_save_submission: bool = False
    moodle_allow_submit_for_grading: bool = False
    moodle_allow_forum_reply: bool = False
    moodle_allow_forum_post: bool = False
    # 上傳作業檔案時，只允許這個資料夾（含子資料夾）裡的檔案；未設定就不能上傳檔案
    moodle_upload_dir: str | None = None

    @field_validator("moodle_url")
    @classmethod
    def _normalize_url(cls, value: str) -> str:
        value = (value or "").strip().rstrip("/")
        if not value:
            raise ValueError("MOODLE_URL 不可為空")
        if not value.startswith(("http://", "https://")):
            raise ValueError("MOODLE_URL 必須以 http:// 或 https:// 開頭")
        return value

    @field_validator("moodle_concurrency")
    @classmethod
    def _positive_concurrency(cls, value: int) -> int:
        return max(1, min(int(value), 16))

    @field_validator("moodle_max_file_mb")
    @classmethod
    def _positive_file_size(cls, value: float) -> float:
        return max(1.0, float(value))

    @property
    def cache_dir(self) -> Path:
        """快取根目錄（不存在會自動建立）。"""
        raw = self.moodle_cache_dir or user_cache_dir("moodle-mcp")
        path = Path(raw).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def downloads_dir(self) -> Path:
        path = self.cache_dir / "downloads"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def host(self) -> str:
        """MOODLE_URL 的 netloc（小寫），用來驗證下載 URL 是否同站。"""
        return urlparse(self.moodle_url).netloc.lower()

    @property
    def max_file_bytes(self) -> int:
        return int(self.moodle_max_file_mb * 1024 * 1024)

    @property
    def ssl_verify(self) -> bool | str | ssl.SSLContext:
        """httpx 的 verify 參數。

        優先序：關閉驗證 > 自訂 CA bundle > 作業系統憑證庫 > certifi 預設。
        """
        if not self.moodle_tls_verify:
            return False
        if self.moodle_ca_bundle:
            return str(Path(self.moodle_ca_bundle).expanduser())
        context = os_ssl_context()
        return context if context is not None else True

    def write_enabled(self, feature: str) -> bool:
        """某項寫入功能是否已在設定中開啟。"""
        if feature not in WRITE_FEATURES:
            return False
        return bool(getattr(self, f"moodle_allow_{feature}", False))

    def enabled_write_features(self) -> list[str]:
        return [feature for feature in WRITE_FEATURES if self.write_enabled(feature)]

    def require_token(self) -> str:
        """回傳 token；未設定時給出可行動的錯誤。"""
        if not self.moodle_token or not self.moodle_token.strip():
            raise RuntimeError(
                "MOODLE_TOKEN 未設定。請在專案目錄的 .env 填入 token，"
                "或執行 `uv run python scripts/get_token.py` 取得。"
            )
        return self.moodle_token.strip()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reload_settings() -> Settings:
    """測試 / 執行期重新載入設定。"""
    get_settings.cache_clear()
    return get_settings()
