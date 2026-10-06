"""Command-line entry points (``aiops``, ``aiops-backlog``).

Declared in ``pyproject.toml`` so the operator-facing workflows (run the API, apply migrations,
verify a release, run the rollback drill, export the backlog) have one documented surface
instead of a collection of shell one-liners. Each command delegates to the module that owns the
behaviour — this file contains no logic of its own beyond argument wiring.

Usage::

    aiops serve --host 0.0.0.0 --port 8000
    aiops migrate
    aiops verify-release --base-url http://localhost:8000
    aiops backlog export
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # pragma: no cover - defensive when run as a script
    sys.path.insert(0, str(REPO_ROOT))


def _serve(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=args.port,
        workers=args.workers,
        reload=args.reload,
        log_level=os.environ.get("AIOPS_LOG_LEVEL", "info").lower(),
    )
    return 0


def _migrate(args: argparse.Namespace) -> int:
    from alembic import command
    from alembic.config import Config

    config = Config(str(REPO_ROOT / "app" / "persistence" / "alembic.ini"))
    command.upgrade(config, args.revision)
    return 0


def _verify_release(args: argparse.Namespace) -> int:
    from scripts.verify_release import main as verify_main

    return int(
        verify_main(
            [
                "--base-url",
                args.base_url,
                *(["--json", args.json] if args.json else []),
            ]
        )
    )


def _rollback_drill(args: argparse.Namespace) -> int:
    from scripts.rollback_drill import main as drill_main

    return int(drill_main(["--json"] if args.json else []))


def _export_backlog(args: argparse.Namespace) -> int:
    from scripts.export_backlog import main as export_main

    argv: list[str] = []
    if args.csv:
        argv += ["--csv", args.csv]
    if args.markdown:
        argv += ["--markdown", args.markdown]
    return int(export_main(argv))


def _evaluate(args: argparse.Namespace) -> int:
    from evals.runner import main as eval_main

    argv = ["--config", args.config]
    if args.check_regression:
        argv.append("--check-regression")
    if args.update_baseline:
        argv.append("--update-baseline")
    return int(eval_main(argv))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aiops", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the API")
    serve.add_argument("--host", default="0.0.0.0")  # noqa: S104 - containers bind all interfaces
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--workers", type=int, default=1)
    serve.add_argument("--reload", action="store_true")
    serve.set_defaults(func=_serve)

    migrate = sub.add_parser("migrate", help="apply database migrations")
    migrate.add_argument("--revision", default="head")
    migrate.set_defaults(func=_migrate)

    verify = sub.add_parser("verify-release", help="run the release verification (DEV-003)")
    verify.add_argument("--base-url", default="http://localhost:8000")
    verify.add_argument("--json", default=None, help="write the report to this path")
    verify.set_defaults(func=_verify_release)

    drill = sub.add_parser("rollback-drill", help="prove the rollback procedure (DEV-004)")
    drill.add_argument("--json", default=None)
    drill.set_defaults(func=_rollback_drill)

    backlog = sub.add_parser("backlog", help="backlog tooling")
    backlog_sub = backlog.add_subparsers(dest="backlog_command", required=True)
    export = backlog_sub.add_parser("export", help="regenerate the CSV and sprint board")
    export.add_argument("--csv", default=None)
    export.add_argument("--markdown", default=None)
    export.set_defaults(func=_export_backlog)

    evaluate = sub.add_parser("eval", help="run the AI evaluation harness (EVAL-002)")
    evaluate.add_argument("--config", default="evals/config.yaml")
    evaluate.add_argument("--check-regression", action="store_true")
    evaluate.add_argument("--update-baseline", action="store_true")
    evaluate.set_defaults(func=_evaluate)

    return parser


def main(argv: list[str] | None = None) -> int:
    args: Any = build_parser().parse_args(argv)
    return int(args.func(args))


def backlog_main(argv: list[str] | None = None) -> int:
    """``aiops-backlog`` — export the backlog without the ``backlog`` subcommand."""
    return int(_export_backlog(build_parser().parse_args(["backlog", "export", *(argv or [])])))


if __name__ == "__main__":  # pragma: no cover - thin wrapper
    raise SystemExit(main())


__all__ = ["backlog_main", "build_parser", "main"]
