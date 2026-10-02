# Handoff — end of session

Everything below is committed. `git status` is clean, both fixture repositories are
clean, and `python -m pytest` passes 15/15 from a cold start.

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

### What is still not proven

**The model call itself has not run.** No API key is available in this environment.
Everything up to and including agent construction is verified; the LLM round trip and
JSON parsing of the reply are not. To close it:

```bat
set ANTHROPIC_API_KEY=sk-...
python -m safi_rca analyze --repo fixtures\sample-repo ^
    --sha 467b3d80a3a2c7bd0cecd81e44838bb3ff5fdc0c ^
    --evidence evidence\pytest_failure.txt --runtime openhands
```

Also still unproven: that an agent runtime behaves read-only in practice. The tool
wiring makes mutation impossible, but that has not been observed on a live run.

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
2. `read_only.clean` proves *safi-rca* changed nothing. It does not prove an agent
   runtime would behave — that needs a live OpenHands run.

## If continuing tomorrow

1. Supply a model credential and run the five scenarios through
   `--runtime openhands`. This is the last open item on Definition of Done #1.
2. Add a test that feeds a recorded model reply through `_parse_report`, so the JSON
   contract is covered even without a live key.
3. Widen the one-hop resolution into a bounded call-graph walk so deeper defects get a
   named component instead of `deepest-failing-frame`.
4. Add detectors for failure classes the fixtures do not cover (exception swallowing,
   off-by-one in loops, resource leaks on error paths).