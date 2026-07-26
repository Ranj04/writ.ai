"""The demo launcher cannot arm a stage where every session is denied.

`/supervisor/sessions/*` fails closed with 503 HOOK_AUTHENTICATION_NOT_CONFIGURED
when the service holds no key (services/supervisor_api.py). Three processes need
the same value and read it from three different places — the services from .env
via `load_dotenv()`, `demo_api.py` from the bare environment, the hooks from each
session's settings.json — so a key set in only one place produces a rehearsal in
which nothing registers and nothing is preserved. That looks exactly like the
product not working, which is why it is pinned here rather than in a runbook.
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DEMO_DIR = REPO_ROOT / "scripts" / "demo"
LIB_PATH = DEMO_DIR / "lib.sh"
UP_PATH = DEMO_DIR / "up.sh"
DEMO_API_PATH = DEMO_DIR / "demo_api.py"

DEMO_KEY = "dragback-demo-hook-key"


@pytest.fixture(scope="module")
def demo_api() -> Iterator[ModuleType]:
    """Import the helper by path. It is a 3.9-compatible script, not a package."""

    spec = importlib.util.spec_from_file_location("dragback_demo_api", DEMO_API_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    try:
        yield module
    finally:
        sys.modules.pop(spec.name, None)


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    """A checkout-shaped tree, so lib.sh resolves REPO_DIR to a .env we control."""

    demo = tmp_path / "scripts" / "demo"
    demo.mkdir(parents=True)
    (demo / "lib.sh").write_text(LIB_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    return tmp_path


def _lib_in(repo: Path) -> Path:
    return repo / "scripts" / "demo" / "lib.sh"


def _resolve(repo: Path, env: dict[str, str]) -> dict[str, str]:
    script = (
        'source "$1"\n'
        'printf "%s\\n" "$HOOK_API_KEY_SOURCE"\n'
        'printf "%s\\n" "$DRAGBACK_HOOK_API_KEY"\n'
        'if env | grep -q "^DRAGBACK_HOOK_API_KEY="; then\n'
        '  printf "exported\\n"\n'
        "else\n"
        '  printf "not-exported\\n"\n'
        "fi\n"
    )
    completed = subprocess.run(
        ["bash", "-c", script, "bash", str(_lib_in(repo))],
        capture_output=True,
        text=True,
        env={"PATH": os.environ.get("PATH", ""), "HOME": str(repo), **env},
        cwd=str(repo),
        check=True,
    )
    source, key, exported = completed.stdout.splitlines()[:3]
    return {"source": source, "key": key, "exported": exported}


def test_the_shell_environment_wins(fake_repo: Path) -> None:
    (fake_repo / ".env").write_text("DRAGBACK_HOOK_API_KEY=from-dotenv\n", encoding="utf-8")

    resolved = _resolve(fake_repo, {"DRAGBACK_HOOK_API_KEY": "from-shell"})

    assert resolved["key"] == "from-shell"
    assert resolved["source"] == "environment"


def test_dotenv_is_used_when_the_shell_is_silent(fake_repo: Path) -> None:
    """The exact gap that produced 503s: the key lived only in .env.

    The services read it there through `load_dotenv()`, but `demo_api.py` reads
    the bare environment and never loads .env, so the generated settings files
    omitted it and the hooks authenticated with nothing.
    """

    (fake_repo / ".env").write_text(
        "DRAGBACK_ENV=development\nDRAGBACK_HOOK_API_KEY=from-dotenv\n",
        encoding="utf-8",
    )

    resolved = _resolve(fake_repo, {})

    assert resolved["key"] == "from-dotenv"
    assert resolved["source"] == ".env"


def test_the_demo_default_is_the_last_resort(fake_repo: Path) -> None:
    resolved = _resolve(fake_repo, {})

    assert resolved["key"] == DEMO_KEY
    assert resolved["source"] == "default"


def test_an_empty_dotenv_assignment_does_not_read_as_a_key(fake_repo: Path) -> None:
    """`.env.example` ships the key empty. Copying it must not disable the demo."""

    (fake_repo / ".env").write_text("DRAGBACK_HOOK_API_KEY=\n", encoding="utf-8")

    resolved = _resolve(fake_repo, {})

    assert resolved["key"] == DEMO_KEY
    assert resolved["source"] == "default"


def test_a_quoted_dotenv_value_is_unwrapped(fake_repo: Path) -> None:
    (fake_repo / ".env").write_text(
        'DRAGBACK_HOOK_API_KEY="quoted-key"\n', encoding="utf-8"
    )

    assert _resolve(fake_repo, {})["key"] == "quoted-key"


@pytest.mark.parametrize("env", [{}, {"DRAGBACK_HOOK_API_KEY": "from-shell"}])
def test_the_key_is_always_exported(fake_repo: Path, env: dict[str, str]) -> None:
    """A plain shell variable never reaches uvicorn or demo_api.py."""

    assert _resolve(fake_repo, env)["exported"] == "exported"


def test_the_dotenv_file_is_never_evaluated(fake_repo: Path) -> None:
    """Sourcing .env would run whatever is in it. It is parsed, never executed."""

    marker = fake_repo / "executed"
    (fake_repo / ".env").write_text(
        f"DRAGBACK_HOOK_API_KEY=parsed-not-run\nEVIL=$(touch {marker})\n",
        encoding="utf-8",
    )

    resolved = _resolve(fake_repo, {})

    assert resolved["key"] == "parsed-not-run"
    assert not marker.exists()


def test_up_sh_passes_the_key_to_every_service() -> None:
    """The services are launched through `env`, which lists what they depend on.

    Inheritance alone would work today, but the URLs next to it are explicit for
    the same reason: a reader has to be able to see that the service needs this.
    """

    source = UP_PATH.read_text(encoding="utf-8")
    launch = source.split("exec nohup env", 1)[1].split("--host", 1)[0]
    assert '"DRAGBACK_HOOK_API_KEY=$DRAGBACK_HOOK_API_KEY"' in launch


def test_up_sh_reports_which_key_it_resolved() -> None:
    """Silent resolution is how the wrong key gets used on stage."""

    source = UP_PATH.read_text(encoding="utf-8")
    assert "$HOOK_API_KEY_SOURCE" in source


def _write_settings(demo_api: ModuleType, dest: Path) -> int:
    return demo_api.command_settings(
        [
            str(dest),
            str(REPO_ROOT),
            "http://127.0.0.1:8002",
            "/usr/bin/python3",
            ".dragback/hook-verdict-cache.json",
            "1",
        ]
    )


def test_settings_carry_the_key_to_the_hooks(
    demo_api: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under tmux the panes inherit the tmux SERVER's environment, not the
    launcher's, so this file is the only path that reliably reaches the hooks."""

    monkeypatch.setenv("DRAGBACK_HOOK_API_KEY", DEMO_KEY)
    dest = tmp_path / "settings.json"

    assert _write_settings(demo_api, dest) == demo_api.EXIT_OK

    written = json.loads(dest.read_text(encoding="utf-8"))
    assert written["env"]["DRAGBACK_HOOK_API_KEY"] == DEMO_KEY


def test_a_settings_file_holding_a_key_is_owner_only(
    demo_api: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """lib.sh may resolve a real per-developer token out of .env, and this file
    lives under the demo root. Assume it is a secret."""

    monkeypatch.setenv("DRAGBACK_HOOK_API_KEY", "a-real-looking-token")
    dest = tmp_path / "settings.json"

    assert _write_settings(demo_api, dest) == demo_api.EXIT_OK

    mode = stat.S_IMODE(dest.stat().st_mode)
    assert mode == 0o600, oct(mode)


def test_an_existing_settings_file_is_restricted_too(
    demo_api: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """chmod runs after the write; a rerun over a world-readable file must fix it."""

    monkeypatch.setenv("DRAGBACK_HOOK_API_KEY", "a-real-looking-token")
    dest = tmp_path / "settings.json"
    dest.write_text("{}\n", encoding="utf-8")
    dest.chmod(0o644)

    assert _write_settings(demo_api, dest) == demo_api.EXIT_OK

    assert stat.S_IMODE(dest.stat().st_mode) == 0o600


def test_settings_still_carry_no_tool_input_or_transcript_data(
    demo_api: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The privacy rule covers what the hook is configured to send, not just what
    it sends: the env block is the hook's whole configuration surface."""

    monkeypatch.setenv("DRAGBACK_HOOK_API_KEY", DEMO_KEY)
    dest = tmp_path / "settings.json"
    _write_settings(demo_api, dest)

    env_block = json.loads(dest.read_text(encoding="utf-8"))["env"]
    assert set(env_block) == {
        "DRAGBACK_HOOK_ENDPOINT",
        "DRAGBACK_HOOK_TIMEOUT_SECONDS",
        "DRAGBACK_HOOK_CACHE_PATH",
        "DRAGBACK_HOOK_API_KEY",
    }


def test_the_example_env_warns_that_an_empty_key_denies_everything() -> None:
    """It ships empty, so the file itself has to say what empty costs."""

    text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    block = text.split("DRAGBACK_HOOK_API_KEY=", 1)[0]
    tail = block[block.rindex("# Claude Code hook enforcement") :]
    assert "HOOK_AUTHENTICATION_NOT_CONFIGURED" in tail
