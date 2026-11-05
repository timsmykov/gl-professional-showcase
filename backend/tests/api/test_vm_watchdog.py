"""Recovery must respect cutover and recreate a bot after its proxy namespace changes."""

import importlib.machinery
import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def watchdog(tmp_path, monkeypatch):
    filename = Path(__file__).resolve().parents[2] / "infra/timeweb/gl-container-watchdog"
    loader = importlib.machinery.SourceFileLoader("vm_watchdog", str(filename))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    monkeypatch.setattr(module, "PRIVATE", tmp_path)
    monkeypatch.setattr(module, "LOCK", tmp_path / "lock")
    (tmp_path / "dev").mkdir()
    return module


def container(identity, *, running=True, health="healthy", namespace=None):
    return {
        "Id": identity,
        "State": {"Running": running, "Health": {"Status": health}},
        "HostConfig": {"NetworkMode": "container:" + (namespace or identity)},
    }


def test_native_environment_is_never_started_by_watchdog(watchdog, monkeypatch):
    def unexpected(*_args):
        pytest.fail("native polling must remain untouched before cutover")

    monkeypatch.setattr(watchdog, "inspect", unexpected)
    monkeypatch.setattr(watchdog, "compose", unexpected)
    watchdog.main()


def test_proxy_recreation_recovers_bot_in_new_namespace(watchdog, monkeypatch):
    (watchdog.PRIVATE / "dev/enabled").touch()
    inspections = {
        "gl-dev-telegram-egress-1": container("new-proxy"),
        "gl-dev-api-1": container("api"),
        "gl-dev-bot-1": container("bot", namespace="old-proxy"),
    }
    monkeypatch.setattr(watchdog, "inspect", inspections.get)
    calls = []
    monkeypatch.setattr(watchdog, "compose", lambda *args: calls.append(args) or True)
    watchdog.main()
    assert calls == [("dev", "up", "-d", "--no-deps", "--force-recreate", "bot")]


@pytest.mark.parametrize("bot_health", ["healthy", "unhealthy"])
def test_database_outage_does_not_restart_polling(watchdog, monkeypatch, capsys, bot_health):
    (watchdog.PRIVATE / "dev/enabled").touch()
    inspections = {
        "gl-dev-telegram-egress-1": container("proxy"),
        "gl-dev-api-1": container("api", health="unhealthy"),
        "gl-dev-bot-1": container("bot", namespace="proxy", health=bot_health),
    }
    monkeypatch.setattr(watchdog, "inspect", inspections.get)
    calls = []
    monkeypatch.setattr(watchdog, "compose", lambda *args: calls.append(args))
    watchdog.main()
    assert calls == []
    assert capsys.readouterr().out == "gl_api_unavailable environment=dev\n"


def test_recovery_resumes_after_database_is_available(watchdog, monkeypatch):
    (watchdog.PRIVATE / "dev/enabled").touch()
    inspections = {
        "gl-dev-telegram-egress-1": container("new-proxy"),
        "gl-dev-api-1": container("api", health="unhealthy"),
        "gl-dev-bot-1": container("bot", namespace="old-proxy", health="unhealthy"),
    }
    monkeypatch.setattr(watchdog, "inspect", inspections.get)
    calls = []
    monkeypatch.setattr(watchdog, "compose", lambda *args: calls.append(args) or True)
    watchdog.main()
    assert calls == []
    inspections["gl-dev-api-1"] = container("api")
    watchdog.main()
    assert calls == [("dev", "up", "-d", "--no-deps", "--force-recreate", "bot")]
