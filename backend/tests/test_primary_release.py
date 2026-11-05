"""Exercise release safety with synthetic Git/GitHub/Docker executables."""

import json
import os
import subprocess
from pathlib import Path

import pytest

REVISION = "f" * 40

MOCK = r"""
#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
name = Path(sys.argv[0]).name
args = sys.argv[1:]
with Path(os.environ['MOCK_TRACE']).open('a') as file:
    file.write(json.dumps([name, *args])+'\n')
state = Path(os.environ['MOCK_IMAGE'])
if name == 'git':
    if 'get-url' in args: print('https://github.com/timsmykov/gl-professional-telegram-bot.git')
    elif 'rev-parse' in args: print('f'*40)
    elif 'worktree' in args: Path(args[-2]).mkdir(parents=True)
    elif 'status' in args: print(os.environ.get('MOCK_DIRTY', ''), end='')
elif name == 'gh':
    print(json.dumps([{'status':'completed','conclusion':os.environ.get('MOCK_CI','success'),'headSha':'f'*40}][0]))
elif name == 'docker':
    if args[0] == 'inspect':
        fmt = args[args.index('--format')+1]
        if 'Id' in fmt: print('stable-database')
        elif 'Config.Image' in fmt: print(state.read_text())
        elif 'Health.Status' in fmt:
            print('unhealthy' if os.environ.get('MOCK_FAIL_NEW') and state.read_text().startswith('gl-professional-bot:main-') else 'healthy')
    elif args[0] == 'compose':
        overrides = [args[i+1] for i, arg in enumerate(args) if arg == '-f']
        state.write_text(json.loads(Path(overrides[-1]).read_text())['services']['preview_bot']['image'])
""".lstrip()


@pytest.fixture
def release(tmp_path):
    root = Path(__file__).resolve().parents[1]
    script = root / "scripts/release-primary-bot.sh"
    if not script.exists():
        pytest.skip("Host release safety test requires source script")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("git", "gh", "docker", "flock", "sleep"):
        file = bin_dir / name
        file.write_text(MOCK)
        file.chmod(0o755)
    override = tmp_path / "primary.yaml"
    override.write_text("services: {}\n")
    image = tmp_path / "image"
    image.write_text("previous-image")
    trace = tmp_path / "trace"
    env = {
        **os.environ,
        "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
        "GL_BOT_RELEASE_REPO": str(tmp_path / "repo"),
        "GL_BOT_RELEASE_ROOT": str(tmp_path / "releases"),
        "GL_BOT_PRIMARY_OVERRIDE": str(override),
        "MOCK_TRACE": str(trace),
        "MOCK_IMAGE": str(image),
    }

    def run(*args, **settings):
        result = subprocess.run(
            ["bash", str(script), *args],
            env={**env, **settings},
            capture_output=True,
            check=False,
            text=True,
            timeout=30,
        )
        calls = [json.loads(line) for line in trace.read_text().splitlines()]
        return result, calls, image.read_text()

    return run


def test_release_dry_run_uses_main_ci_and_does_not_deploy(release):
    result, calls, image = release()
    assert result.returncode == 0
    assert "dry run" in result.stdout
    github = next(call for call in calls if call[0] == "gh")
    assert github[github.index("--branch") + 1] == "main"
    assert github[github.index("--commit") + 1] == REVISION
    assert not any("build" in call or "compose" in call or "worktree" in call for call in calls)
    assert image == "previous-image"


def test_failed_main_ci_blocks_even_explicit_release(release):
    result, calls, image = release("--apply", MOCK_CI="failure")
    assert result.returncode != 0
    assert not any("build" in call or "compose" in call for call in calls)
    assert image == "previous-image"


def test_release_applies_only_bot_without_recreating_database(release):
    result, calls, image = release("--apply")
    assert result.returncode == 0
    assert image == "gl-professional-bot:main-" + REVISION
    build = next(call for call in calls if "build" in call)
    assert "org.opencontainers.image.revision=" + REVISION in build
    compose = next(call for call in calls if "compose" in call)
    assert "--no-deps" in compose and "--no-build" in compose
    assert compose[-1] == "preview_bot"
    assert not any("down" in call or "stop" in call for call in calls)


def test_unhealthy_new_release_restores_previous_image(release):
    result, calls, image = release("--apply", MOCK_FAIL_NEW="1")
    assert result.returncode != 0
    assert image == "previous-image"
    assert len([call for call in calls if "compose" in call]) == 2
    assert "restoring previous image" in result.stderr


def test_dirty_release_source_is_rejected_before_build(release):
    result, calls, image = release("--apply", MOCK_DIRTY=" M gl_bot/flow.py")
    assert result.returncode != 0
    assert not any("build" in call or "compose" in call for call in calls)
    assert image == "previous-image"
