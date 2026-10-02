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