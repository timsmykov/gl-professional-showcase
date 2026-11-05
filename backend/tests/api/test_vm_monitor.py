"""Local alarms deduplicate failures and never activate native bot environments."""

import importlib.machinery
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def monitor(tmp_path, monkeypatch):
    filename = Path(__file__).resolve().parents[2] / "infra/timeweb/gl-container-monitor"
    loader = importlib.machinery.SourceFileLoader("vm_monitor", str(filename))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    monkeypatch.setattr(module, "STATE", tmp_path / "state/observations.json")
    monkeypatch.setattr(module, "PRIVATE", tmp_path / "private")
    return module


def test_alarm_is_emitted_once_and_recovery_is_recorded(monitor, capsys):
    monitor.record({"dev.api": False})
    assert capsys.readouterr().out == "<3>gl_alert component=dev.api status=unavailable\n"
    monitor.record({"dev.api": False})
    assert capsys.readouterr().out == ""
    monitor.record({"dev.api": True})
    assert capsys.readouterr().out == "<6>gl_health component=dev.api status=recovered\n"
    assert json.loads(monitor.STATE.read_text()) == {"dev.api": True}
    assert monitor.STATE.stat().st_mode & 0o777 == 0o600


def test_monitor_skips_native_consumers_until_cutover(monitor, monkeypatch):
    calls = []
    monkeypatch.setattr(monitor, "command", lambda *args: calls.append(args) or True)
    monkeypatch.setattr(monitor, "site_available", lambda _domain: True)
    monkeypatch.setattr(
        monitor.shutil,
        "disk_usage",
        lambda _: SimpleNamespace(free=10 * 1024**3, total=40 * 1024**3),
    )
    observations = monitor.collect()
    # Local dump age must not claim anything about provider-managed backups.
    assert not any(key.endswith(".backup") for key in observations)
    assert all(observations.values())
    assert not any(key.endswith((".bot", ".api", ".proxy")) for key in observations)
    assert not any(args[0] == "docker" for args in calls)
    observations = monitor.collect(staged=True)
    assert observations["dev.api"] and observations["prod.api"]
    assert not any(key.endswith(".bot") for key in observations)
