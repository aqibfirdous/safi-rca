"""``safi-rca`` command line interface."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .api import analyze, describe_runtimes
from .errors import SafiRcaError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="safi-rca",
        description="Trainable, read-only root cause analyzer for one git repository at one exact sha.",
    )
    parser.add_argument("--version", action="version", version=f"safi-rca {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("analyze", help="diagnose a failure against an exact git sha")
    run.add_argument("--repo", required=True, help="path to the git repository to analyse")
    run.add_argument("--sha", required=True, help="exact git commit sha to analyse (never a branch name)")
    run.add_argument("--evidence", required=True, help="file holding the failure evidence")
    run.add_argument("--json-out", help="write the JSON report to this path")
    run.add_argument("--text-out", help="write the human readable report to this path")
    run.add_argument("--json", action="store_true", help="print JSON on stdout instead of the text report")
    run.add_argument(
        "--runtime",
        default="auto",
        choices=["auto", "openhands", "local"],
        help="agent runtime; auto uses OpenHands when usable and the local engine otherwise",
    )
    run.add_argument("--map-tokens", type=int, default=4096, help="budget for the Aider RepoMap")
    run.add_argument("--keep-export", action="store_true", help="keep the temporary export of the analysed commit")

    info = sub.add_parser("runtimes", help="show which agent runtimes are usable")
    info.set_defaults(_info=True)

    serve = sub.add_parser("serve", help="run the local dashboard (stdlib only, loopback by default)")
    serve.add_argument("--host", default="127.0.0.1", help="bind address; default is loopback only")
    serve.add_argument("--port", type=int, default=8765, help="bind port")
    serve.add_argument("--warm", action="store_true", help="pre-run scenarios so the first page is fast")
    serve.set_defaults(_serve=True)
    return parser


def _cmd_analyze(args: argparse.Namespace) -> int:
    report = analyze(
        repo=args.repo,
        sha=args.sha,
        evidence=args.evidence,
        runtime=args.runtime,
        map_tokens=args.map_tokens,
        keep_export=args.keep_export,
    )
    text = report.render()
    payload = report.to_json()

    if args.json_out:
        Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_out).write_text(payload, encoding="utf-8")
    if args.text_out:
        Path(args.text_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.text_out).write_text(text, encoding="utf-8")

    if args.json:
        sys.stdout.write(payload + "\n")
    else:
        sys.stdout.write(text + "\n")
    return 0


def _cmd_runtimes(_args: argparse.Namespace) -> int:
    print(json.dumps(describe_runtimes(), indent=2))
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    from .web import serve  # noqa: PLC0415 - optional surface, imported on demand

    return serve(host=args.host, port=args.port, warm=args.warm)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):  # pragma: no cover - non-tty streams
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if getattr(args, "_serve", False):
            return _cmd_serve(args)
        if getattr(args, "_info", False):
            return _cmd_runtimes(args)
        return _cmd_analyze(args)
    except SafiRcaError as exc:
        print(f"safi-rca: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"safi-rca: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
