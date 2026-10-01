# Handoff — end of session

Everything below is committed. `git status` is clean, both fixture repositories are
clean, and `python -m pytest` passes 13/13 from a cold start.

Last commit: `796a94e safi-rca: trainable read-only RCA analyzer over OpenHands + Aider RepoMap`

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

RepoMap is produced by real `aider 0.16.0` in every run. Runtimes report:

```
openhands           available: false  (cannot import openhands.sdk / openhands.core)
local-deterministic available: true
```

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

- Python `3.14.5`, git `2.55.0.windows.2`, `aider-chat 0.16.0`, `tree-sitter 0.26.0`,
  `pytest 9.1.1`. System Python / user-site is the working runtime. No `.venv`.
- `aider-chat==0.16.0` pins `numpy==1.24.3`, which has no CPython 3.14 wheel. On 3.14
  it must be installed `--no-deps` plus its pure-Python requirements.
- Aider 0.16 uses the pre-0.25 tree-sitter query API. `safi_rca/tree_sitter_compat.py`
  adapts `QueryCursor`; that shim is what makes the real Aider RepoMap work. Do not
  remove it.
- Shell is `cmd.exe`: use `&&`, not `;`; `rmdir` not `rm`; `python -m ...` for modules.
  Pipelines like `... | head` do not exist; use `findstr` or redirect to a file.
- Scratch exports go to `B:\safi-rca-tmp` by default (same drive as the repo), or
  `$SAFI_RCA_TMP_DIR`. Nothing is written to `C:`.
- The two fixtures are submodules pointing at bare mirrors in `fixtures/origin/`, so
  `git submodule update --init` restores them with no network.

## Honest limitations

1. **OpenHands has never run.** The adapter is implemented and guarded, but
   `openhands-ai` cannot install on Python 3.14 (e2b dependency conflict) and there are
   no model credentials in this environment. Every result so far comes from the local
   deterministic engine. The README says this in the Environment notes section.
2. `read_only.clean` proves the analyzer changed nothing in the repository. It does not
   prove an agent runtime would behave — that needs a live OpenHands run.
3. The two-level analysis (one hop from the failing test, then the producing function)
   is tuned for the demo fixtures. Deeper call chains will fall back to
   `deepest-failing-frame`, which is honest but less precise.

## If continuing tomorrow

Reasonable next steps, in order of value:

1. Get OpenHands installed on a Python it supports (3.11/3.12 in a venv) and run the
   five scenarios through `--runtime openhands` to exercise the real agent path.
2. Widen the one-hop resolution into a bounded call-graph walk so deeper defects get a
   named component instead of `deepest-failing-frame`.
3. Add detectors for the failure classes the fixtures do not cover (exception swallowing,
   off-by-one in loops, resource leaks on error paths).
