"""The approved optional receiver must survive boot, recovery and API promotion."""

import importlib.machinery
import importlib.util
from pathlib import Path

import pytest
from test_vm_release import fake_tools, release  # noqa: F401
from test_vm_watchdog import container, watchdog  # noqa: F401


def load(name):
    loader = importlib.machinery.SourceFileLoader(
        name, str(Path(__file__).resolve().parents[2] / "infra/timeweb" / name)
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.mark.parametrize("environment, namespace", [("prod", "production"), ("dev", "sandbox")])
def test_boot_recipe_opt_in_and_wrong_marker_fail_closed(tmp_path, environment, namespace):
    module = load("gl-backend-compose")
    module.PRIVATE = tmp_path / "private"
    module.RUNTIME = tmp_path / "runtime"
    folder = module.PRIVATE / environment / "fiscal"
    folder.mkdir(parents=True)
    module.RUNTIME.mkdir()
    assert module.command(environment, ["up", "-d", "@runtime"])[-3:] == [
        "api",
        "telegram-egress",
        "bot",
    ]
    marker = folder / "receiver_enabled"
    marker.write_text(namespace)
    with pytest.raises(ValueError, match="missing fiscal recipe"):
        module.command(environment, ["up", "@runtime"])
    (module.RUNTIME / "compose.fiscal.yaml").touch()
    command = module.command(environment, ["stop", "@runtime"])
    assert command[-4:] == ["api", "telegram-egress", "bot", "fiscal-receiver"]
    assert str(module.RUNTIME / "compose.fiscal.yaml") in command
    marker.write_text("invalid")
    with pytest.raises(ValueError, match="invalid fiscal receiver"):
        module.command(environment, ["up", "@runtime"])


@pytest.mark.parametrize("health,running", [("unhealthy", True), ("healthy", False)])
def test_receiver_recovery_does_not_restart_for_db_outage(watchdog, monkeypatch, health, running):  # noqa: F811
    (watchdog.PRIVATE / "dev/enabled").touch()
    folder = watchdog.PRIVATE / "dev/fiscal"
    folder.mkdir()
    (folder / "receiver_enabled").write_text("sandbox")
    states = {
        "gl-dev-telegram-egress-1": container("proxy"),
        "gl-dev-api-1": container("api"),
        "gl-dev-bot-1": container("bot", namespace="proxy"),
        "gl-dev-fiscal-receiver-1": container("receipt", running=running, health=health),
    }
    monkeypatch.setattr(watchdog, "inspect", states.get)
    calls = []
    monkeypatch.setattr(watchdog, "compose", lambda *args: calls.append(args) or True)
    watchdog.main()
    assert calls == ([] if running else [("dev", "up", "-d", "--no-deps", "fiscal-receiver")])


def test_api_only_release_updates_receiver_and_keeps_bot(release, monkeypatch):  # noqa: F811
    from types import SimpleNamespace

    folder = release.PRIVATE / "prod/fiscal"
    folder.mkdir(parents=True)
    (folder / "receiver_enabled").write_text("production")
    (folder.parent / "compose.env").write_text(
        "GL_API_IMAGE=old-api\nGL_BOT_IMAGE=old-bot\nGL_FISCAL_IMAGE=old-receipt\n"
    )
    runtime = release.ROOT / "containers/backend"
    runtime.mkdir()
    (runtime / "compose.fiscal.yaml").touch()
    monkeypatch.setattr("sys.argv", ["gl-release", "backend", "prod", "--api-only", "--apply"])
    monkeypatch.setattr(release.subprocess, "run", lambda *_a, **_k: SimpleNamespace(returncode=1))
    calls = fake_tools(release, monkeypatch, component="backend")
    release.main()
    config = (folder.parent / "compose.env").read_text()
    assert "GL_BOT_IMAGE=old-bot" in config
    assert "GL_FISCAL_IMAGE=sha256:" + "1" * 64 in config
    up = next(args for args in calls if args[:2] == ("docker", "compose") and "up" in args)
    assert up[-2:] == ("api", "fiscal-receiver")
    assert str(runtime / "compose.fiscal.yaml") in up
