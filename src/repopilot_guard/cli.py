"""安全优先的 RepoPilot Guard 骨架 CLI 入口。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from repopilot_guard.coordinator import TaskCoordinator
from repopilot_guard.models import TaskRequest, default_output_root
from repopilot_guard.preflight import PreflightInspector


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="repopilot-guard", description="Safe repository maintenance agent skeleton")
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser("inspect", help="Run read-only Java/Maven preflight checks")
    inspect_parser.add_argument("--repo", required=True, type=Path, help="Path to the target Git repository")

    run_parser = subparsers.add_parser("run", help="Run the current read-only dry-run lifecycle")
    run_parser.add_argument("--repo", required=True, type=Path, help="Path to the target Git repository")
    run_parser.add_argument("--task", required=True, help="Bug description or small change request")
    run_parser.add_argument("--output", type=Path, default=default_output_root(), help="Directory for evidence artifacts")
    run_parser.add_argument("--max-steps", type=int, default=12, help="Maximum future agent steps")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "inspect":
        result = PreflightInspector().inspect(args.repo)
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        return 0 if result.ready else 2

    request = TaskRequest(
        repository=args.repo,
        description=args.task,
        output_root=args.output,
        max_steps=args.max_steps,
    )
    result = TaskCoordinator().run_dry_run(request)
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0 if result.verdict.value == "UNVERIFIED" else 2
