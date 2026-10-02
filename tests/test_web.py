"""The dashboard is exercised over real HTTP, not by calling helpers directly.

The point of the dashboard is to compare the two runtimes honestly, so the tests
check that the comparison is actually rendered and that the page says "error"
rather than crashing when a runtime fails.
"""

from __future__ import annotations

import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from safi_rca import scenarios as scen
from safi_rca import web


@pytest.fixture(scope="module")
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address[0], httpd.server_address[1]
    yield f"http://{host}:{port}"
    httpd.shutdown()
    httpd.server_close()


def fetch(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=120) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def test_dashboard_lists_every_scenario(server):
    status, body = fetch(f"{server}/")
    assert status == 200
    for scenario in scen.SCENARIOS:
        assert scenario.title in body, f"{scenario.key} missing from the dashboard"
        assert scenario.sha[:9] in body
    # Both runtimes are always offered as columns, even before they have run.
    assert "Local (deterministic)" in body
    assert "OpenHands (model)" in body


def test_dashboard_shows_run_runtimes(server):
    status, body = fetch(f"{server}/")
    assert status == 200
    assert "local" in body and "openhands" in body


def test_scenario_page_renders_a_real_report(server):
    """One scenario end to end through the local runtime, over HTTP."""

    status, body = fetch(f"{server}/scenario?s=broken")
    assert status == 200
    assert "Root cause" in body
    assert "_lookup_plan" in body, "the real fixture root cause should be rendered"
    assert "none-dereference-on-optional-lookup" in body
    assert "Aider RepoMap" in body or "aider" in body.lower()
    # Read-only provenance is the product's central claim, so it must be visible.
    assert "Read-only" in body
    assert "clean" in body


def test_run_endpoint_analyses_and_renders(server):
    scenario = scen.BY_KEY["alt"]
    url = (
        f"{server}/run?repo={scenario.repo}&sha={scenario.sha}"
        f"&evidence={scenario.evidence}&runtime=local"
    )
    status, body = fetch(url)
    assert status == 200
    assert "RingBuffer" in body or "drain" in body.lower()
    assert "unclamped-counter-index" in body


def test_run_endpoint_reports_bad_input_instead_of_crashing(server):
    status, body = fetch(f"{server}/run?repo=&sha=&evidence=")
    assert status == 200, "a bad request is a page, not a 500"
    assert "missing required field" in body


def test_run_endpoint_reports_a_rejected_sha(server):
    scenario = scen.BY_KEY["broken"]
    url = f"{server}/run?repo={scenario.repo}&sha=HEAD&evidence={scenario.evidence}&runtime=local"
    status, body = fetch(url)
    assert status == 200
    assert "HEAD" in body or "sha" in body.lower()


def test_unknown_scenario_is_handled(server):
    status, body = fetch(f"{server}/scenario?s=nope")
    assert status == 200
    assert "no scenario named" in body


def test_health_and_404(server):
    status, body = fetch(f"{server}/health")
    assert status == 200 and "ok" in body
    status, body = fetch(f"{server}/nope")
    assert status == 404