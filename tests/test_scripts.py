"""scripts/ 共用工具的測試。"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from get_token import read_env, upsert_env  # noqa: E402
from token_from_launch import extract_token, link_from_args  # noqa: E402


def test_upsert_env_keeps_other_lines(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# 註解\nMOODLE_TZ=Asia/Taipei\nMOODLE_TOKEN=old\n", encoding="utf-8")

    upsert_env(env, {"MOODLE_TOKEN": "new", "MOODLE_URL": "https://moodle.example.edu"})

    text = env.read_text(encoding="utf-8")
    assert "# 註解" in text
    assert read_env(env) == {
        "MOODLE_TZ": "Asia/Taipei",
        "MOODLE_TOKEN": "new",
        "MOODLE_URL": "https://moodle.example.edu",
    }


@pytest.mark.skipif(os.name == "nt", reason="Windows 沒有 POSIX 權限位元")
def test_upsert_env_restricts_permissions(tmp_path):
    env = tmp_path / ".env"
    env.write_text("MOODLE_TOKEN=old\n", encoding="utf-8")
    env.chmod(0o644)

    upsert_env(env, {"MOODLE_TOKEN": "secret"})

    assert stat.S_IMODE(env.stat().st_mode) == 0o600


def test_link_from_args_ignores_from_launch_flag():
    # get_token.py --from-launch 會把自己的 argv 原封不動交給 token_from_launch
    assert link_from_args(["--from-launch"]) == ""
    assert link_from_args([]) == ""
    assert link_from_args(["moodlemobile://token=abc"]) == "moodlemobile://token=abc"


def test_extract_token_from_launch_link():
    import base64

    payload = base64.b64encode(b"md5sig:::the-token:::private").decode()
    assert extract_token(f"moodlemobile://token={payload}") == "the-token"
    assert extract_token("not a link") is None
