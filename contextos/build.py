"""Build mode from the terminal.

    python -m contextos.build "A CLI expense tracker with SQLite and CSV export" --workspace ./expenses

Research -> plan -> your approval -> build feature by feature (each verified by
running its tests) -> security review -> BUILD_REPORT.md. Approvals are asked
here as y/n questions. Ctrl+C stops after the current step.
"""
from __future__ import annotations

import argparse
import sys
from typing import Any, Optional

from .builder import BuildRun
from .live import load_env
from .mcp_client import Connectors
from .tools import ToolError


def _ask(q: str, default: bool = False) -> bool:
    try:
        a = input(f"  {q} [{'Y/n' if default else 'y/N'}] ").strip().lower()
    except EOFError:
        return default
    return default if not a else a.startswith("y")


def _print(ev: dict[str, Any], run: BuildRun, yes: bool) -> None:
    t = ev["type"]
    if t == "phase":
        print(f"\n== {ev['phase'].upper()}: {ev['detail']}")
    elif t == "step":
        args = ", ".join(f"{k}={str(v)[:60]}" for k, v in (ev.get("args") or {}).items()
                         if k not in ("content", "new", "old"))
        print(f"  [{ev['n']:>2}] {ev['tool']}({args})  - {ev.get('provider', '')}")
    elif t == "observation" and not ev["ok"]:
        print(f"       ! {ev['result'].splitlines()[0][:110] if ev['result'] else ''}")
    elif t == "model_failed":
        print(f"       ({ev['provider']} unavailable, resting {ev['rest']}s)")
    elif t == "research":
        print(f"\n  Research brief:\n    " + ev["brief"].replace("\n", "\n    ")[:2500])
    elif t == "plan":
        p = ev["plan"]
        print(f"\n  PLAN: {p['summary']}\n  Stack: {p['stack']}")
        for i, f in enumerate(p["features"], 1):
            print(f"   {i}. {f['name']}: {f['description'][:120]}\n      test: {f['test_command']}")
    elif t == "plan_review":
        ok = yes or _ask("Approve this plan and start building?", True)
        run.answer(ev["id"], {"allow": ok})
    elif t == "approval":
        print(f"\n  The agent wants to run:\n    {ev['action']}")
        run.answer(ev["id"], {"allow": _ask("Allow?", False)})
    elif t == "auto_approved":
        print(f"       (running the plan's test: {ev['command']})")
    elif t == "verify":
        print(f"  {'PASS' if ev['passed'] else 'FAIL'}  {ev['feature']}  (round {ev['round']}, "
              f"checked by ContextOS)")
        if not ev["passed"]:
            print("    " + "\n    ".join(ev["output"].splitlines()[-8:]))
    elif t == "security":
        print(f"\n  Security {ev['stage']}:\n    " + ev["report"].replace("\n", "\n    ")[:2000])
    elif t == "done":
        print(f"\nDone: {ev['passed']}/{ev['total']} features pass. Report: "
              f"{run.ws.root / 'BUILD_REPORT.md'}")
    elif t == "failed":
        print(f"\nBuild failed: {ev['error']}")
    elif t == "stopped":
        print("\nStopped.")


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Research, plan and build a project with tests")
    ap.add_argument("goal", help="what to build, in a sentence or two")
    ap.add_argument("--workspace", "-w", required=True,
                    help="project folder (created if missing; must be a dedicated folder)")
    ap.add_argument("--env", default=".env")
    ap.add_argument("--mcp", default="mcp.json", help="MCP servers to connect (optional)")
    ap.add_argument("--max-features", type=int, default=4)
    ap.add_argument("--no-research", action="store_true", help="skip the research phase")
    ap.add_argument("--yes", action="store_true",
                    help="approve the plan automatically (commands still ask)")
    ap.add_argument("--ask-tests", action="store_true",
                    help="ask before running even the plan's own test commands")
    a = ap.parse_args(argv)
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")     # type: ignore[attr-defined]

    conn = Connectors(a.mcp)
    conn.start()
    for row in conn.status():
        print(f"MCP {row['name']}: " + (f"{len(row['tools'])} tools" if row["running"]
                                        else row["error"] or "disabled"))
    try:
        run = BuildRun(a.goal, a.workspace, load_env(a.env), connectors=conn,
                       auto_approve_tests=not a.ask_tests, research=not a.no_research,
                       review_plan=True, max_features=a.max_features)
    except ToolError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    print(f"Building in {run.ws.root}\nGoal: {run.goal}")
    run.start()
    seen = 0
    try:
        while True:
            for ev in run.events_after(seen, 1):
                seen = ev["seq"] + 1
                _print(ev, run, a.yes)
            if run.status in ("done", "failed", "stopped") and seen >= len(run.events):
                break
    except KeyboardInterrupt:
        print("\nStopping after the current step…")
        run.stop.set()
        if run.thread:
            run.thread.join(timeout=180)
    finally:
        conn.close()
    return 0 if run.status == "done" else 1


if __name__ == "__main__":
    raise SystemExit(main())
