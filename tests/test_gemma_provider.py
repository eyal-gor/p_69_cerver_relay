"""
Unit tests for the Gemma CLI provider + its runner.

Gemma is wired in like Grok: there's no vendor binary, so the provider
runs a bundled Python runner (`gemma_runner`) that POSTs to Google's
OpenAI-compatible Gemini endpoint and translates the response back into
the claude `--output-format` surface the relay parser consumes.

Two halves:
  - GemmaProvider: registry membership, "always installed" semantics
    (never falls back to claude), auth-status from a Gemini key, and the
    argv/env its build_*_command methods emit.
  - gemma_runner: key sanitization, the no-key guard, and the
    OpenAI→claude-stream-json translation incl. the system-prompt fold
    (Gemma has no system role) and usage-key remap.

All tests are offline: the no-key guard runs before any HTTP, and the
translation tests monkeypatch urlopen with a canned response.

Run with: uv run --with pytest python -m pytest tests/test_gemma_provider.py
"""

import io
import json
import sys
from pathlib import Path

import pytest

# Make the package importable when running from repo root.
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

from branch_monkey_mcp.bridge_and_local_actions import cli_providers  # noqa: E402
from branch_monkey_mcp.bridge_and_local_actions.cli_providers import (  # noqa: E402
    GemmaProvider,
    get_provider,
)
from branch_monkey_mcp.bridge_and_local_actions import gemma_runner  # noqa: E402

_RUNNER_MODULE = "branch_monkey_mcp.bridge_and_local_actions.gemma_runner"


# ---------------------------------------------------------------------------
# Provider: registry + availability
# ---------------------------------------------------------------------------

def test_gemma_is_registered():
    assert "gemma" in cli_providers._PROVIDERS
    assert isinstance(cli_providers._PROVIDERS["gemma"], GemmaProvider)


def test_get_provider_returns_gemma_without_falling_back():
    """The runner ships in-package, so is_available() is always truthy and
    get_provider() must NOT silently fall back to claude (the behaviour for
    a not-installed provider)."""
    provider = get_provider("gemma")
    assert isinstance(provider, GemmaProvider)
    assert provider.is_available()  # truthy → no fallback to claude


# ---------------------------------------------------------------------------
# Provider: auth status from a Gemini key
# ---------------------------------------------------------------------------

def test_auth_status_unauthenticated_without_key(monkeypatch):
    monkeypatch.setattr(cli_providers, "_load_config", lambda: {})
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    status = GemmaProvider().get_auth_status()
    assert status["authenticated"] is False
    assert status["method"] == "none"


def test_auth_status_from_env_key(monkeypatch):
    monkeypatch.setattr(cli_providers, "_load_config", lambda: {})
    monkeypatch.setenv("GEMINI_API_KEY", "AIza" + "x" * 35)
    status = GemmaProvider().get_auth_status()
    assert status["authenticated"] is True
    assert status["method"] == "api_key"


def test_auth_status_from_stored_config(monkeypatch):
    monkeypatch.setattr(
        cli_providers, "_load_config", lambda: {"gemini_api_key": "AIza" + "y" * 35}
    )
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    status = GemmaProvider().get_auth_status()
    assert status["authenticated"] is True


# ---------------------------------------------------------------------------
# Provider: build_*_command argv + env
# ---------------------------------------------------------------------------

def test_build_run_command_invokes_runner_with_model(monkeypatch):
    monkeypatch.setattr(GemmaProvider, "get_auth_env", lambda self: {})
    cmd = GemmaProvider().build_run_command("hello world", model="gemma-3-27b-it")
    assert cmd.args[0] == sys.executable
    assert cmd.args[1] == "-m"
    assert cmd.args[2] == _RUNNER_MODULE
    assert cmd.args[3:5] == ["-p", "hello world"]
    assert "stream-json" in cmd.args
    # model override threads through to the runner's --model flag
    assert "--model" in cmd.args
    assert cmd.args[cmd.args.index("--model") + 1] == "gemma-3-27b-it"
    # nested launches need CLAUDECODE removed
    assert cmd.env_overrides == {"CLAUDECODE": None}


def test_build_text_and_oneshot_output_formats(monkeypatch):
    monkeypatch.setattr(GemmaProvider, "get_auth_env", lambda self: {})
    p = GemmaProvider()
    text_cmd = p.build_text_command("hi")
    one_cmd = p.build_oneshot_command("hi")
    assert text_cmd.args[text_cmd.args.index("--output-format") + 1] == "text"
    assert one_cmd.args[one_cmd.args.index("--output-format") + 1] == "json"


def test_system_prompt_passed_as_append_flag(monkeypatch):
    monkeypatch.setattr(GemmaProvider, "get_auth_env", lambda self: {})
    cmd = GemmaProvider().build_run_command(
        "do the thing", system_prompt="You are terse."
    )
    assert "--append-system-prompt" in cmd.args
    assert cmd.args[cmd.args.index("--append-system-prompt") + 1] == "You are terse."


def test_auth_env_injected_into_command(monkeypatch):
    monkeypatch.setattr(
        GemmaProvider, "get_auth_env", lambda self: {"GEMINI_API_KEY": "AIzaSECRET"}
    )
    cmd = GemmaProvider().build_run_command("hi")
    assert cmd.env_inject.get("GEMINI_API_KEY") == "AIzaSECRET"


# ---------------------------------------------------------------------------
# Runner: key sanitization
# ---------------------------------------------------------------------------

def test_sanitize_key_extracts_aiza_run_from_noise():
    real = "AIza" + "A1b2C3d4" * 5  # 44 chars, valid shape
    noisy = f"\x1b[?2004h{real}↓ "  # paste-mode escape + arrow + nbsp
    assert gemma_runner._sanitize_key(noisy) == real


def test_sanitize_key_strips_nonascii_when_no_aiza_match():
    # No AIza pattern → printable-ASCII fallback strip.
    assert gemma_runner._sanitize_key("key↓123 ") == "key123"


# ---------------------------------------------------------------------------
# Runner: no-key guard (offline — runs before any HTTP)
# ---------------------------------------------------------------------------

def test_runner_exits_2_without_key(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", [_RUNNER_MODULE, "-p", "hi", "--output-format", "text"])
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    rc = gemma_runner._run()
    assert rc == 2
    assert "no API key" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Runner: OpenAI → claude stream-json translation (monkeypatched urlopen)
# ---------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


def _patch_urlopen(monkeypatch, payload, captured):
    def fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data.decode())
        captured["url"] = req.full_url
        captured["headers"] = {k.lower(): v for k, v in req.header_items()}
        return _FakeResp(payload)

    monkeypatch.setattr(gemma_runner.urllib.request, "urlopen", fake_urlopen)


_OPENAI_PAYLOAD = {
    "choices": [{"message": {"content": "Hello from Gemma"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
}


def test_runner_translates_to_claude_stream_json(monkeypatch):
    captured = {}
    _patch_urlopen(monkeypatch, _OPENAI_PAYLOAD, captured)
    monkeypatch.setattr(
        sys, "argv", [_RUNNER_MODULE, "-p", "hi", "--output-format", "stream-json"]
    )
    monkeypatch.setenv("GEMINI_API_KEY", "AIza" + "z" * 35)

    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    rc = gemma_runner._run()
    assert rc == 0

    events = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
    kinds = [e.get("type") for e in events]
    assert kinds == ["system", "assistant", "result"]

    init = events[0]
    assert init["subtype"] == "init" and init["provider"] == "google"

    assistant = events[1]
    assert assistant["message"]["content"][0]["text"] == "Hello from Gemma"

    result = events[2]
    assert result["result"] == "Hello from Gemma"
    # OpenAI usage keys are remapped to the claude-style keys the relay expects.
    assert result["usage"] == {"input_tokens": 11, "output_tokens": 7}

    # Endpoint + auth shape.
    assert captured["url"].endswith("/chat/completions")
    assert captured["headers"]["authorization"].startswith("Bearer AIza")


def test_runner_folds_system_prompt_into_user_turn(monkeypatch):
    """Gemma has no system role — the system prompt must be prepended to the
    user message, and no `system`-role message may be sent."""
    captured = {}
    _patch_urlopen(monkeypatch, _OPENAI_PAYLOAD, captured)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            _RUNNER_MODULE,
            "-p", "What is 2+2?",
            "--output-format", "json",
            "--append-system-prompt", "Answer in one word.",
        ],
    )
    monkeypatch.setenv("GEMINI_API_KEY", "AIza" + "q" * 35)

    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    rc = gemma_runner._run()
    assert rc == 0

    messages = captured["body"]["messages"]
    assert all(m["role"] != "system" for m in messages)
    assert messages[0]["role"] == "user"
    assert messages[0]["content"].startswith("Answer in one word.")
    assert "What is 2+2?" in messages[0]["content"]
    assert captured["body"]["model"] == gemma_runner.DEFAULT_MODEL
