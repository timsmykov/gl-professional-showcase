"""Validate the actual Compose rendering, including the tmpfs regression."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


def test_dev_compose_keeps_database_private_and_tmpfs_options_together(tmp_path):
    root = Path(__file__).resolve().parents[1]
    compose = root / "compose.dev-bot.yaml"
    if not compose.exists() or not shutil.which("docker"):
        pytest.skip("Host Compose configuration check requires Docker Compose and source file")
    version = subprocess.run(
        ["docker", "compose", "version"], capture_output=True, timeout=10, check=False
    )
    if version.returncode:
        pytest.skip("Docker Compose plugin is unavailable")
    env = {
        **os.environ,
        "GL_BOT_DEV_IMAGE": "synthetic-bot:checked",
        "GL_BOT_DEV_SECRETS_DIR": str(tmp_path),
        "GL_BOT_DEV_INTERNAL_SUBNET": "10.250.1.0/28",
        "GL_BOT_DEV_OUTBOUND_SUBNET": "10.250.2.0/28",
    }
    result = subprocess.run(
        ["docker", "compose", "-f", str(compose), "config", "--format", "json"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    config = json.loads(result.stdout)
    bot, db = config["services"]["bot"], config["services"]["db"]
    assert bot["tmpfs"] == ["/tmp:rw,noexec,nosuid,size=16m"]
    assert bot["read_only"] and bot["cap_drop"] == ["ALL"]
    assert bot["environment"]["BOT_MESSAGE_LABEL"] == ""
    assert not db.get("ports") and set(db["networks"]) == {"internal"}
    assert config["networks"]["internal"]["internal"]
    assert bot["environment"]["PREVIEW_EXPECTED_BOT_ID"] == "8723981790"
    assert bot["environment"]["DB_HOST"] == "db"
    assert config["secrets"]["telegram_bot_token"]["file"] == str(tmp_path / "telegram_bot_token")


def test_dev_catalogue_uses_final_wording_without_test_markers():
    root = Path(__file__).resolve().parents[1]
    seed = root / "docker/dev_seed.sql"
    if not seed.exists():
        pytest.skip("Host seed check requires source file")
    values = seed.read_text().split("\n", 1)[1].lower()
    assert not any(marker in values for marker in ("демо", "demo", "тестов", "демонстрац"))
    assert "покупка и просмотр записи пока недоступны" in values
