"""Storage safety: only proven obsolete release refs and owned test artifacts."""

import importlib.machinery
import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def identity(number):
    return f"sha256:{number:064x}"


@pytest.fixture
def history(tmp_path, monkeypatch):
    loader = importlib.machinery.SourceFileLoader(
        "storage_release", str(ROOT / "infra/timeweb/gl-release")
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    monkeypatch.setattr(module, "ROOT", tmp_path / "runtime")
    monkeypatch.setattr(module, "PRIVATE", tmp_path / "private")
    monkeypatch.setattr(module, "LOCK", tmp_path / "lock")
    for env in ("dev", "prod"):
        (module.PRIVATE / env).mkdir(parents=True)
        (module.PRIVATE / env / "compose.env").write_text(f"GL_API_IMAGE={identity(9)}\n")
    (module.PRIVATE / "site.env").write_text(f"GL_SITE_PROD_IMAGE={identity(9)}\n")
    calls = []
    containers = []
    tags = {identity(9): {f"gl-api:{9:040x}"}}
    module.testing_tags = tags

    def run(*args, **kwargs):
        calls.append(args)
        if args[:3] == ("docker", "image", "inspect"):
            return args[3]
        if args[:3] == ("docker", "ps", "-aq"):
            return " ".join(str(i) for i in range(len(containers)))
        if args[:2] == ("docker", "inspect"):
            return json.dumps([{"Image": image} for image in containers])
        if args[:3] == ("docker", "image", "ls"):
            return "\n".join(
                json.dumps(
                    {"ID": image, "Repository": ref.split(":")[0], "Tag": ref.split(":")[1]}
                )
                for image, refs in tags.items()
                for ref in refs
            )
        return ""

    monkeypatch.setattr(module, "run", run)
    return module, calls, containers, tags


def record(module, number, *, applied=True, previous=9, component="backend", environment="prod"):
    module.testing_tags.setdefault(identity(number), {f"gl-api:{number:040x}"})
    directory = module.ROOT / "releases" / component / f"{number:040x}" / environment
    directory.mkdir(parents=True)
    filename = directory / ("applied.json" if applied else "verified.json")
    filename.write_text(json.dumps({"images": {"GL_API_IMAGE": identity(number)}}))
    os.utime(filename, (number, number))
    if applied:
        (directory / "previous.env").write_text(f"GL_API_IMAGE={identity(previous)}\n")
    return directory


def test_keeps_three_revisions_rollback_configs_current_and_stopped_containers(history):
    module, calls, containers, tags = history
    for number in range(1, 7):
        record(module, number, previous=number - 1 if number > 1 else 9)
        tags[identity(number)] = {f"gl-api:{number:040x}", f"gl-retained:{number:064x}"}
    containers.append(identity(2))  # Docker inspect includes stopped containers too.
    plan = module.history_plan()
    assert [entry["image"] for entry in plan] == [identity(1), identity(3)]
    assert not any(args[:3] == ("docker", "image", "rm") for args in calls)
    module.prune_history(apply=True)
    removals = [args for args in calls if args[:3] == ("docker", "image", "rm")]
    assert removals == [
        ("docker", "image", "rm", *sorted(tags[identity(number)])) for number in (1, 3)
    ]
    assert all("-f" not in args for args in calls)


def test_unknown_tags_tools_and_unrecorded_jobs_never_become_candidates(history):
    module, _calls, _containers, tags = history
    record(module, 1, applied=False)
    tags[identity(1)] = {f"gl-api:{1:040x}", "certbot/certbot:latest"}
    tags[identity(2)] = {f"gl-api:{2:040x}"}  # No completed release evidence.
    tags[identity(3)] = {"postgres:18-alpine"}
    assert module.history_plan() == []


def test_old_verified_only_image_is_eligible_but_config_reference_protects_it(history):
    module, _calls, _containers, tags = history
    record(module, 1, applied=False)
    tags[identity(1)] = {f"gl-bot:{1:040x}"}
    assert len(module.history_plan()) == 1
    (module.PRIVATE / "dev/compose.env").write_text(f"GL_BOT_IMAGE={identity(1)}\n")
    assert module.history_plan() == []


@pytest.mark.parametrize("damage", ["config", "metadata", "previous", "inspect"])
def test_missing_or_invalid_evidence_aborts_before_any_deletion(history, monkeypatch, damage):
    module, calls, _containers, tags = history
    directory = record(module, 1)
    record(module, 2, applied=False)
    tags[identity(2)] = {f"gl-api:{2:040x}"}
    if damage == "config":
        (module.PRIVATE / "dev/compose.env").unlink()
    elif damage == "metadata":
        (directory / "applied.json").write_text('{"images":{"GL_API_IMAGE":"invalid"}}')
    elif damage == "previous":
        (directory / "previous.env").unlink()
    else:
        original = module.run
        monkeypatch.setattr(
            module,
            "run",
            lambda *a, **k: "" if a[:3] == ("docker", "image", "inspect") else original(*a, **k),
        )
    with pytest.raises((OSError, ValueError, RuntimeError)):
        module.prune_history(apply=True)
    assert not any(args[:3] == ("docker", "image", "rm") for args in calls)


def test_dry_run_only_and_busy_lock_skips_even_inspection(history, monkeypatch, capsys):
    module, calls, _containers, tags = history
    record(module, 1, applied=False)
    tags[identity(1)] = {f"gl-api:{1:040x}"}
    module.prune_history(apply=False)
    assert '"apply": false' in capsys.readouterr().out
    assert not any(args[:3] == ("docker", "image", "rm") for args in calls)
    calls.clear()
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)
    monkeypatch.setattr("sys.argv", ["gl-release", "--prune-history", "--apply"])
    with module.LOCK.open("w") as held:
        module.fcntl.flock(held, module.fcntl.LOCK_EX | module.fcntl.LOCK_NB)
        module.main()
    assert "gl_history_busy" in capsys.readouterr().out
    assert calls == []


def test_quality_failure_removes_only_owned_tag_and_keeps_original_error(history):
    module, calls, _containers, _tags = history
    with (
        pytest.raises(RuntimeError, match="quality failed"),
        module.disposable_image("gl-site-quality:synthetic-123"),
    ):
        raise RuntimeError("quality failed")
    assert calls == [("docker", "image", "rm", "gl-site-quality:synthetic-123")]


@pytest.mark.parametrize("failure", ["none", "build", "run", "term"])
def test_verification_cleans_own_test_tags_on_success_failure_and_term(tmp_path, failure):
    """Execute real shell/traps with a deterministic Docker stub, no host secrets."""
    executable = tmp_path / "docker"
    log = tmp_path / "docker.jsonl"
    executable.write_text("""#!/usr/bin/env python3
import json,os,signal,sys
args=sys.argv[1:]
with open(os.environ['STORAGE_TEST_LOG'],'a') as out: out.write(json.dumps(args)+'\\n')
case=os.environ['STORAGE_TEST_FAILURE']
if args[0]==case or (case=='term' and args[0]=='run'):
    if case=='term': os.kill(os.getppid(), signal.SIGTERM)
    sys.exit(23)
""")
    executable.chmod(0o755)
    result = subprocess.run(
        ["bash", str(ROOT / "infra/timeweb/verify-source.sh"), str(ROOT), "a" * 40],
        env={
            **os.environ,
            "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
            "STORAGE_TEST_LOG": str(log),
            "STORAGE_TEST_FAILURE": failure,
            "GL_MAINTENANCE_LOCK_HELD": "1",
        },
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == {"none": 0, "build": 23, "run": 23, "term": 143}[failure]
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    removal = next(call for call in calls if call[:2] == ["image", "rm"])
    build = next(call for call in calls if call[0] == "build")
    own_tag = build[build.index("-t") + 1]
    assert own_tag in removal and own_tag.startswith("gl-bot-test:" + "a" * 40 + "-")
    assert all(ref.startswith(("gl-bot-test:", "gl-api-test:")) for ref in removal[2:])
    assert "-f" not in removal and "--force" not in removal
    if failure == "none":
        assert any(call[0] == "build" and "runtime" in call for call in calls)


def test_retention_is_independent_for_each_environment_and_component(history):
    module, _calls, _containers, tags = history
    for number in range(1, 5):
        record(module, number)
        tags[identity(number)] = {f"gl-api:{number:040x}"}
    record(module, 1, environment="dev")
    record(module, 2, component="site")
    assert module.history_plan() == []


def test_unavailable_saved_rollback_prevents_all_removal(history, monkeypatch):
    module, calls, _containers, tags = history
    record(module, 1, previous=8)
    record(module, 2, applied=False)
    tags[identity(2)] = {f"gl-api:{2:040x}"}
    original = module.run

    def run(*args, **kwargs):
        if args[:4] == ("docker", "image", "inspect", identity(8)):
            raise RuntimeError("synthetic missing rollback")
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "run", run)
    with pytest.raises(RuntimeError, match="missing rollback"):
        module.prune_history(apply=True)
    assert not any(args[:3] == ("docker", "image", "rm") for args in calls)


def test_private_values_never_enter_history_plan(history, capsys):
    module, _calls, _containers, _tags = history
    config = module.PRIVATE / "site.env"
    config.write_text(config.read_text() + "PRIVATE_CANARY=synthetic-do-not-log\n")
    module.prune_history(apply=False)
    assert "synthetic-do-not-log" not in capsys.readouterr().out


@pytest.mark.parametrize("operation", ["publication", "history"])
def test_temporary_label_never_becomes_runtime_or_enters_history_gc(
    history, monkeypatch, operation
):
    module, calls, _containers, _tags = history
    original = module.run

    def run(*args, **kwargs):
        if args[:3] == ("docker", "image", "inspect") and "temporary" in args[-1]:
            return "true"
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "run", run)
    with pytest.raises(RuntimeError, match="temporary"):
        if operation == "publication":
            module.image(identity(9))
        else:
            module.prune_history(apply=True)
    assert not any(
        args[:3] in (("docker", "image", "rm"), ("docker", "image", "tag")) for args in calls
    )


def test_retired_native_predecessor_is_archival_while_recent_images_remain_protected(history):
    module, calls, _containers, tags = history
    initial = record(module, 1)
    (initial / "previous.env").write_text("GL_API_IMAGE=gl-api:retired-native-staging\n")
    record(module, 2, previous=9)
    tags[identity(1)] = {f"gl-api:{1:040x}"}
    tags[identity(2)] = {f"gl-api:{2:040x}"}
    assert module.history_plan() == []
    assert not any("gl-api:retired-native-staging" in args for args in calls)


def test_later_verification_cannot_replace_the_actual_applied_image_set(history):
    module, _calls, _containers, tags = history
    directory = record(module, 1)
    (directory / "verified.json").write_text(json.dumps({"images": {"GL_API_IMAGE": identity(2)}}))
    tags[identity(1)] = {f"gl-api:{1:040x}"}
    tags[identity(2)] = {f"gl-api:{2:040x}"}
    assert [entry["image"] for entry in module.history_plan()] == [identity(2)]


def test_known_missing_old_release_is_reported_and_not_counted_as_usable_rollback(history, capsys):
    module, _calls, _containers, tags = history
    for number in range(1, 5):
        record(module, number)
    del tags[identity(2)]
    assert module.history_plan() == []  # Keep available sets 4, 3 and 1.
    assert "unavailable older release backend/prod/" in capsys.readouterr().out
    assert len(list(module.ROOT.rglob("applied.json"))) == 4


def test_missing_latest_applied_image_still_aborts_before_deletion(history):
    module, calls, _containers, tags = history
    record(module, 1)
    del tags[identity(1)]
    with pytest.raises(RuntimeError, match="latest applied image unavailable"):
        module.prune_history(apply=True)
    assert not any(args[:3] == ("docker", "image", "rm") for args in calls)
