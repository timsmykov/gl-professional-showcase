"""Polling release checks reject stale namespaces, leaked DB secrets and conflicts."""

import importlib.util
from copy import deepcopy
from pathlib import Path

import pytest


@pytest.fixture
def probe():
    path = Path(__file__).resolve().parents[2] / "infra/timeweb/verify-polling.py"
    spec = importlib.util.spec_from_file_location("vm_polling_probe", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def containers(probe):
    state = {"Running": True, "Health": {"Status": "healthy"}}
    bot = {
        "Id": "synthetic-bot-id",
        "Image": "sha256:" + "a" * 64,
        "State": deepcopy(state),
        "Config": {
            "User": "10001:10001",
            "Cmd": ["python", "-m", "gl_bot.poller"],
            "Labels": {
                "com.docker.compose.project": "gl-dev",
                "com.docker.compose.service": "bot",
            },
            "Env": [
                "GL_BOT_API_URL=http://api:8000",
                "TELEGRAM_PROXY_URL=http://127.0.0.1:19881",
                "GL_BOT_API_TOKEN_FILE=/run/secrets/internal_token",
                "TELEGRAM_BOT_TOKEN_FILE=/run/secrets/telegram_bot_token",
                "TELEGRAM_WEBHOOK_SECRET_FILE=/run/secrets/telegram_webhook_secret",
                "PREVIEW_EXPECTED_BOT_ID=987654321",
            ],
        },
        "HostConfig": {
            "ReadonlyRootfs": True,
            "PortBindings": {},
            "NetworkMode": "container:synthetic-proxy-id",
        },
        "Mounts": [
            {
                "Type": "bind",
                "Destination": f"/run/secrets/{name}",
                "Source": str(probe.PRIVATE / "dev" / name),
                "RW": False,
            }
            for name in probe.SECRET_FILES
        ],
    }
    proxy = {
        "Id": "synthetic-proxy-id",
        "State": deepcopy(state),
        "HostConfig": {"PortBindings": {}},
        "NetworkSettings": {"Networks": {"gl-dev_application": {}}},
    }
    return bot, proxy


def test_expected_runtime_has_only_its_read_only_bot_secrets(probe, containers):
    bot, proxy = containers
    probe.verify_containers("dev", bot, proxy, bot["Image"])


def test_full_sdk_probe_has_its_own_memory_limit_and_keeps_real_consumer_namespace(
    probe, containers
):
    bot, _ = containers
    command, environment = probe.metadata_command(bot, bot["Image"])
    assert command[:3] == ["docker", "run", "--rm"]
    assert command[command.index("--memory") + 1] == "256m"
    assert command[command.index("--network") + 1] == "container:" + bot["Id"]
    assert command[-2:] == [bot["Image"], "-"]
    assert "--read-only" in command and "no-new-privileges:true" in command
    assert sum(value.startswith("type=bind,") for value in command) == 3
    assert not any("allowed_user_id" in value for value in command)
    assert all(value.endswith(",readonly") for value in command if value.startswith("type=bind,"))
    assert "987654321" not in command and environment["PREVIEW_EXPECTED_BOT_ID"] == "987654321"
    assert not any(name.startswith("DB_") for name in command)


@pytest.mark.parametrize(
    "failure", ["stale_proxy", "db_secret", "cross_env", "extra_mount", "old_image"]
)
def test_untrusted_runtime_is_rejected(probe, containers, failure):
    bot, proxy = containers
    image = bot["Image"]
    if failure == "stale_proxy":
        bot["HostConfig"]["NetworkMode"] = "container:old-proxy-id"
    elif failure == "db_secret":
        bot["Config"]["Env"].append("DATABASE_URL=postgresql://synthetic")
    elif failure == "cross_env":
        bot["Mounts"][0]["Source"] = "/etc/gl-professional/containers/prod/internal_token"
    elif failure == "extra_mount":
        bot["Mounts"].append({"Type": "bind", "Destination": "/var/run/docker.sock", "RW": True})
    else:
        image = "sha256:" + "b" * 64
    with pytest.raises(AssertionError):
        probe.verify_containers("dev", bot, proxy, image)


@pytest.mark.parametrize(
    "log",
    [
        "preview_poll_failed type=TelegramConflictError",
        "bot_startup_failed type=ValueError",
        "synthetic-private-token",
    ],
)
def test_conflicts_startup_failure_and_known_credentials_refuse_acceptance(probe, log):
    with pytest.raises(AssertionError):
        probe.verify_logs(log, ["synthetic-private-token"])


def test_safe_log_passes_without_disclosing_values(probe):
    probe.verify_logs("restricted Telegram polling started", ["synthetic-private-token"])


@pytest.mark.parametrize("heartbeat", [990.0, 850.0])
def test_healthy_but_stalled_or_stale_heartbeat_cannot_accept(probe, heartbeat):
    elapsed = [0.0]

    def sleep(seconds):
        elapsed[0] += seconds

    with pytest.raises(AssertionError):
        probe.wait_for_heartbeat(
            heartbeat,
            lambda: heartbeat,
            clock=lambda: 1000.0 + elapsed[0],
            monotonic=lambda: elapsed[0],
            sleep=sleep,
        )


def test_two_new_polling_responses_are_required(probe):
    elapsed = [0.0]

    def sleep(seconds):
        elapsed[0] += seconds

    def heartbeat():
        return 1000.0 + 20 * int(elapsed[0] // 20)

    advances = probe.wait_for_heartbeat(
        1000.0,
        heartbeat,
        clock=lambda: 1000.0 + elapsed[0],
        monotonic=lambda: elapsed[0],
        sleep=sleep,
    )
    assert advances == 2 and elapsed[0] >= 40
