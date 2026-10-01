# safi-rca

A trainable, **read-only**, repo-aware root cause analyzer.

Given a git repository, **one exact commit sha**, and a file of failure evidence
(pytest output, a traceback, a build log, an HTTP error dump), `safi-rca` returns a
structured root cause report: the symptom, the cause, the affected component, the
reasoning, concrete `file:line` evidence, confidence, and what it does *not* know.

It never edits the repository, never moves `HEAD`, and never diagnoses a branch.

---

## Architecture (fixed)

```
safi-rca
├── Agent Runtime          OpenHands  ── falls back to the local deterministic engine
│                           (read-only workspace = an immutable export of one commit)
├── RepoMap                Aider RepoMap over the exported tree (tiered structure map)
├── Repository training    AGENTS.md / CLAUDE.md / .agents/skills/*.md at the analysed sha
├── Role training          .agents/skills/root-cause-analyzer.md (the RCA role itself)
├── Exact sha              git archive export; no checkout, no worktree, no ref writes
├── Failure evidence       parsed into frames, tests, assertions, log lines, statuses
└── Read-only guard        HEAD + status + content fingerprint before and after
```

The two integrations are honest about themselves: the JSON report always names the
RepoMap producer that ran and the runtime that ran, and an unavailable runtime
states why.

## Requirements

- Python >= 3.11 (developed and verified on 3.14)
- `git` on `PATH` (2.55 used here)
- Optional: `aider-chat` for the real Aider RepoMap, `openhands-ai` for the agent runtime

```bat
pip install -e .                    :: core (standard library only)
pip install -e ".[repomap]"         :: + Aider RepoMap
pip install -e ".[agent]"           :: + OpenHands runtime
pip install -e ".[test]"            :: + pytest
```

If Aider is not importable, `safi-rca` falls back to an AST-based structural map and
reports `producer: "ast-fallback"`. Nothing else changes.

## Usage

```bat
safi-rca analyze --repo fixtures\sample-repo ^
                 --sha 467b3d80a3a2c7bd0cecd81e44838bb3ff5fdc0c ^
                 --evidence evidence\pytest_failure.txt

safi-rca analyze --repo R --sha <40-hex> --evidence E --json --json-out report.json
safi-rca runtimes
python -m safi_rca --help
```

| flag | meaning |
| --- | --- |
| `--sha` | exact commit id. Branch names, `HEAD`, tags and ranges are rejected. |
| `--evidence` | file of failure output to diagnose. |
| `--runtime` | `auto` (default), `openhands`, or `local`. |
| `--map-tokens` | RepoMap budget, default 4096. |
| `--json` / `--json-out` / `--text-out` | machine and human report forms. |
| `--keep-export` | keep the temporary export of the analysed commit. |

Programmatic use:

```python
from safi_rca.api import analyze
report = analyze(repo="fixtures/sample-repo", sha="467b3d8", evidence="evidence/pytest_failure.txt")
print(report.root_cause)
```

## Training

**Repository training** is read from the analysed commit, not from the work tree, so a
diagnosis can never rest on text that does not exist in that version. Recognised
files: `AGENTS.md`, `CLAUDE.md`, `GEMINI.md`, `docs/agents/*.md`,
`.github/copilot-instructions.md`, `.agents/skills/*.md`. Facts mentioning the failing
component, or carrying a marker such as `PLAN-RESOLUTION-1847`, are selected and cited
in the report as `training` evidence.

**Role training** (`.agents/skills/root-cause-analyzer.md`) is the RCA role itself and is
resolved in this order: a copy pinned in the repository at that sha, then
`$SAFI_RCA_ROLE_SKILL`, then the copy shipped with safi-rca, then a built-in minimum.
It is parsed into sections and recorded in the report, so it is auditable.

To train a new repository, add training to it and re-run; no safi-rca change is needed.

## Read-only guarantee

1. `ReadOnlyGuard` fingerprints `HEAD`, `git status --porcelain` and the content hash of
   every dirty/untracked file before analysis.
2. The commit is materialised with `git archive` into a scratch directory on the same
   drive as the repository (`B:\safi-rca-tmp`, or `$SAFI_RCA_TMP_DIR`). No checkout is
   performed, so the working tree, index, `HEAD` and refs cannot be touched.
3. The fingerprint is taken again afterwards. The report carries
   `read_only.clean`, both `HEAD` values, and the list of changed files.

`safi-rca` recommends; it does not apply fixes.

## Exact-sha scoping

Every report is stamped with the analysed sha and says so in prose:

> This diagnosis applies to `<sha>` only. It is not valid for any other commit.

When the evidence cannot occur at the analysed sha, the analyzer says *that* instead of
inventing a cause (`detector: evidence-not-reproducible-at-sha`).

## Detectors

Each report names the reasoning that produced it in `detectors_fired`.

| detector | recognises |
| --- | --- |
| `none-dereference-on-optional-lookup` | a lookup that can miss whose `None` result is dereferenced, no guard |
| `evidence-not-reproducible-at-sha` | the reported `None`/assertion cannot be produced at the analysed sha |
| `unnormalised-numeric-precision` | decimal arithmetic whose scale contradicts the asserted value |
| `unclamped-counter-index` | a growing counter used as a bound into a fixed-length container |
| `deepest-failing-frame` | honest fallback: deepest repository frame, cause not ranked |
| `insufficient-evidence` | no code site in the evidence; refuses to name a cause |

## Tests

```bat
python -m pytest
```

`tests/test_acceptance.py` is the acceptance suite: one test per acceptance criterion
(exact sha, real Aider RepoMap, repository training markers, role training, symptom vs
cause, known root cause, evidence-backed claims, read-only, sha sensitivity, second
repository plus honest uncertainty) plus CLI and runtime checks. Every source citation
is re-read from the exported tree of the analysed sha and matched against its quote, so
a citation that does not exist — or does not say what it claims — fails the suite.

## Layout

```
safi_rca/            package
  api.py             analyze(); orchestration, sha protection, scratch dir
  cli.py             argparse CLI
  gitutil.py         read-only git plumbing; git archive export
  readonly.py        before/after fingerprint and verdict
  training.py        repository + role training loading, selection, marker extraction
  repomap.py         Aider RepoMap with AST fallback
  tree_sitter_compat.py  shim for Aider 0.16 on tree-sitter >= 0.25
  evidence.py        failure evidence parser
  symbols.py         AST symbol table, call graph, nullability, arithmetic, indexing
  analyst.py         detectors, site resolution, symptom/cause separation
  report.py          JSON contract and human rendering
  context.py         assembles training + repomap + source slices
  runtime/           agent runtimes: openhands_runtime, local_runtime
.agents/skills/root-cause-analyzer.md   role training
fixtures/sample-repo/                  demo repository, two shas, AGENTS.md
fixtures/alt-repo/                     second repository, different failure class
evidence/                              sample failure evidence
tests/                                 acceptance suite
```

## Environment notes

- Aider 0.16 calls the pre-0.25 tree-sitter query API; `tree_sitter_compat.py` adapts
  `QueryCursor` so the real Aider RepoMap works on modern tree-sitter.
- `aider-chat==0.16.0` pins `numpy==1.24.3`, which has no wheel for CPython 3.14. On
  3.14, install Aider with `--no-deps` (plus its pure-Python requirements) or use an
  older interpreter; the RepoMap itself needs only `aider` and tree-sitter.
- The OpenHands runtime requires the `openhands-ai` package and model credentials
  (`LLM_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, or `SAFI_RCA_LLM_API_KEY`).
  In this environment OpenHands cannot be installed, so `safi-rca runtimes` reports it
  as unavailable and `--runtime auto` uses the local deterministic engine. The adapter
  is implemented and guarded; it has not been exercised against a live model.
