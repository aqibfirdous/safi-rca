# Handoff — end of session

Everything below is committed and pushed. `git status` is clean, `origin/main` and
`main` are identical, both fixture repositories are clean, and `python -m pytest`
passes **57/57** from a cold start.

Remote: `https://github.com/aqibfirdous/safi-rca` (branch `main`).

## State: feature complete against the frozen design

All five demo scenarios diagnose correctly with the local runtime. RepoMap is
produced by real `aider 0.16.0` in every run. Nothing in this repository or its
fixtures has been modified by the analyzer at any point.

| scenario | detector | confidence |
| --- | --- | --- |
| sample-repo `467b3d8` | `none-dereference-on-optional-lookup` | High |
| sample-repo `ad23862` | `unnormalised-numeric-precision` | Medium |
| `ad23862` + SHA-A evidence | `evidence-not-reproducible-at-sha` | Low |
| alt-repo `f3ab813` | `unclamped-counter-index` | Medium |
| uncertain evidence | `insufficient-evidence` | Low |

## Blocked: the model runtime needs an OpenRouter balance

`OPENROUTER_API_KEY` is configured and valid — it is an OpenRouter key
(`sk-or-v1-`, 73 chars), **not** an OpenAI key; both prefixes start with `sk`.
But `GET https://openrouter.ai/api/v1/credits` reports `total_credits: 0`, so
every call is rejected before any token is used:

```
This request requires more credits, or fewer max_tokens.
You requested up to 64000 tokens, but can only afford less
```

A `:free` OpenRouter model (`qwen/qwen3.8-27b:free`) is also rate-limited right
now, so there is no model-runtime path until credits are added at
<https://openrouter.ai/credits>. The **local runtime is unaffected and needs no
account** — that is the working path today.

The previous session's Gemini key (`gemini/gemini-3-flash-preview`) hit its
free-tier cap of 20 requests/day and was spent during this session.

## Both runtimes, live, side by side

All five scenarios were run through the live model (`gemini/gemini-3-flash-preview`)
before the quota ran out. This is the most important finding in the repo:

| scenario | local | openhands |
| --- | --- | --- |
| broken sha, traced error | correct | correct |
| precision defect | correct (Medium) | right class, wrong literal (High) |
| stale evidence | correct (Low) | invented a cause (High) |
| alt-repo drain order | correct | correct |
| vague evidence | correct (Low) | invented a cause (Medium) |

**The two "say you don't know" scenarios are both wrong for the model runtime.**
It manufactures a plausible defect instead of reporting that the evidence cannot
occur at the analysed sha, and it claims High confidence while doing so. This is
the gap the dashboard was built to make visible.

On the `precision` scenario it named the right defect class but invented a
literal `Decimal('1.0000')` that does not appear in the source; the real cause is
Decimal scale expansion in `amount * (Decimal("1") - rate)`.

It also misquoted its own evidence: `_lookup_plan` cited at line 48 when it is
line 47, and it asserted gold was missing from `DISCOUNT_PLANS` while quoting only
`DISCOUNT_PLANS = {`, which proves nothing. Trust its conclusions more than its
line numbers until this is addressed.

## Dashboard: `python -m safi_rca serve`

Stdlib-only (`http.server`), loopback `127.0.0.1:8765`, no new dependency —
this repo already juggles a pinned `aider-chat` against `openhands-sdk` and a
third conflict is not worth a nicer page. Adds `--warm` and `--port`.

- Lists all five scenarios with the pinned sha, click any for a full report.
- Shows both runtimes and why they are or are not available.
- A form to analyse your own repo / sha / evidence file.
- Analyses run on a worker thread with a timeout so a wedged model call cannot
  hang a request.

`--warm` currently pre-runs only the **local** runtime; `/scenario?s=<key>` runs
both on demand. The scenario table lives in `safi_rca/scenarios.py` and is read by
both the acceptance suite and the dashboard, so the UI cannot drift from what the
tests prove.

## Credentials: `.env`, gitignored

Credentials were previously passed inline, putting them in shell history. Now:

- `.env` is gitignored and never committed; `.env.example` is committed.
- Real environment variables **win** over the file, so `set KEY=...` still
  overrides.
- `SAFI_RCA_NO_DOTENV=1` disables loading, and the test suite sets it so a test
  run can never start a billable model call.
- `safi_rca/dotenv.py` is a ~40-line stdlib loader. `python-dotenv` would also
  work but adds a dependency to save thirty lines.

`.env` currently holds both `GEMINI_API_KEY` (spent) and `OPENROUTER_API_KEY`
(empty balance), with `LLM_MODEL=openrouter/anthropic/claude-sonnet-4.5`.

**Both keys have appeared in this session's shell history and transcripts.
Treat them as compromised and rotate.**

## Bugs found and fixed this session

1. **`SAFI_RCA_TMP_DIR` trailing space.** On cmd.exe, `set VAR=value && cmd`
   bakes the space before `&&` into the value, producing
   `B:\repo\.tmp \safi-rca-x` and a bare `FileNotFoundError: [WinError 3]` naming
   a path nobody typed. Now stripped. Same class as the
   `OPENHANDS_SUPPRESS_BANNER` bug from `27c8a69`.
2. **Client disconnect reported as a server fault.** `do_GET` caught the
   disconnect in its generic `except Exception`, then tried to send a 500 page to
   the socket that had just died, which raised again and escaped to
   `socketserver`. `Handler.handle()` now catches both the request read and the
   response write. Verified by replay: 6 aborted requests produced 6 tracebacks
   before, 0 after.
3. **Spent quota was unreadable.** Four litellm retries and ~90s of backoff, then
   a JSON blob naming an internal Google metric. `retry_max_wait` is now 8s and a
   quota rejection is reported as one, with the model, the reset time, and two
   actions. A credit rejection is reported separately — the key works, the
   balance does not.
4. **Switching provider was impossible.** `_credentials()` walked `LLM_ENV_KEYS`
   in declaration order, so with `GEMINI_API_KEY` and `OPENROUTER_API_KEY` both
   set it returned the Gemini key for *every* model. A litellm model name carries
   its provider as a prefix, so the credential is now resolved from the requested
   model; the fixed order is only the fallback.
5. **`max_output_tokens` was 64000.** OpenRouter pre-authorises against
   `max_tokens`; on a $15/Mtok model that asks for ~$1 for a reply that is one
   JSON report. Bounded to 4096, env-overridable via
   `SAFI_RCA_MAX_OUTPUT_TOKENS`.
6. **Malformed model replies** (literal newlines in strings, greedy braces,
   Markdown headings) are parsed or re-asked rather than discarded — see
   `c8ac21b`.

## Pinned shas — do not rewrite these fixtures without updating the tests

| repository | sha | what it demonstrates |
| --- | --- | --- |
| fixtures/sample-repo | `467b3d80a3a2c7bd0cecd81e44838bb3ff5fdc0c` | tier lookup with no `DEFAULT_PLAN` fallback |
| fixtures/sample-repo | `ad23862538978a8f1e39a251c6882e9b1d29839a` | fallback added, Decimal-scale defect remains |
| fixtures/alt-repo | `f3ab813dcd44e258451682b0da624b4840d56092` | ring-buffer drain order after wrap |

`tests/conftest.py` asserts all three at session start. Training markers:
`PLAN-RESOLUTION-1847` (sample), `BATCH-DRAIN-77` (alt).

## Environment facts that cost time to discover

- Python `3.14.5` only (no `py` launcher, no other interpreters), git
  `2.55.0.windows.2`, `aider-chat 0.16.0`, `openhands-sdk 1.50.1`,
  `tree-sitter 0.26.0`, `pytest 9.1.1`. System Python / user-site is the working
  runtime. No `.venv`.
- `aider-chat==0.16.0` pins `numpy==1.24.3`, which has no CPython 3.14 wheel. On
  3.14 it must be installed `--no-deps` plus its pure-Python requirements. Its
  many other pins (`openai`, `tiktoken`, `numpy`, …) are all violated; only two
  shims matter, `tree_sitter_compat.py` and `openai_compat.py`. Do not remove
  either.
- Shell is `cmd.exe`: use `&&`, not `;`; `rmdir` not `rm`; `python -m ...` for
  modules. Pipelines like `... | head` do not exist; use `findstr` or redirect to
  a file. **The `grep` tool is broken here** — it shells out to `powershell.exe`,
  which is not installed. Use `read`, or `findstr` from `cmd`.
- `python -c "..."` frequently produces no output when the expression contains
  quotes or escapes, because `cmd` mangles them. Write a script file instead.
- Scratch exports go to `.tmp/` in the repo (`SAFI_RCA_TMP_DIR`), nothing on `C:`.
- The two fixtures are submodules pointing at bare mirrors in `fixtures/origin/`,
  so `git submodule update --init` restores them with no network.
- `pip install` prints a long list of `aider-chat` dependency conflicts. They are
  expected; do not "fix" them by downgrading openai.
- GitHub **push protection** blocks this repo on any committed string matching a
  GCP or OpenAI key pattern, including inside test fixtures. Use a synthetic key
  like `"AQ.Ab8FAKEfakefakefakefakefakefakeFAKE01"` in tests — it tripped this
  once already when a real key was pasted into `tests/test_dotenv.py`.

## Known open issues

1. **`--json` pollutes stdout.** The SDK echoes the prompt and its own
   `Tokens: ...` accounting to stdout, so `--json > out.json` captures ~32KB of
   noise before the JSON. Redirecting stderr is not enough — it is stdout. This
   breaks piping the report into `jq`. Not yet fixed.
2. **The model runtime invents causes on insufficient evidence** (table above).
   Root cause not yet investigated; candidates are the default system prompt
   (see below) and the absence of any "refuse to answer" pressure in the
   contract.
3. The two-level analysis (one hop from the failing test, then the producing
   function) is tuned for the demo fixtures. Deeper call chains fall back to
   `deepest-failing-frame`, which is honest but less precise.
4. `read_only.clean` proves nothing was changed during the run. Combined with
   the tool allowlist this is backed by a live run, but it is a before/after
   fingerprint, not a syscall-level sandbox.
5. The OpenHands system prompt is the SDK's default, which describes an agent
   that may execute commands and edit code. No such tool is granted and the RCA
   instruction is appended to the user prompt, so the conflict is currently
   neutralised by the allowlist rather than by the prompt itself.

## If continuing tomorrow

1. Add OpenRouter credits, then re-run the five scenarios through
   `--runtime openhands` on a second model family to see whether the
   invent-a-cause behaviour in issue 2 is the model or the setup.
2. Fix `--json` stdout pollution (issue 1) so reports can be piped.
3. Override the OpenHands system prompt via `AgentContext.system_message_suffix`
   instead of relying on the tool allowlist to neutralise it, then re-test issue 2.
4. Widen the one-hop resolution into a bounded call-graph walk so deeper defects
   get a named component instead of `deepest-failing-frame`.
5. Add detectors for failure classes the fixtures do not cover (exception
   swallowing, off-by-one in loops, resource leaks on error paths).