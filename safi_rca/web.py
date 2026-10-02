"""A local dashboard for safi-rca, in the standard library only.

Purpose: make it obvious *what the tool does* and, just as importantly, *where
it is unreliable*.  The dashboard runs each acceptance scenario through both
runtimes and puts them side by side, because the interesting fact is the
difference between them -- the deterministic engine says "the traceback cannot
occur at this sha", while a language model will often invent a plausible cause
and report High confidence.

No web framework is added as a dependency.  `http.server` is enough for a
loopback-only tool, and keeping the dependency set frozen matters more here than
page polish: this repository already juggles a pinned `aider-chat` against
`openhands-sdk`, and adding a web stack would be a third conflict.

Binding is restricted to 127.0.0.1 by default.  Analyses are read-only and are
performed in a worker thread so a slow model call cannot block the UI.
"""

from __future__ import annotations

import html
import json
import threading
import traceback
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import scenarios as scen
from .api import analyze, describe_runtimes
from .errors import SafiRcaError

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

#: Analysis is bounded so one wedged model call cannot hold a request open for
#: ever.  A full OpenHands run on the fixtures takes well under this.
RUN_TIMEOUT_SECONDS = 600

STYLES = """
:root { color-scheme: light dark; --line:#d0d7de; --muted:#57606a; --bg:#fff; --fg:#1f2328; }
@media (prefers-color-scheme: dark) {
  :root { --line:#30363d; --muted:#9198a1; --bg:#0d1117; --fg:#e6edf3; }
}
* { box-sizing: border-box; }
body { margin:0; padding:1.5rem; background:var(--bg); color:var(--fg);
  font:15px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif; }
h1 { font-size:1.35rem; margin:0 0 .25rem; }
h2 { font-size:1.05rem; margin:2rem 0 .5rem; }
.sub { color:var(--muted); margin:0 0 1.25rem; }
a { color:#0969da; }
table { border-collapse:collapse; width:100%; margin:.5rem 0 1rem; }
th,td { border:1px solid var(--line); padding:.45rem .6rem; text-align:left; vertical-align:top; }
th { background:rgba(127,127,127,.09); font-weight:600; }
code,pre { font-family:ui-monospace,SFMono-Regular,Consolas,monospace; font-size:.87em; }
pre { background:rgba(127,127,127,.09); padding:.6rem; overflow:auto; border-radius:6px; }
.badge { display:inline-block; padding:.05rem .45rem; border-radius:999px;
  border:1px solid var(--line); font-size:.78rem; white-space:nowrap; }
.ok { background:#1a7f37; color:#fff; border-color:#1a7f37; }
.bad { background:#b42318; color:#fff; border-color:#b42318; }
.warn { background:#9a6700; color:#fff; border-color:#9a6700; }
.mute { background:rgba(127,127,127,.2); color:var(--muted); }
.agree { color:#1a7f37; font-weight:600; }
.disagree { color:#b42318; font-weight:600; }
form { border:1px solid var(--line); border-radius:8px; padding:.9rem; margin:1rem 0; }
label { display:block; margin:.5rem 0 .15rem; font-size:.85rem; color:var(--muted); }
input,select { width:100%; padding:.4rem; font:inherit;
  border:1px solid var(--line); border-radius:6px; background:var(--bg); color:var(--fg); }
button { margin-top:.85rem; padding:.45rem 1rem; font:inherit; cursor:pointer;
  border:1px solid var(--line); border-radius:6px; background:#1f883d; color:#fff; }
.note { color:var(--muted); font-size:.88rem; }
.err { border:1px solid #b42318; background:rgba(180,35,24,.08);
  padding:.7rem; border-radius:6px; white-space:pre-wrap; }
"""


def _esc(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


@dataclass
class RunOutcome:
    """Result of one scenario/runtime pair, including how it went wrong."""

    scenario_key: str
    runtime: str
    ok: bool = False
    error: str = ""
    payload: dict = field(default_factory=dict)


def _summarise(report) -> dict:
    """Reduce a report to plain JSON-serialisable data.

    The report is serialised through its own ``to_json`` rather than by reading
    attributes, so the dashboard can never show something the ``--json`` output
    disagrees with, and nested evidence objects arrive as dicts.
    """

    return json.loads(report.to_json())


def run_scenario(scenario: scen.Scenario, runtime: str) -> RunOutcome:
    """Analyse one scenario with one runtime.  Never raises."""

    outcome = RunOutcome(scenario_key=scenario.key, runtime=runtime)
    try:
        report = analyze(
            repo=scenario.repo,
            sha=scenario.sha,
            evidence=scenario.evidence,
            runtime=runtime,
        )
    except (SafiRcaError, FileNotFoundError, ValueError, OSError) as exc:
        outcome.error = f"{type(exc).__name__}: {exc}"
        return outcome
    except Exception as exc:  # noqa: BLE001 - a bad model reply must not 500 the page
        outcome.error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=3)}"
        return outcome
    outcome.ok = True
    outcome.payload = _summarise(report)
    return outcome


def _conf_badge(confidence: str | None) -> str:
    value = (confidence or "").strip().lower()
    css = {"high": "ok", "medium": "warn"}.get(value, "mute")
    return f'<span class="badge {css}">{_esc(confidence or "unstated")}</span>'


def _verdict_row(scenario: scen.Scenario, outcomes: dict[str, RunOutcome]) -> str:
    """One scenario: what each runtime said, and whether they agree.

    Agreement is on the *substance* -- the detector the local engine fired, or
    failing that the named component.  Two engines that agree on the defect but
    not on how sure they are still shown as agreeing, because the confidence
    gap is the thing worth reading in the next column, not a disagreement about
    the defect.
    """

    cells: list[str] = []
    agree: bool | None = None
    for runtime in ("local", "openhands"):
        outcome = outcomes.get(runtime)
        if outcome is None:
            cells.append('<td class="note">not run</td>')
            continue
        if not outcome.ok:
            cells.append(f'<td><span class="badge bad">error</span><div class="note">{_esc(outcome.error[:200])}</div></td>')
            agree = False if agree is not False else agree
            continue
        data = outcome.payload
        detectors = ", ".join(data["detectors_fired"]) or "<span class='note'>none</span>"
        cells.append(
            "<td>"
            f'<div>{_conf_badge(data.get("confidence"))}</div>'
            f'<div class="note">detector: {detectors}</div>'
            f'<div style="margin-top:.35rem">{_esc(data.get("root_cause"))}</div>'
            "</td>"
        )

    local = outcomes.get("local")
    oh = outcomes.get("openhands")
    if local is not None and oh is not None and local.ok and oh.ok:
        same = bool(local.payload["detectors_fired"]) == bool(oh.payload["detectors_fired"])
        if same and local.payload["detectors_fired"]:
            same = local.payload["detectors_fired"] == oh.payload["detectors_fired"]
        agree = same
    if agree is None:
        verdict = '<span class="badge mute">n/a</span>'
    elif agree:
        verdict = '<span class="agree">agree</span>'
    else:
        verdict = '<span class="disagree">differ</span>'
    cells.insert(0, f"<td>{verdict}</td>")

    expected = scenario.expect_detector or "none"
    got = (local.payload["detectors_fired"] if local is not None and local.ok else []) or ["none"]
    hit = '<span class="badge ok">as expected</span>' if expected in got else '<span class="badge bad">off-target</span>'

    return (
        "<tr>"
        f"<td><a href='/scenario?s={_esc(scenario.key)}'><strong>{_esc(scenario.title)}</strong></a>"
        f"<div class='note'>{_esc(scenario.repo_name)} @ {_esc(scenario.sha[:9])}</div></td>"
        + cells[0]
        + f"<td>{hit}</td></tr>"
    )


def _runtime_table() -> str:
    """Availability of each runtime, keyed by the ``--runtime`` choice."""

    try:
        described = describe_runtimes()
    except Exception as exc:  # noqa: BLE001 - discovery must not break the page
        return f'<tr><td colspan="3"><span class="badge bad">error</span> {_esc(exc)}</td></tr>'

    rows = []
    for choice in _runtimes_to_try():
        info = described.get(_RUNTIME_NAME[choice]) or described.get(choice) or {}
        available = bool(info.get("available"))
        rows.append(
            f"<tr><td><code>{_esc(choice)}</code></td>"
            f'<td><span class="badge {"ok" if available else "mute"}">'
            f'{"available" if available else "unavailable"}</span></td>'
            f'<td class="note">{_esc(info.get("reason"))}</td></tr>'
        )
    return "".join(rows)


def _dashboard_html(results: dict[str, dict[str, RunOutcome]], runtimes: dict, error: str = "") -> str:
    rows = "\n".join(_verdict_row(s, results.get(s.key, {})) for s in scen.SCENARIOS)

    banner = f'<div class="err">{_esc(error)}</div>' if error else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>safi-rca dashboard</title><style>{STYLES}</style></head><body>
<h1>safi-rca &mdash; read-only root cause analysis</h1>
<p class="sub">Every diagnosis is scoped to one exact sha, built from
<code>git archive</code> with no checkout. safi-rca reports; it never applies fixes.</p>
{banner}
<h2>Acceptance scenarios</h2>
<table>
<tr><th>Scenario</th><th>Verdict</th><th>Local (deterministic)</th>
<th>OpenHands (model)</th><th>Local vs expected</th></tr>
{rows}
</table>
<p class="note">The comparison exists because the two runtimes fail differently.
Where the evidence cannot be reproduced, or names no code site, the deterministic
engine says so and lowers its confidence; a language model tends to invent a
plausible cause and report High. Read the confidence column before the prose.</p>

<h2>Runtimes</h2>
<table><tr><th>Runtime</th><th>State</th><th>Detail</th></tr>{_runtime_table()}</table>

<h2>Run an analysis</h2>
<form method="get" action="/run">
<label for="repo">Repository</label>
<input id="repo" name="repo" value="{_esc(scen.SAMPLE_REPO)}">
<label for="sha">Exact commit sha (a branch name or <code>HEAD</code> is rejected)</label>
<input id="sha" name="sha" value="{_esc(scen.SAMPLE_BROKEN_SHA)}">
<label for="evidence">Failure evidence file</label>
<input id="evidence" name="evidence" value="{_esc(scen.EVIDENCE_DIR / 'pytest_failure.txt')}">
<label for="runtime">Runtime</label>
<select id="runtime" name="runtime">
<option value="local">local &mdash; deterministic detectors, no model</option>
<option value="openhands">openhands &mdash; live model, read-only tools</option>
<option value="auto">auto</option>
</select>
<button type="submit">Analyse</button>
</form>
<p class="note">Runs on a worker thread with a {_esc(RUN_TIMEOUT_SECONDS)}s bound.
The analysed repository is never checked out or modified.</p>
</body></html>"""


def _report_html(title: str, data: dict, back: str = "/") -> str:
    evidence_rows = "".join(
        f"<tr><td>{_esc(e.get('kind'))}</td><td><code>{_esc(e.get('file'))}:{_esc(e.get('line'))}</code></td>"
        f"<td>{_esc(e.get('symbol'))}</td><td>{_esc(e.get('statement'))}</td></tr>"
        for e in data.get("evidence", [])
    )
    ro = data.get("read_only") or {}
    rt = data.get("runtime") or {}
    repomap = data.get("repomap") or {}
    training = data.get("training") or {}
    limitations = "".join(f"<li>{_esc(x)}</li>" for x in data.get("limitations") or [])

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(title)}</title><style>{STYLES}</style></head><body>
<p><a href="{_esc(back)}">&larr; back</a></p>
<h1>{_esc(title)}</h1>
<h2>Symptom</h2><pre>{_esc(data.get('symptom'))}</pre>
<h2>Root cause</h2><pre>{_esc(data.get('root_cause'))}</pre>
<p>Component <code>{_esc(data.get('affected_component'))}</code>
{_conf_badge(data.get('confidence'))}</p>
<h2>Reasoning</h2><pre>{_esc(data.get('reasoning_summary'))}</pre>
<h2>Uncertainty</h2><pre>{_esc(data.get('uncertainty'))}</pre>
<h2>Evidence ({_esc(data.get('evidence_count', 0))})</h2>
<table><tr><th>Kind</th><th>Location</th><th>Symbol</th><th>Statement</th></tr>
{evidence_rows or '<tr><td colspan="4" class="note">none cited</td></tr>'}</table>
<h2>Recommended next action</h2><pre>{_esc(data.get('recommended_next_action'))}</pre>
<h2>Provenance</h2>
<table>
<tr><th>RepoMap producer</th><td><code>{_esc(repomap.get('producer'))}</code>
({_esc(repomap.get('files_mapped'))} files, {_esc(repomap.get('chars'))} chars)</td></tr>
<tr><th>Repository training</th><td>{_esc(training.get('repo_fact_count'))} facts</td></tr>
<tr><th>Role training</th><td>{_esc(training.get('role_fact_count'))} facts</td></tr>
<tr><th>Read-only</th><td><span class="badge {"ok" if ro.get('clean') else "bad"}">
{"clean" if ro.get('clean') else "MUTATED"}</span>
changed files: {_esc(ro.get('files_changed')) or 'none'}</td></tr>
<tr><th>HEAD before / after</th><td><code>{_esc(ro.get('head_before'))}</code> /
<code>{_esc(ro.get('head_after'))}</code></td></tr>
<tr><th>Analysed sha</th><td><code>{_esc((data.get('sha_protection') or {}).get('analysed_sha'))}</code></td></tr>
<tr><th>Runtime</th><td>{_esc(rt.get('selected'))} &mdash; {_esc(rt.get('backend'))}</td></tr>
</table>
{"<h2>Limitations</h2><ul>" + limitations + "</ul>" if limitations else ""}
<h2>Raw JSON</h2><pre>{_esc(json.dumps(data, indent=1))}</pre>
</body></html>"""


def _scenario_html(key: str) -> str:
    scenario = scen.BY_KEY.get(key)
    if scenario is None:
        return _report_html("Unknown scenario", {"symptom": f"no scenario named {key!r}"})
    runtimes = _runtimes_to_try()
    blocks = []
    for runtime in runtimes:
        outcome = run_scenario(scenario, runtime)
        heading = f"<h2>{_esc(runtime)}</h2>"
        if not outcome.ok:
            blocks.append(heading + f'<div class="err">{_esc(outcome.error)}</div>')
        else:
            blocks.append(heading + _report_html_inner(outcome.payload))
    note = f'<p class="note">{_esc(scenario.note)}</p>'
    head = f"<h1>{_esc(scenario.title)}</h1><p>{_esc(scenario.repo_name)} @ <code>{_esc(scenario.sha)}</code></p>{note}"
    return _page(f"{scenario.title} &mdash; safi-rca", head + "".join(blocks))


def _report_html_inner(data: dict) -> str:
    return _report_html("", data).split("<body>", 1)[-1].split("</body>", 1)[0]


def _page(title: str, body: str) -> str:
    return (
        f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{_esc(title)}</title><style>{STYLES}</style></head><body>{body}</body></html>"
    )


def _runtimes_to_try() -> list[str]:
    """The runtimes to show, in order, using the names the CLI accepts.

    These are the ``--runtime`` choices, which are not the same strings the
    runtime objects report as their ``.name`` (``local`` vs
    ``local-deterministic``), so availability is looked up through
    :data:`_RUNTIME_NAME`.
    """

    return ["local", "openhands"]


#: ``--runtime`` choice -> the name the runtime object reports.
_RUNTIME_NAME = {"local": "local-deterministic", "openhands": "openhands"}


#: A client that hangs up mid-response.  These are not server faults: there is
#: nobody left to send a page to, and reporting them as 500s produced a
#: double-fault traceback (the error page write also raised) whenever a browser
#: navigated away or reloaded during a long model call.
_DISCONNECTS = (BrokenPipeError, ConnectionAbortedError, ConnectionResetError)


class Handler(BaseHTTPRequestHandler):
    server_version = "safi-rca"
    _results: dict = {}
    _lock = threading.Lock()

    def log_message(self, *_args) -> None:  # keep the console clean
        pass

    def handle(self) -> None:
        """Serve requests, treating a vanished client as routine.

        ``BaseHTTPRequestHandler.handle`` reads the request line and the
        response body, so a client that disconnects can raise on either side.
        Overriding this catches both, where catching inside ``do_GET`` only
        covers the write.
        """

        try:
            super().handle()
        except _DISCONNECTS:
            pass

    def _send(self, body: str, status: int = 200) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(payload)

    def _send_quietly(self, body: str, status: int = 200) -> None:
        """Send a page, tolerating a client that already gave up."""

        try:
            self._send(body, status=status)
        except _DISCONNECTS:
            pass

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        parsed = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        try:
            if parsed.path in ("/", "/index.html"):
                self._send_quietly(self._dashboard())
            elif parsed.path == "/scenario":
                self._send_quietly(_scenario_html(query.get("s", "")))
            elif parsed.path == "/run":
                self._send_quietly(self._run(query))
            elif parsed.path == "/health":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok":true}')
            else:
                self._send_quietly(
                    _page("Not found", "<h1>404</h1><p><a href='/'>back</a></p>"),
                    status=404,
                )
        except _DISCONNECTS:
            # The browser closed the tab, reloaded, or hit its own timeout while
            # a model call was in flight.  Expected during long runs.
            pass
        except Exception as exc:  # noqa: BLE001 - a broken page must still be a page
            self._send_quietly(
                _page("Error", f'<div class="err">{_esc(type(exc).__name__)}: {_esc(exc)}</div>'),
                status=500,
            )

    def _dashboard(self, error: str = "") -> str:
        with self._lock:
            results = self._results
        try:
            runtimes = describe_runtimes()
        except Exception as exc:  # noqa: BLE001
            runtimes = {"error": {"available": False, "reason": str(exc)}}
        return _dashboard_html(results, runtimes, error)

    def _run(self, query: dict) -> str:
        repo = query.get("repo", "").strip()
        sha = query.get("sha", "").strip()
        evidence = query.get("evidence", "").strip()
        runtime = query.get("runtime", "local").strip() or "local"

        missing = [n for n, v in (("repo", repo), ("sha", sha), ("evidence", evidence)) if not v]
        if missing:
            return self._dashboard("missing required field(s): " + ", ".join(missing))

        holder: dict = {}

        def work() -> None:
            try:
                report = analyze(repo=repo, sha=sha, evidence=evidence, runtime=runtime)
                holder["data"] = _summarise(report)
            except (SafiRcaError, FileNotFoundError, ValueError, OSError) as exc:
                holder["error"] = f"{type(exc).__name__}: {exc}"
            except Exception as exc:  # noqa: BLE001
                holder["error"] = f"{type(exc).__name__}: {exc}"

        worker = threading.Thread(target=work, daemon=True)
        worker.start()
        worker.join(RUN_TIMEOUT_SECONDS)
        if worker.is_alive():
            return self._dashboard(f"analysis still running after {RUN_TIMEOUT_SECONDS}s; try again")

        if "error" in holder:
            return self._dashboard(holder["error"])
        data = holder.get("data")
        if not data:
            return self._dashboard("analysis produced no report")
        return _report_html(f"{Path(repo).name} @ {data.get('sha', '')[:9]}", data)


def serve(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, warm: bool = False) -> int:
    """Run the dashboard until interrupted.  Returns a process exit code."""

    if warm:
        _warm_cache()
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"safi-rca dashboard on http://{host}:{port}  (ctrl-c to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
    return 0


def _warm_cache() -> None:
    """Pre-run every scenario with the local runtime for a fast first paint."""

    results: dict = {}
    for scenario in scen.SCENARIOS:
        outcome = run_scenario(scenario, "local")
        results.setdefault(scenario.key, {})["local"] = outcome
    with Handler._lock:  # noqa: SLF001 - deliberate: one shared cache
        Handler._results = results  # noqa: SLF001