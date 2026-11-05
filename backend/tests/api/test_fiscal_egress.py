"""No live firewall mutations: verify the disabled-by-default receipt candidate."""

from types import SimpleNamespace

import pytest
from test_atol_egress import load


@pytest.mark.parametrize("enabled", [False, True])
def test_receipt_network_does_not_open_frontend_database_or_internal_api(
    tmp_path, monkeypatch, enabled
):
    guard = load("gl-container-egress")
    monkeypatch.setattr(guard, "PRIVATE", tmp_path)
    (tmp_path / "network.env").write_text(
        "GL_VPN_IP=8.8.8.8\nGL_DATABASE_IP=9.9.9.9\nGL_VPN_PORT=443\n"
    )
    if enabled:
        folder = tmp_path / "prod/fiscal"
        folder.mkdir(parents=True)
        (folder / "receiver_enabled").write_text("production")
    operations = []

    def run(*args, **kwargs):
        operations.append((args, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(guard, "run", run)
    monkeypatch.setattr("sys.argv", ["guard", "apply"])
    guard.main()
    rules = next(kwargs["data"] for args, kwargs in operations if args[0] == "iptables-restore")
    lines = rules.splitlines()
    receipt_rules = [line for line in lines if "10.240.10.5" in line]
    assert len(receipt_rules) == (3 if enabled else 0)
    if enabled:
        assert (
            "-A GL-CONTAINERS-FWD -s 10.240.10.5 -d 9.9.9.9 -p tcp --dport 5432 -j ACCEPT" in lines
        )
        assert all("--dport 8001" in line for line in receipt_rules if "10.241" in line)
    assert not any("10.240.20.5" in line for line in lines)
    assert not any("-s 10.241" in line and "--dport 5432" in line for line in lines)
    assert not any("-s 10.241" in line and "--dport 8000" in line for line in lines)
    assert "*nat" not in rules
    raw_batches = [
        kwargs["data"]
        for args, kwargs in operations
        if args[0] == "iptables-restore" and kwargs["data"].startswith("*raw")
    ]
    assert len(raw_batches) == 1
    raw_rules = [x for x in raw_batches[0].splitlines() if "-j ACCEPT" in x]
    assert len(raw_rules) == (2 if enabled else 0)
    assert all("-d 10.240.10.5" in x and "--dport 8001" in x for x in raw_rules)
    assert all("-s 10.241.10.2" in x or "-s 10.241.11.2" in x for x in raw_rules)
    assert "-F PREROUTING" not in raw_batches[0]


def test_wrong_environment_marker_aborts_before_any_firewall_command(tmp_path, monkeypatch):
    guard = load("gl-container-egress")
    monkeypatch.setattr(guard, "PRIVATE", tmp_path)
    folder = tmp_path / "prod/fiscal"
    folder.mkdir(parents=True)
    (folder / "receiver_enabled").write_text("sandbox")
    operations = []
    monkeypatch.setattr(guard, "run", lambda *a, **k: operations.append(a))
    monkeypatch.setattr("sys.argv", ["guard", "apply"])
    with pytest.raises(ValueError, match="invalid fiscal receiver environment"):
        guard.main()
    assert operations == []


def test_bridge_guard_limits_same_bridge_receipt_traffic_without_br_netfilter(
    tmp_path, monkeypatch
):
    guard = load("gl-container-egress")
    monkeypatch.setattr(guard, "PRIVATE", tmp_path)
    (tmp_path / "network.env").write_text(
        "GL_VPN_IP=8.8.8.8\nGL_DATABASE_IP=9.9.9.9\nGL_VPN_PORT=443\n"
    )
    folder = tmp_path / "prod/fiscal"
    folder.mkdir(parents=True)
    (folder / "receiver_enabled").write_text("production")
    operations = []
    monkeypatch.setattr(
        guard,
        "run",
        lambda *args, **kwargs: operations.append((args, kwargs)) or SimpleNamespace(returncode=0),
    )
    monkeypatch.setattr("sys.argv", ["guard", "apply"])
    guard.main()
    bridge = next(k["data"] for a, k in operations if a == ("nft", "-f", "-"))
    assert "table bridge gl_receipt_guard" in bridge
    assert (
        "ip daddr 10.240.10.5 tcp dport 8001 ip saddr != { 10.241.10.2, 10.241.11.2 } counter drop"
        in bridge
    )
    assert "ip saddr 10.240.10.5 ip daddr 10.240.10.0/24 counter drop" in bridge
    assert "10.240.20.5" not in bridge
    assert "flush ruleset" not in bridge
