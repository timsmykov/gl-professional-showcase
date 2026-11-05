import fcntl
import importlib.machinery
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


def load(name):
    path = Path(__file__).resolve().parents[2] / "infra/timeweb" / name
    loader = importlib.machinery.SourceFileLoader(name.replace("-", "_"), str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_provider_rules_only_enabled_api_uid_network_and_exact_https(tmp_path, monkeypatch):
    guard = load("gl-container-egress")
    monkeypatch.setattr(guard, "PRIVATE", tmp_path)
    (tmp_path / "prod/kinescope").mkdir(parents=True)
    (tmp_path / "prod/kinescope/api_ipv4").write_text("1.1.1.1\n")
    (tmp_path / "network.env").write_text(
        "GL_VPN_IP=8.8.8.8\nGL_DATABASE_IP=9.9.9.9\nGL_VPN_PORT=443\n"
    )
    operations = []

    def run(*args, **kwargs):
        operations.append((args, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(guard, "run", run)
    monkeypatch.setattr("sys.argv", ["guard", "apply"])
    guard.main()
    rules = next(kwargs["data"] for args, kwargs in operations if args[0] == "iptables-restore")
    provider_rules = [line for line in rules.splitlines() if "-d 1.1.1.1" in line]
    assert provider_rules == [
        "-A GL-CONTAINERS-FWD -s 10.240.10.2 -d 1.1.1.1 -p tcp --dport 443 -j ACCEPT"
    ]
    assert "-A GL-CONTAINERS-IN -s 10.241.0.0/16 -j REJECT" in rules
    assert "-A GL-CONTAINERS-FWD -s 10.241.0.0/16 -j REJECT" in rules
    assert "--flush" not in rules and "*nat" not in rules


@pytest.mark.parametrize("value", ["", "127.0.0.1", "192.168.1.1", "1.1.1.0/24", "::1"])
def test_invalid_allowlist_rejected_before_firewall_mutation(tmp_path, monkeypatch, value):
    guard = load("gl-container-egress")
    monkeypatch.setattr(guard, "PRIVATE", tmp_path)
    (tmp_path / "prod/kinescope").mkdir(parents=True)
    (tmp_path / "prod/kinescope/api_ipv4").write_text(value)
    monkeypatch.setattr(guard, "run", lambda *_a, **_k: pytest.fail("firewall changed"))
    monkeypatch.setattr("sys.argv", ["guard", "apply"])
    with pytest.raises(ValueError):
        guard.main()


def test_dns_failure_preserves_previous_rules(tmp_path, monkeypatch):
    refresh = load("gl-kinescope-egress")
    monkeypatch.setattr(refresh, "PRIVATE", tmp_path)
    monkeypatch.setattr(refresh, "LOCK", tmp_path / "lock")
    monkeypatch.setattr(refresh.os, "geteuid", lambda: 0)
    directory = tmp_path / "prod/kinescope"
    directory.mkdir(parents=True)
    (directory / "api_token").write_text("synthetic-key")
    (directory / "api_ipv4").write_text("1.1.1.1\n")

    def fail():
        raise OSError("synthetic DNS unavailable")

    monkeypatch.setattr(refresh, "addresses", fail)
    monkeypatch.setattr(refresh.subprocess, "run", lambda *_a, **_k: pytest.fail("rules changed"))
    with pytest.raises(OSError):
        refresh.main()
    assert (directory / "api_ipv4").read_text() == "1.1.1.1\n"


def test_reload_failure_restores_allowlist_file(tmp_path, monkeypatch):
    refresh = load("gl-kinescope-egress")
    monkeypatch.setattr(refresh, "PRIVATE", tmp_path)
    monkeypatch.setattr(refresh, "LOCK", tmp_path / "lock")
    monkeypatch.setattr(refresh.os, "geteuid", lambda: 0)
    directory = tmp_path / "prod/kinescope"
    directory.mkdir(parents=True)
    (directory / "api_token").write_text("synthetic-key")
    (directory / "api_ipv4").write_text("1.1.1.1\n")
    monkeypatch.setattr(refresh, "addresses", lambda: "8.8.8.8\n")

    def fail(*_a, **_k):
        raise OSError("reload failed")

    monkeypatch.setattr(refresh.subprocess, "run", fail)
    with pytest.raises(OSError):
        refresh.main()
    assert (directory / "api_ipv4").read_text() == "1.1.1.1\n"


def test_real_maintenance_lock_skips_dns_and_rules_without_timer_failure(
    tmp_path, monkeypatch, capsys
):
    refresh = load("gl-kinescope-egress")
    monkeypatch.setattr(refresh, "PRIVATE", tmp_path)
    monkeypatch.setattr(refresh, "LOCK", tmp_path / "lock")
    monkeypatch.setattr(refresh.os, "geteuid", lambda: 0)
    monkeypatch.setattr(refresh, "addresses", lambda: pytest.fail("DNS during maintenance"))
    with refresh.LOCK.open("w") as neighbor:
        fcntl.flock(neighbor, fcntl.LOCK_EX)
        refresh.main()
    assert capsys.readouterr().out == "kinescope_egress_busy\n"
