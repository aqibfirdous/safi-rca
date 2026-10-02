# Handoff — end of session

Everything below is committed. `git status` is clean, both fixture repositories are
clean, and `python -m pytest` passes 18/18 from a cold start.

## State: feature complete against the frozen design

The product works end to end. All five demo scenarios diagnose correctly, the
acceptance suite passes, and nothing in this repository or its fixtures was modified
by the analyzer at any point.

| scenario | detector | confidence |
| --- | --- | --- |
| sample-repo `467b3d8` | `none-dereference-on-optional-lookup` | High |
| sample-repo `ad23862` | `unnormalised-numeric-precision` | Medium |
| `ad23862` + SHA-A evidence | `evidence-not-reproducible-at-sha` | Low |
| alt-repo `f3ab813` | `unclamped-counter-index` | Medium |
| uncertain evidence | `insufficient-evidence` | Low |

RepoMap is produced by real `aider 0.16.0` in every run.

## What changed this session: OpenHands

The previous handoff said the OpenHands runtime "has never run" because
`openhands-ai` would not install on Python 3.14. That was a wrong conclusion drawn
from the wrong package. `openhands-sdk` 1.50.1 installs cleanly on 3.14.

1. **`openhands-sdk` is now installed** and the adapter resolves the real
   `openhands.sdk` backend. `safi-rca runtimes` now reports
   `openhands present (openhands.sdk) but no model credentials` instead of
   "cannot import".
2. **The adapter was rewritten against the actual API.** Every call the old adapter
   made was wrong: it used `Conversation(llm=...)`, `sdk.Llm`, `conversation.add_tool()`,
   and invented tool classes (`ReadOnlyBashTool`, `ReadFileTool`, `GrepTool`, `GlobTool`)
   that do not exist. The real path is
   `OpenHandsAgentSettings(...).create_agent()` → `LocalWorkspace(working_dir=...)` →
   `LocalConversation(agent=, workspace=)`, and the reply is the last `MessageEvent`
   with `source == "agent"`.
3. **Read-only is now structural.** The agent is granted exactly `ThinkTool` and
   `FinishTool`. Neither writes; the SDK ships no shell or file-write tool. So the
   agent cannot mutate the tree even if instructed to. No browsing tools are needed
   because the RepoMap, source slices and training are already in the prompt.
4. **Banner suppression.** `openhands.sdk` prints an ASCII banner on import, which
   corrupted `--json` and `runtimes`. The adapter now sets
   `OPENHANDS_SUPPRESS_BANNER=1` before the first import.

### New file: `safi_rca/openai_compat.py`

Installing `openhands-sdk` upgraded `openai` 0.27 → 2.54 and **broke Aider**
(acceptance test 2 failed: `module 'openai' has no attribute 'api_base'`). The two
packages pin incompatible `openai` versions and cannot be resolved together:

- `aider-chat==0.16.0` → `openai==0.27.6`
- `openhands-sdk` → `openai>=2.20`

Aider reads `openai.api_base` at module import (`aider/models/model.py:22`), so
`import aider.repomap` fails outright. The shim restores that one attribute before
Aider is imported — same pattern as the existing `tree_sitter_compat.py`. Guarded by
`test_openai_shim_keeps_the_real_aider_repomap_working`.

### The live model call now runs

**The model call itself has been exercised.** Verified against
`gemini/gemini-3-flash-preview`:

```
set OPENHANDS_SUPPRESS_BANNER=1
set GEMINI_API_KEY=<key>
set LLM_MODEL=gemini/gemini-3-flash-preview
python -m safi_rca analyze --repo fixtures\sample-repo ^
    --sha 467b3d80a3a2c7bd0cecd81e44838bb3ff5fdc0c ^
    --evidence evidence\pytest_failure.txt --runtime openhands
```

It returns the correct root cause (`PaymentValidator._lookup_plan` returns `None`
for the unmapped `gold` tier because the `DEFAULT_PLAN` fallback required by
marker `PLAN-RESOLUTION-1847` is missing), cites the real failing test and the
real code sites with line numbers, and reports `read_only.clean: true` with
`files_changed: []`. `repomap.producer` is `aider 0.16.0`, 11/11 files mapped.

Note the credential is required per-run in the environment and must never be
written to a file, a fixture, or a commit.

### Four bugs the live run exposed, all now fixed and regression-tested

These did not surface in tests, because tests used fakes and a live run does not.

1. **Duplicate tools crashed agent construction.** Passing the read-only tools via
   `tools=` *duplicates* the ones `create_agent()` already injects as defaults, so
   the SDK raised `Duplicate tool names found: {'finish', 'think'}`. The agent is
   now built with `tools=[]` and the effective set — explicit specs plus
   `include_default_tools` — is asserted to be exactly `ThinkTool` and
   `FinishTool`, so a future SDK default that can write fails loudly.
2. **The agent answers in two different shapes.** Sometimes it calls the `finish`
   tool, in which case the text is on the *action* and the observation is
   deliberately empty; sometimes it just returns assistant text. The extractor
   only handled one, so correct runs were reported as
   `RuntimeUnavailable: no assistant message`. Both are read now.
3. **`content_to_str` returns `list[str]`.** It is a display helper, not an
   extractor, and it substitutes `[Image: N URLs]` for image parts. Reading
   `TextContent.text` directly is both correct and keeps a placeholder out of a
   report we are about to parse as JSON.
4. **`report.repamap` was a typo for `report.repomap`.** `RcaReport` defaults that
   field to `{}`, so the mistyped assignment created a stray attribute and the
   report shipped with *no repo-map provenance at all* while still looking
   successful. `test_parsed_report_carries_real_provenance` now asserts the
   producer is named and files were actually mapped.

### OpenRouter

`OPENROUTER_API_KEY` is now a supported credential and the model is routed by the
prefix LiteLLM uses (`openrouter/anthropic/claude-sonnet-4.5`). A credential whose
provider disagrees with the model prefix is reported by `safi-rca runtimes` rather
than failing later as an opaque auth error inside LiteLLM. Verified at
construction level; no live OpenRouter call was made.

## Pinned shas — do not rewrite these fixtures without updating the tests

| repository | sha | what it demonstrates |
| --- | --- | --- |
| fixtures/sample-repo | `467b3d80a3a2c7bd0cecd81e44838bb3ff5fdc0c` | tier lookup with no `DEFAULT_PLAN` fallback |
| fixtures/sample-repo | `ad23862538978a8f1e39a251c6882e9b1d29839a` | fallback added, Decimal-scale defect remains |
| fixtures/alt-repo | `f3ab813dcd44e258451682b0da624b4840d56092` | ring-buffer drain order after wrap |

`tests/conftest.py` asserts all three at session start, so a rewritten fixture fails the
suite loudly instead of silently testing something else. Training markers:
`PLAN-RESOLUTION-1847` (sample), `BATCH-DRAIN-77` (alt).

## Environment facts that cost time to discover

- Python `3.14.5` only (no `py` launcher, no other interpreters), git `2.55.0.windows.2`,
  `aider-chat 0.16.0`, `openhands-sdk 1.50.1`, `tree-sitter 0.26.0`, `pytest 9.1.1`.
  System Python / user-site is the working runtime. No `.venv`.
- `aider-chat==0.16.0` pins `numpy==1.24.3`, which has no CPython 3.14 wheel. On 3.14
  it must be installed `--no-deps` plus its pure-Python requirements. Its many other
  pins (`openai`, `tiktoken`, `numpy`, …) are all violated; only two shims matter,
  `tree_sitter_compat.py` and `openai_compat.py`. Do not remove either.
- Shell is `cmd.exe`: use `&&`, not `;`; `rmdir` not `rm`; `python -m ...` for modules.
  Pipelines like `... | head` do not exist; use `findstr` or redirect to a file.
  **The `grep` tool is broken here** — it shells out to `powershell.exe`, which is not
  installed. Use `read`, or `findstr` from `cmd`.
- Scratch exports go to `B:\safi-rca-tmp` by default (same drive as the repo), or
  `$SAFI_RCA_TMP_DIR`. Nothing is written to `C:`.
- The two fixtures are submodules pointing at bare mirrors in `fixtures/origin/`, so
  `git submodule update --init` restores them with no network.
- `pip install` prints a long list of `aider-chat` dependency conflicts. They are
  expected; do not "fix" them by downgrading openai, that breaks the agent runtime.

## Other honest limitations

1. The two-level analysis (one hop from the failing test, then the producing function)
   is tuned for the demo fixtures. Deeper call chains fall back to
   `deepest-failing-frame`, which is honest but less precise.
2. `read_only.clean` proves nothing was changed during the run. Combined with the
   tool allowlist this is now backed by a live OpenHands run, but it is a
   before/after fingerprint, not a syscall-level sandbox.
3. The OpenHands system prompt is the SDK's default, which describes an agent that
   may execute commands and edit code. No such tool is granted, and the RCA
   instruction is appended to the user prompt, so the conflict is currently
   neutralised by the allowlist rather than by the prompt itself.

## If continuing tomorrow

1. Run the remaining four demo scenarios through `--runtime openhands`; only the
   deliberately-broken `sample-repo` sha has been exercised live.
2. Override the OpenHands system prompt via `AgentContext.system_message_suffix`
   instead of relying on the tool allowlist to neutralise it.
3. Widen the one-hop resolution into a bounded call-graph walk so deeper defects get a
   named component instead of `deepest-failing-frame`.
4. Add detectors for failure classes the fixtures do not cover (exception swallowing,
   off-by-one in loops, resource leaks on error paths).