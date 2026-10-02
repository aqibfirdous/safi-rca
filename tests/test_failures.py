"""Behaviour under a spent provider quota, and client disconnects.

Both were found by running the dashboard for real: a Gemini free-tier key ran
out of daily quota mid-session, and the browser then hung up on the long
request.  Neither should produce a stack trace or an unactionable message.
"""

from __future__ import annotations

import socket
import threading

import pytest

from safi_rca.errors import SafiRcaError
from safi_rca.runtime import openhands_runtime as oh
from safi_rca.web import _DISCONNECTS, Handler

# The literal provider response, trimmed.  A real quota rejection is a large
# JSON blob naming an internal quota metric, not a short message.
QUOTA_BLOB = (
    "litellm.RateLimitError: litellm.RateLimitError: geminiException - {\n"
    '  "error": {\n'
    '    "code": 429,\n'
    '    "message": "You exceeded your current quota, please check your plan and '
    'billing details.",\n'
    '    "status": "RESOURCE_EXHAUSTED",\n'
    "    details: [\n"
    '      "@type": "type.googleapis.com/google.rpc.QuotaFailure",\n'
    "      violations: [\n"
    "        quotaMetric: generativelanguage.googleapis.com/"
    "generate_content_free_tier_requests,\n"
    "        quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier,\n"
    "        quotaValue: '20'\n"
    "      ]\n"
    "    ]\n"
    "  }\n"
    "}\n"
    ". Attempt #4 | You can customize retry values in the configuration."
)


def test_a_spent_quota_is_recognised():
    assert oh.quota_exhausted(Exception(QUOTA_BLOB))


#: OpenRouter pre-authorises against max_tokens and rejects a thin balance.
CREDIT_BLOB = (
    "litellm.APIError: APIError: OpenrouterException - "
    '{"error":{"message":"This request requires more credits, or fewer '
    'max_tokens. You requested up to 64000 tokens, but can only afford '
    'less."}}'
)


def test_insufficient_credit_is_distinguished_from_a_rate_limit():
    assert oh.credits_exhausted(Exception(CREDIT_BLOB))
    assert not oh.quota_exhausted(Exception(CREDIT_BLOB)), "the key works, the balance does not"
    assert not oh.credits_exhausted(Exception(QUOTA_BLOB))


def test_credit_message_is_actionable():
    text = oh.credits_message(Exception(CREDIT_BLOB), "openrouter/anthropic/claude-sonnet-4.5")
    assert "insufficient credit" in text
    assert "openrouter/anthropic/claude-sonnet-4.5" in text
    assert "SAFI_RCA_MAX_OUTPUT_TOKENS" in text
    assert "64000" not in text, "the provider's internal number is not the message"


def test_the_reply_ceiling_is_far_below_the_sdk_default(monkeypatch):
    """64000 output tokens is ~$1 of pre-authorisation for a one-report reply."""

    monkeypatch.delenv("SAFI_RCA_MAX_OUTPUT_TOKENS", raising=False)
    assert oh.max_output_tokens() == oh.DEFAULT_MAX_OUTPUT_TOKENS
    assert oh.max_output_tokens() <= 8192


def test_the_reply_ceiling_can_be_overridden(monkeypatch):
    monkeypatch.setenv("SAFI_RCA_MAX_OUTPUT_TOKENS", "1024")
    assert oh.max_output_tokens() == 1024
    monkeypatch.setenv("SAFI_RCA_MAX_OUTPUT_TOKENS", "nonsense")
    assert oh.max_output_tokens() == oh.DEFAULT_MAX_OUTPUT_TOKENS


def test_a_thin_balance_is_reported_as_a_credit_problem():
    class _Conversation:
        def send_message(self, _message):
            pass

        def run(self):
            raise Exception(CREDIT_BLOB)

    with pytest.raises(SafiRcaError) as excinfo:
        oh._guarded_run(_Conversation(), "analyse this")
    assert "insufficient credit" in str(excinfo.value)


def test_ordinary_failures_are_not_mistaken_for_a_quota():
    for message in ("connection reset", "boom", "invalid api key", "timed out"):
        assert not oh.quota_exhausted(Exception(message)), message


def test_quota_message_names_the_model_and_the_reset_time():
    text = oh.quota_message(Exception(QUOTA_BLOB + "\nPlease retry in 8h41m27.6s."))
    assert "8h41m" in text, "the operator needs to know when it clears"
    assert "LLM_MODEL" in text, "the message must suggest an actionable next step"
    assert "local" in text, "a quota-free runtime should be offered"
    # The internal metric name is noise; do not dump the whole blob.
    assert len(text) < 400


def test_quota_message_survives_a_missing_reset_hint():
    text = oh.quota_message(Exception(QUOTA_BLOB))
    assert "quota exhausted" in text
    assert "resets in" not in text


def test_guarded_run_reports_a_quota_as_runtime_unavailable():
    """A spent quota must reach the operator as a readable reason, not JSON."""

    class _Conversation:
        def send_message(self, _message):
            pass

        def run(self):
            raise Exception(QUOTA_BLOB)

    with pytest.raises(SafiRcaError) as excinfo:
        oh._guarded_run(_Conversation(), "analyse this")

    message = str(excinfo.value)
    assert "quota exhausted" in message
    assert "429" not in message, "the raw provider status code is not the message"


def test_guarded_run_does_not_mask_other_errors():
    class _Conversation:
        def send_message(self, _message):
            pass

        def run(self):
            raise ValueError("something else entirely")

    with pytest.raises(ValueError, match="something else entirely"):
        oh._guarded_run(_Conversation(), "analyse this")


def test_guarded_run_lets_success_through():
    calls = []

    class _Conversation:
        def send_message(self, message):
            calls.append(message)

        def run(self):
            calls.append("run")

    oh._guarded_run(_Conversation(), "analyse this")
    assert calls == ["analyse this", "run"]


def test_the_sdk_is_asked_not_to_back_off_for_an_hour():
    """litellm retries are uncountable; the wait between them must still be short."""

    assert 0 < oh.RETRY_MAX_WAIT_SECONDS <= 15


def test_a_model_name_selects_its_own_credential():
    assert oh.key_for_model("openrouter/anthropic/claude-sonnet-4.5") == "OPENROUTER_API_KEY"
    assert oh.key_for_model("anthropic/claude-sonnet-4-5") == "ANTHROPIC_API_KEY"
    assert oh.key_for_model("gemini/gemini-3-flash-preview") == "GEMINI_API_KEY"
    assert oh.key_for_model("openai/gpt-4o") == "OPENAI_API_KEY"
    assert oh.key_for_model("gpt-4o") == "OPENAI_API_KEY", "no prefix means OpenAI"
    assert oh.key_for_model("") is None


def test_the_requested_provider_wins_over_a_fixed_key_order(monkeypatch):
    """Changing LLM_MODEL alone must switch provider.

    With both keys configured, the old code returned GEMINI_API_KEY for every
    model because it walked LLM_ENV_KEYS in declaration order, so asking for an
    openrouter/ model sent an OpenRouter-prefixed request with a Gemini key.
    """

    monkeypatch.setenv("GEMINI_API_KEY", "gemini-secret")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-secret")

    assert oh._credentials("openrouter/anthropic/claude-sonnet-4.5") == (
        "OPENROUTER_API_KEY",
        "sk-or-v1-secret",
    )
    assert oh._credentials("gemini/gemini-3-flash-preview") == ("GEMINI_API_KEY", "gemini-secret")


def test_only_the_needed_credential_is_required(monkeypatch):
    """A provider key is enough on its own; no fallback key is required."""

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-only")

    assert oh._credentials("openrouter/anthropic/claude-sonnet-4.5")[0] == "OPENROUTER_API_KEY"
    # No key for the requested provider, so the generic order still applies
    # rather than reporting "no credentials".
    assert oh._credentials("gemini/gemini-3-flash-preview")[0] == "OPENROUTER_API_KEY"


def test_availability_reports_the_key_that_matches_the_model(monkeypatch):
    """The summary line must name the provider actually in use."""

    from safi_rca.runtime.openhands_runtime import OpenHandsRuntime

    monkeypatch.setenv("GEMINI_API_KEY", "gemini-secret")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-secret")

    ok, reason = OpenHandsRuntime(model="openrouter/anthropic/claude-sonnet-4.5").availability()
    if ok:  # openhands is not installed in every environment
        assert "OPENROUTER_API_KEY" in reason
        assert "WARNING" not in reason, "matching provider must not warn"


def test_disconnects_are_recognised_as_os_errors():
    """These must be caught before the generic `except Exception`."""

    for exc in (BrokenPipeError(), ConnectionResetError(), ConnectionAbortedError()):
        assert isinstance(exc, _DISCONNECTS)
        assert isinstance(exc, OSError)


def test_a_client_that_hangs_up_does_not_break_the_server():
    """The exact failure from the console: a disconnect while writing a page.

    Request a page, then drop the socket before reading the response.  The
    handler must return quietly; previously the 500 fallback tried to write to
    the same dead socket and raised a second, more confusing error.
    """

    from http.server import ThreadingHTTPServer

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        for _ in range(5):
            sock = socket.create_connection((host, port), timeout=10)
            sock.sendall(b"GET /run?repo=x&sha=y&evidence=z HTTP/1.1\r\nHost: x\r\n\r\n")
            sock.close()  # abandon the response mid-flight
        # The server must still be serving new clients afterwards.
        probe = socket.create_connection((host, port), timeout=10)
        probe.sendall(b"GET /health HTTP/1.1\r\nHost: x\r\n\r\n")
        assert b"200" in probe.recv(4096)
        probe.close()
    finally:
        server.shutdown()
        server.server_close()


def test_send_quietly_swallows_a_disconnect():
    sent = {}

    class _Dead(Handler):
        def _send(self, body, status=200):
            sent["body"] = body
            raise ConnectionAbortedError(10053, "aborted")

    _Dead.__new__(_Dead)._send_quietly("page")  # type: ignore[misc]
    assert sent["body"] == "page", "the page was rendered, then dropped"