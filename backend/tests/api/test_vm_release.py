"""Critical release boundaries: changed main and failed acceptance cannot publish."""

import importlib.machinery
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def release(tmp_path, monkeypatch):
    loader = importlib.machinery.SourceFileLoader(
        "vm_release", str(ROOT / "infra/timeweb/gl-release")
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)
    monkeypatch.setattr(module, "ROOT", tmp_path / "runtime")
    monkeypatch.setattr(module, "PRIVATE", tmp_path / "private")
    monkeypatch.setattr(module, "LOCK", tmp_path / "lock")
    module.PRIVATE.mkdir()
    (module.ROOT / "containers/site").mkdir(parents=True)
    (module.PRIVATE / "site.env").write_text("GL_SITE_PROD_IMAGE=sha256:old\n")
    monkeypatch.setattr("sys.argv", ["gl-release", "site", "prod", "--apply"])
    return module


def fake_tools(
    module,
    monkeypatch,
    *,
    moved=False,
    fail_probe=False,
    fail_polling=False,
    fail_recovery_enable=False,
    component="site",
):
    operations = []
    main_sha = "a" * 40

    def run(*args, **_kwargs):
        operations.append(args)
        if args[0] == "git":
            if "get-url" in args:
                return module.REPOS[component]
            if "rev-parse" in args:
                return main_sha
            if "ls-remote" in args:
                return ("b" * 40 if moved else main_sha) + "\trefs/heads/main"
        if args[0] == "git" and "archive" in args:
            source = next((module.ROOT / "releases").rglob("source-*"))
            (source / "infra/timeweb").mkdir(parents=True)
            (source / "infra/timeweb/verify-managed.py").write_text("# synthetic probe\n")
        if args[:3] == ("docker", "image", "inspect"):
            return "sha256:" + "1" * 64
        if fail_probe and (args[0] == "python3" or args[:2] == ("docker", "exec")):
            raise RuntimeError("synthetic acceptance failed")
        if fail_polling and args[0] == "python3" and args[1].endswith("verify-polling.py"):
            raise RuntimeError("polling acceptance failed")
        if fail_recovery_enable and args == (
            "systemctl",
            "enable",
            "--now",
            "gl-container-watchdog.timer",
        ):
            raise RuntimeError("recovery activation failed")
        return ""

    monkeypatch.setattr(module, "run", run)
    return operations


def test_changed_main_refuses_switch_after_checks(release, monkeypatch):
    operations = fake_tools(release, monkeypatch, moved=True)
    with pytest.raises(RuntimeError, match="main changed"):
        release.main()
    assert (release.PRIVATE / "site.env").read_text() == "GL_SITE_PROD_IMAGE=sha256:old\n"
    assert not any(args[:2] == ("docker", "compose") for args in operations)
    assert not list(release.ROOT.rglob("applied.json"))


def test_checked_image_keeps_content_tag_for_boot_and_rollback(release, monkeypatch):
    operations = fake_tools(release, monkeypatch)
    expected = "sha256:" + "1" * 64
    assert release.image("gl-api:synthetic-source-sha") == expected
    assert operations[-1] == ("docker", "image", "tag", expected, "gl-retained:" + "1" * 64)


def test_retention_failure_refuses_image_publication(release, monkeypatch):
    def run(*args, **kwargs):
        if args[:3] == ("docker", "image", "inspect"):
            return "sha256:" + "1" * 64
        raise RuntimeError("synthetic content tag failure")

    monkeypatch.setattr(release, "run", run)
    with pytest.raises(RuntimeError, match="content tag failure"):
        release.image("gl-api:synthetic-source-sha")


def test_failed_acceptance_restores_previous_image_and_private_permissions(release, monkeypatch):
    operations = fake_tools(release, monkeypatch, fail_probe=True)
    with pytest.raises(RuntimeError, match="synthetic acceptance"):
        release.main()
    config = release.PRIVATE / "site.env"
    assert config.read_text() == "GL_SITE_PROD_IMAGE=sha256:old\n"
    assert config.stat().st_mode & 0o777 == 0o600
    assert len([args for args in operations if args[:2] == ("docker", "compose")]) == 2
    assert not list(release.ROOT.rglob("applied.json"))
    assert len(list(release.ROOT.rglob("verified.json"))) == 1


def test_native_consumer_is_disabled_before_container_start_and_restored_on_failure(
    release, monkeypatch
):
    environment = release.PRIVATE / "dev"
    environment.mkdir()
    (environment / "compose.env").write_text("GL_API_IMAGE=old-api\nGL_BOT_IMAGE=old-bot\n")
    (release.ROOT / "containers/backend").mkdir()
    monkeypatch.setattr("sys.argv", ["gl-release", "backend", "dev", "--apply"])
    monkeypatch.setattr(release.subprocess, "run", lambda *_a, **_k: SimpleNamespace(returncode=0))
    operations = fake_tools(release, monkeypatch, fail_probe=True, component="backend")
    with pytest.raises(RuntimeError, match="synthetic acceptance"):
        release.main()
    stop = operations.index(("systemctl", "disable", "--now", "gl-bot@dev"))
    starts = [
        i
        for i, args in enumerate(operations)
        if args[:2] == ("docker", "compose") and "up" in args
    ]
    assert stop < starts[0]
    assert ("systemctl", "enable", "--now", "gl-bot@dev") in operations
    assert not (environment / "enabled").exists()
    assert (
        environment / "compose.env"
    ).read_text() == "GL_API_IMAGE=old-api\nGL_BOT_IMAGE=old-bot\n"


def test_failed_polling_acceptance_never_enables_recovery_and_restores_native(
    release, monkeypatch
):
    environment = release.PRIVATE / "dev"
    environment.mkdir()
    (environment / "compose.env").write_text("GL_API_IMAGE=old-api\nGL_BOT_IMAGE=old-bot\n")
    (release.ROOT / "containers/backend").mkdir()
    monkeypatch.setattr("sys.argv", ["gl-release", "backend", "dev", "--apply"])
    monkeypatch.setattr(release.subprocess, "run", lambda *_a, **_k: SimpleNamespace(returncode=0))
    operations = fake_tools(release, monkeypatch, fail_polling=True, component="backend")
    with pytest.raises(RuntimeError, match="polling acceptance failed"):
        release.main()
    assert any(args[:2] == ("docker", "exec") for args in operations)
    assert ("systemctl", "enable", "--now", "gl-bot@dev") in operations
    assert not (environment / "enabled").exists()
    assert not list(release.ROOT.rglob("applied.json"))
    assert not any("gl-container-watchdog.timer" in args for args in operations)
    assert ("systemctl", "enable", "gl-containers@dev") not in operations
    assert (
        environment / "compose.env"
    ).read_text() == "GL_API_IMAGE=old-api\nGL_BOT_IMAGE=old-bot\n"


def test_inactive_but_enabled_native_is_disabled_and_not_started_by_failed_cutover(
    release, monkeypatch
):
    environment = release.PRIVATE / "dev"
    environment.mkdir()
    (environment / "compose.env").write_text("GL_API_IMAGE=old-api\nGL_BOT_IMAGE=old-bot\n")
    (release.ROOT / "containers/backend").mkdir()
    monkeypatch.setattr("sys.argv", ["gl-release", "backend", "dev", "--apply"])

    def service_state(args, **_kwargs):
        return SimpleNamespace(
            returncode=0 if "is-enabled" in args and "gl-bot@dev" in args else 3
        )

    monkeypatch.setattr(release.subprocess, "run", service_state)
    operations = fake_tools(release, monkeypatch, fail_polling=True, component="backend")
    with pytest.raises(RuntimeError, match="polling acceptance failed"):
        release.main()
    disable = operations.index(("systemctl", "disable", "--now", "gl-bot@dev"))
    startup = next(
        i
        for i, args in enumerate(operations)
        if args[:2] == ("docker", "compose") and "up" in args
    )
    assert disable < startup
    assert ("systemctl", "enable", "gl-bot@dev") in operations
    assert ("systemctl", "enable", "--now", "gl-bot@dev") not in operations
    assert ("systemctl", "start", "gl-bot@dev") not in operations
    assert not (environment / "enabled").exists()


def test_failure_after_boot_activation_disables_new_container_boot_and_restores_native(
    release, monkeypatch
):
    environment = release.PRIVATE / "dev"
    environment.mkdir()
    (environment / "compose.env").write_text("GL_API_IMAGE=old-api\nGL_BOT_IMAGE=old-bot\n")
    (release.ROOT / "containers/backend").mkdir()
    monkeypatch.setattr("sys.argv", ["gl-release", "backend", "dev", "--apply"])

    def service_state(args, **_kwargs):
        return SimpleNamespace(returncode=0 if "gl-bot@dev" in args else 3)

    monkeypatch.setattr(release.subprocess, "run", service_state)
    operations = fake_tools(release, monkeypatch, fail_recovery_enable=True, component="backend")
    with pytest.raises(RuntimeError, match="recovery activation failed"):
        release.main()
    assert ("systemctl", "enable", "gl-containers@dev") in operations
    assert ("systemctl", "disable", "gl-containers@dev") in operations
    assert ("systemctl", "enable", "--now", "gl-bot@dev") in operations
    assert not (environment / "enabled").exists()
    assert not list(release.ROOT.rglob("applied.json"))


def test_api_only_preserves_bot_image_consumer_and_verifies_existing_polling(release, monkeypatch):
    environment = release.PRIVATE / "dev"
    environment.mkdir()
    (environment / "compose.env").write_text("GL_API_IMAGE=old-api\nGL_BOT_IMAGE=old-bot\n")
    (environment / "enabled").touch()
    (release.ROOT / "containers/backend").mkdir()
    monkeypatch.setattr("sys.argv", ["gl-release", "backend", "dev", "--apply", "--api-only"])
    monkeypatch.setattr(release.subprocess, "run", lambda *_a, **_k: SimpleNamespace(returncode=1))
    operations = fake_tools(release, monkeypatch, component="backend")
    release.main()
    config = (environment / "compose.env").read_text()
    assert "GL_BOT_IMAGE=old-bot\n" in config
    assert "GL_API_IMAGE=sha256:" in config
    starts = [args for args in operations if args[:2] == ("docker", "compose") and "up" in args]
    assert len(starts) == 1 and starts[0][-1] == "api"
    assert "bot" not in starts[0] and "telegram-egress" not in starts[0]
    probes = [args for args in operations if args[0] == "python3"]
    assert probes[0][-1] == "old-bot"


def test_api_only_refuses_native_consumer_before_schema_or_config_change(release, monkeypatch):
    monkeypatch.setattr("sys.argv", ["gl-release", "backend", "dev", "--apply", "--api-only"])
    monkeypatch.setattr(release.subprocess, "run", lambda *_a, **_k: SimpleNamespace(returncode=0))
    operations = fake_tools(release, monkeypatch, component="backend")
    with pytest.raises(RuntimeError, match="retired native"):
        release.main()
    assert not any(args[:2] == ("docker", "compose") for args in operations)


def test_api_only_failed_acceptance_restores_api_without_recreating_bot(release, monkeypatch):
    environment = release.PRIVATE / "dev"
    environment.mkdir()
    original = "GL_API_IMAGE=old-api\nGL_BOT_IMAGE=old-bot\n"
    (environment / "compose.env").write_text(original)
    (environment / "enabled").touch()
    (release.ROOT / "containers/backend").mkdir()
    monkeypatch.setattr("sys.argv", ["gl-release", "backend", "dev", "--apply", "--api-only"])
    monkeypatch.setattr(release.subprocess, "run", lambda *_a, **_k: SimpleNamespace(returncode=1))
    operations = fake_tools(release, monkeypatch, component="backend", fail_probe=True)
    with pytest.raises(RuntimeError, match="synthetic acceptance"):
        release.main()
    assert (environment / "compose.env").read_text() == original
    starts = [args for args in operations if args[:2] == ("docker", "compose") and "up" in args]
    assert len(starts) == 2 and all(args[-1] == "api" for args in starts)
    assert not any("bot" in args for args in starts)


@pytest.mark.parametrize("fail_probe", [False, True])
def test_source_archive_and_quality_tag_are_released_after_check(release, monkeypatch, fail_probe):
    operations = fake_tools(release, monkeypatch, fail_probe=fail_probe)
    original = release.run
    archive_paths = []

    def run(*args, **kwargs):
        result = original(*args, **kwargs)
        if args[0] == "git" and "archive" in args:
            archive = Path(
                next(arg.split("=", 1)[1] for arg in args if arg.startswith("--output="))
            )
            archive.write_bytes(b"synthetic archive")
            archive_paths.append(archive)
        return result

    monkeypatch.setattr(release, "run", run)
    if fail_probe:
        with pytest.raises(RuntimeError, match="synthetic acceptance"):
            release.main()
    else:
        release.main()
    assert archive_paths and all(path.parent.name.startswith("source-") for path in archive_paths)
    assert not list(release.ROOT.rglob("source.tar"))
    assert not list(release.ROOT.rglob("source-*"))
    assert any(
        args[:3] == ("docker", "image", "rm") and args[3].startswith("gl-site-quality:")
        for args in operations
    )
