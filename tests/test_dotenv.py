"""`.env` handling.

The two properties that matter are that a credential file can never be
committed, and that it can never override a variable the operator set
deliberately on the command line.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from safi_rca import dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def test_dotenv_is_never_committed():
    """A credential file in the repo would be a leaked credential."""

    ignored = PROJECT_ROOT / ".gitignore"
    patterns = {line.strip() for line in ignored.read_text(encoding="utf-8").splitlines()}
    assert ".env" in patterns, ".env must be gitignored"
    # The example file is the documented, secret-free counterpart and is allowed.
    assert "!.env.example" in patterns
    assert (PROJECT_ROOT / ".env.example").is_file()


def test_env_file_is_not_tracked():
    import subprocess

    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", ".env"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    assert tracked.returncode != 0, ".env must not be tracked by git"


def test_real_environment_wins_over_the_file(monkeypatch, tmp_path):
    """`set GEMINI_API_KEY=...` must not be shadowed by .env."""

    env = tmp_path / ".env"
    env.write_text("GEMINI_API_KEY=from-file\nLLM_MODEL=gemini/x\n", encoding="utf-8")

    monkeypatch.delenv("SAFI_RCA_NO_DOTENV", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "from-shell")
    monkeypatch.delenv("LLM_MODEL", raising=False)

    applied = dotenv.load_dotenv(start=tmp_path, force=True)

    assert applied["GEMINI_API_KEY"] == "from-file", "the file should still be read"
    import os

    assert os.environ["GEMINI_API_KEY"] == "from-shell", "the shell must win"
    assert os.environ["LLM_MODEL"] == "gemini/x", "unset variables do come from the file"


def test_parser_handles_comments_quotes_and_export(monkeypatch):
    text = """
# a comment

export GEMINI_API_KEY='single-quoted'
ANTHROPIC_API_KEY="double-quoted"
OPENROUTER_API_KEY=sk-or-v1-abc123
EMPTY=
MALFORMED_NO_EQUALS
  INDENTED_API_KEY=works
"""
    parsed = dotenv._parse(text)
    assert parsed["GEMINI_API_KEY"] == "single-quoted"
    assert parsed["ANTHROPIC_API_KEY"] == "double-quoted"
    assert parsed["OPENROUTER_API_KEY"] == "sk-or-v1-abc123"
    assert parsed["EMPTY"] == ""
    assert "MALFORMED_NO_EQUALS" not in parsed
    assert parsed["INDENTED_API_KEY"] == "works"


def test_missing_file_is_harmless(monkeypatch, tmp_path):
    """A missing .env must not raise."""

    monkeypatch.setenv("SAFI_RCA_NO_DOTENV", "1")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert dotenv.load_dotenv(start=tmp_path, force=True) == {}


def test_the_opt_out_prevents_reading_a_real_credential(monkeypatch, tmp_path):
    """CI must never pick up the developer's key and start a live model call."""

    env = tmp_path / ".env"
    env.write_text("GEMINI_API_KEY=should-not-be-used\n", encoding="utf-8")
    monkeypatch.setenv("SAFI_RCA_NO_DOTENV", "1")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    assert dotenv.load_dotenv(start=tmp_path, force=True) == {}

    import os

    assert "GEMINI_API_KEY" not in os.environ


def test_scratch_path_survives_cmd_exe_trailing_spaces(monkeypatch, tmp_path):
    """`set VAR=value && cmd` leaves a trailing space in the value on cmd.exe.

    That produced paths like ``B:\\repo\\.tmp \\safi-rca-x``, which fail deep
    inside tempfile with a bare WinError 3 and no hint of the cause.
    """

    from safi_rca.api import temp_root

    repo = tmp_path / "repo"
    repo.mkdir()
    scratch = tmp_path / "scratch dir with spaces"

    monkeypatch.setenv("SAFI_RCA_TMP_DIR", str(scratch) + " ")
    root = temp_root(repo)
    assert root.is_dir()
    assert root == scratch.resolve()
    assert " " not in root.name.strip() or root.name == scratch.name

    # Quoted values and stray quotes are also tolerated.
    monkeypatch.setenv("SAFI_RCA_TMP_DIR", f'"{scratch}" ')
    assert temp_root(repo) == scratch.resolve()


def test_redact_never_shows_the_secret():
    """A synthetic key, deliberately: a real one here would trip push protection."""

    assert dotenv.redact("") == ""
    assert dotenv.redact("abc") == "***"
    secret = "AQ.Ab8FAKEfakefakefakefakefakefakeFAKE01"
    out = dotenv.redact(secret)
    assert "Ab8FAKEfakefake" not in out, "the middle of the key must not survive"
    assert "AQ.A" in out and "chars)" in out
    assert secret not in out


def test_runtimes_never_print_the_credential_value():
    """Availability is reported to the user; the secret must not appear in it."""

    from safi_rca.api import describe_runtimes

    blob = repr(describe_runtimes())
    assert "sk-" not in blob
    for key in ("GEMINI_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY"):
        assert f"{key}=" not in blob, f"{key} value must not be echoed"