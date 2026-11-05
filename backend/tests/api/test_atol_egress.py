"""Prepared payment allowlist: no host firewall mutations in unit tests."""

import fcntl
import importlib.machinery
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


def load(name):
    path = Path(__file__).resolve().parents[2] / "infra/timeweb" / name
    loader = importlib.machinery.SourceFileLoader(
        name.replace("-", "_").replace(".", "_"), str(path)
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def prepare(module, tmp_path, monkeypatch, environment="dev"):
    monkeypatch.setattr(module, "PRIVATE", tmp_path)
    folder = tmp_path / environment / "atol_pay"
    folder.mkdir(parents=True)
    (folder / "egress_enabled").write_text("sandbox" if environment == "dev" else "production")
    (folder / "api_ipv4").write_text("1.1.1.1\n")
    return folder


@pytest.mark.parametrize("env,prefix", [("dev", "10.240.20"), ("prod", "10.240.10")])
def test_only_enabled_api_gets_https_not_frontend_bot_or_other_environment(
    tmp_path, monkeypatch, env, prefix
):
    guard = load("gl-container-egress")
    prepare(guard, tmp_path, monkeypatch, env)
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
    rules = next(options["data"] for args, options in operations if args[0] == "iptables-restore")
    assert [line for line in rules.splitlines() if "-d 1.1.1.1" in line] == [
        f"-A GL-CONTAINERS-FWD -s {prefix}.2 -d 1.1.1.1 -p tcp --dport 443 -j ACCEPT"
    ]
    assert "-A GL-CONTAINERS-FWD -s 10.241.0.0/16 -j REJECT" in rules
    assert "*nat" not in rules and "--flush" not in rules
    assert not any(args[:3] == ("iptables", "-w", "-P") for args, _ in operations)


@pytest.mark.parametrize(
    "value", ["", "127.0.0.1", "192.168.1.1", "224.0.0.1", "::1", "1.1.1.0/24"]
)
def test_invalid_destinations_fail_before_any_firewall_operation(tmp_path, monkeypatch, value):
    guard = load("gl-container-egress")
    folder = prepare(guard, tmp_path, monkeypatch)
    (folder / "api_ipv4").write_text(value)
    monkeypatch.setattr(guard, "run", lambda *_a, **_k: pytest.fail("firewall changed"))
    monkeypatch.setattr("sys.argv", ["guard", "apply"])
    with pytest.raises(ValueError):
        guard.main()


def test_stale_cache_or_merchant_key_alone_does_not_enable_egress(tmp_path, monkeypatch):
    guard = load("gl-container-egress")
    folder = prepare(guard, tmp_path, monkeypatch)
    (folder / "egress_enabled").unlink()
    (folder / "api_token").write_text("synthetic-private-credential")
    assert guard.atol_addresses("dev") == []


def test_dev_cannot_enable_production_provider(tmp_path, monkeypatch):
    guard = load("gl-container-egress")
    folder = prepare(guard, tmp_path, monkeypatch)
    (folder / "egress_enabled").write_text("production")
    with pytest.raises(ValueError):
        guard.atol_addresses("dev")


@pytest.fixture
def refresher(tmp_path, monkeypatch):
    refresh = load("atol_egress.py")
    folder = prepare(refresh, tmp_path, monkeypatch)
    monkeypatch.setattr(refresh, "LOCK", tmp_path / "lock")
    monkeypatch.setattr(refresh.os, "geteuid", lambda: 0)
    return refresh, folder


def test_dns_failure_preserves_cache_and_firewall(refresher, monkeypatch):
    refresh, folder = refresher

    def fail(_hostname):
        raise OSError("synthetic DNS unavailable")

    monkeypatch.setattr(refresh, "addresses", fail)
    monkeypatch.setattr(refresh.subprocess, "run", lambda *_a, **_k: pytest.fail("rules changed"))
    with pytest.raises(OSError):
        refresh.main()
    assert (folder / "api_ipv4").read_text() == "1.1.1.1\n"


def test_reload_failure_restores_cache(refresher, monkeypatch):
    refresh, folder = refresher
    monkeypatch.setattr(refresh, "addresses", lambda _host: "8.8.8.8\n")

    def fail(*_args, **_kwargs):
        raise OSError("synthetic reload failed")

    monkeypatch.setattr(refresh.subprocess, "run", fail)
    with pytest.raises(OSError):
        refresh.main()
    assert (folder / "api_ipv4").read_text() == "1.1.1.1\n"


def test_busy_maintenance_lock_skips_dns_and_rules(refresher, monkeypatch, capsys):
    refresh, _ = refresher
    monkeypatch.setattr(refresh, "addresses", lambda _host: pytest.fail("DNS while locked"))
    with refresh.LOCK.open("w") as neighbor:
        fcntl.flock(neighbor, fcntl.LOCK_EX)
        refresh.main()
    assert capsys.readouterr().out == "atol_egress_busy\n"


def test_only_fixed_sandbox_hostname_and_bounded_apply_without_credential_reads(
    refresher, monkeypatch
):
    refresh, folder = refresher
    hosts, operations = [], []

    def resolve(host):
        hosts.append(host)
        return "8.8.8.8\n"

    monkeypatch.setattr(refresh, "addresses", resolve)
    monkeypatch.setattr(refresh.subprocess, "run", lambda *a, **k: operations.append((a, k)))
    refresh.main()
    assert hosts == ["croc-sandbox-api-mobile.atolpay.ru"]
    assert (folder / "api_ipv4").read_text() == "8.8.8.8\n"
    assert operations == [
        (
            (["/usr/local/sbin/gl-container-egress", "apply"],),
            {"check": True, "capture_output": True, "timeout": 10},
        )
    ]


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.1", "224.0.0.1"])
def test_dns_cannot_authorize_private_or_multicast_destinations(monkeypatch, address):
    refresh = load("atol_egress.py")
    monkeypatch.setattr(
        refresh.socket, "getaddrinfo", lambda *_a, **_k: [(None, None, None, None, (address, 443))]
    )
    with pytest.raises(ValueError):
        refresh.addresses("new-api-mobile.atolpay.ru")


def test_one_environment_dns_failure_cannot_partially_update_the_other(refresher, monkeypatch):
    refresh, dev = refresher
    prod = refresh.PRIVATE / "prod/atol_pay"
    prod.mkdir(parents=True)
    (prod / "egress_enabled").write_text("production")
    (prod / "api_ipv4").write_text("9.9.9.9\n")

    def resolve(host):
        if host == "new-api-mobile.atolpay.ru":
            raise OSError("synthetic DNS unavailable")
        return "8.8.8.8\n"

    monkeypatch.setattr(refresh, "addresses", resolve)
    monkeypatch.setattr(refresh.subprocess, "run", lambda *_a, **_k: pytest.fail("rules changed"))
    with pytest.raises(OSError):
        refresh.main()
    assert (dev / "api_ipv4").read_text() == "1.1.1.1\n"
    assert (prod / "api_ipv4").read_text() == "9.9.9.9\n"


def test_non_admin_cannot_refresh_or_apply(refresher, monkeypatch):
    refresh, _ = refresher
    monkeypatch.setattr(refresh.os, "geteuid", lambda: 10001)
    with pytest.raises(RuntimeError, match="administrator required"):
        refresh.main()
