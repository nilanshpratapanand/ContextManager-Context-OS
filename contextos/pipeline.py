"""Pipeline mode: split one prompt into subtasks, give each to the free model that
suits it, run independent ones in parallel, then merge.

Design, and the evidence behind it (verified against the arXiv abstracts):
  * Plan -> parallel execute -> join is the LLMCompiler shape (Kim et al., 2312.04511:
    up to 3.7x lower latency than sequential ReAct). Subtasks form a DAG; each wave of
    ready subtasks runs concurrently.
  * Routing each piece to the cheapest model that can do it is the FrugalGPT /
    RouteLLM idea (2305.05176: up to 98% cost cut at GPT-4 quality; 2406.18665: >2x
    in some settings). Here "cost" is free-tier quota, so easy pieces use the fast
    lane and only hard pieces spend smart-lane quota.
  * One aggregator model reading several proposers' outputs is Mixture-of-Agents
    (Wang et al., 2406.04692). The final merge step does that.
  * Each worker sees only the outputs of the subtasks it depends on, never the whole
    transcript: the same "carry state, not trajectory" idea ContextOS is built on, and
    it keeps every call small enough for free-tier token limits.

The planner is an ordinary model call, but a deterministic fallback plan (numbered
parts, or one task) is used when its reply is not usable, so a flaky planner can
never block the turn. Everything takes an `ask(lane, system, user) -> (text, who)`
callable, so tests run with a scripted model and no network.
"""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Optional

from . import router

Ask = Callable[[str, str, str], "tuple[str, str]"]

MAX_STEPS = 6
# Which lane a kind of work belongs to. Reasoning-heavy work spends smart-lane
# quota; mechanical work goes to the fast lane.
KIND_LANE = {"reason": router.SMART, "code": router.SMART, "math": router.SMART,
             "write": router.SMART, "extract": router.FAST, "summarize": router.FAST,
             "format": router.FAST, "lookup": router.FAST, "vision": router.SMART}
KINDS = tuple(KIND_LANE)

PLANNER_SYSTEM = (
    "You split a user's request into the smallest useful set of subtasks.\n"
    "Reply with ONE JSON object and nothing else:\n"
    '{"steps":[{"id":"s1","task":"...","kind":"reason|code|math|write|extract|'
    'summarize|format|lookup","needs":[]}]}\n'
    f"Rules: at most {MAX_STEPS} steps. 'needs' lists ids whose output this step "
    "requires; steps with no dependency run in parallel. Make each task self-contained: "
    "a worker sees only the task text and its needed outputs. If the request is simple, "
    "return a single step. Do not answer the request yourself.")

WORKER_SYSTEM = ("You complete one subtask of a larger job. Use only the task and the "
                 "provided inputs. Be complete and concise; no preamble.")

MERGE_SYSTEM = ("You merge the results of several subtasks into one final answer to the "
                "user's original request. Resolve any contradictions, keep every concrete "
                "number, name and code block that matters, and do not mention subtasks.")


@dataclass
class Step:
    id: str
    task: str
    kind: str = "reason"
    needs: list[str] = field(default_factory=list)
    lane: str = router.SMART
    output: str = ""
    provider: str = ""
    error: str = ""


# ---------------------------------------------------------------- planning
def _json_obj(text: str) -> Optional[dict]:
    dec = json.JSONDecoder()
    for i, ch in enumerate(text or ""):
        if ch == "{":
            try:
                obj, _ = dec.raw_decode(text, i)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and isinstance(obj.get("steps"), list):
                return obj
    return None


def validate(raw_steps: list[Any]) -> list[Step]:
    """Turn model output into a safe DAG: known kinds, real ids, no self/forward
    cycles. Raises ValueError when nothing usable remains."""
    steps: list[Step] = []
    seen: set[str] = set()
    for i, r in enumerate(raw_steps[:MAX_STEPS]):
        if not isinstance(r, dict) or not str(r.get("task", "")).strip():
            continue
        sid = re.sub(r"[^\w-]", "", str(r.get("id") or f"s{i + 1}"))[:12] or f"s{i + 1}"
        if sid in seen:
            sid = f"s{i + 1}"
        kind = str(r.get("kind", "reason")).lower()
        kind = kind if kind in KIND_LANE else "reason"
        # Only earlier steps may be dependencies: forward references cannot form a cycle.
        needs = [n for n in (r.get("needs") or []) if isinstance(n, str) and n in seen]
        steps.append(Step(sid, str(r["task"]).strip()[:1500], kind, needs, KIND_LANE[kind]))
        seen.add(sid)
    if not steps:
        raise ValueError("no usable steps")
    return steps


_NUMBERED = re.compile(r"^\s*(?:\d+[.)]|[-*])\s+(.+)$", re.M)


def fallback_plan(prompt: str) -> list[Step]:
    """Deterministic plan: one step per numbered/bulleted part, else a single step."""
    parts = [m.strip() for m in _NUMBERED.findall(prompt)]
    if len(parts) >= 2:
        steps = []
        for i, p in enumerate(parts[:MAX_STEPS]):
            sc, _ = router.score(p)
            kind = "reason" if sc >= router.DEFAULT_THRESHOLD else "summarize"
            steps.append(Step(f"s{i + 1}", p, kind, [], KIND_LANE[kind]))
        return steps
    sc, _ = router.score(prompt)
    kind = "reason" if sc >= router.DEFAULT_THRESHOLD else "write"
    return [Step("s1", prompt.strip(), kind, [], router.SMART if sc >= router.DEFAULT_THRESHOLD
                 else router.FAST)]


def plan(prompt: str, ask: Ask, context: str = "") -> tuple[list[Step], str]:
    """Returns (steps, how) where how is 'model' or 'fallback'."""
    user = (f"Context on file:\n{context}\n\n" if context else "") + f"Request:\n{prompt}"
    try:
        text, _ = ask(router.SMART, PLANNER_SYSTEM, user)
        obj = _json_obj(text)
        if obj:
            return validate(obj["steps"]), "model"
    except Exception:
        pass
    return fallback_plan(prompt), "fallback"


def waves(steps: list[Step]) -> list[list[Step]]:
    """Group steps into dependency waves; steps inside a wave are independent."""
    done: set[str] = set()
    left = list(steps)
    out: list[list[Step]] = []
    while left:
        ready = [s for s in left if all(n in done for n in s.needs)] or [left[0]]
        out.append(ready)
        done.update(s.id for s in ready)
        left = [s for s in left if s not in ready]
    return out


# --------------------------------------------------------------- execution
def _worker_prompt(step: Step, by_id: dict[str, Step], context: str) -> str:
    parts = []
    if context:
        parts.append(f"## Context on file\n{context}")
    for n in step.needs:
        dep = by_id[n]
        parts.append(f"## Input from {dep.id} ({dep.task[:80]})\n{dep.output}")
    parts.append(f"## Your subtask\n{step.task}")
    return "\n\n".join(parts)


def run(prompt: str, ask: Ask, context: str = "", *, workers: int = 3,
        merge: bool = True) -> Iterator[dict[str, Any]]:
    """Yield events: plan, step_start, step_done, merge_start, result."""
    steps, how = plan(prompt, ask, context)
    by_id = {s.id: s for s in steps}
    yield {"type": "pipeline_plan", "how": how,
           "steps": [{"id": s.id, "task": s.task, "kind": s.kind, "lane": s.lane,
                      "needs": s.needs} for s in steps]}
    for wave in waves(steps):
        for s in wave:
            yield {"type": "step_start", "id": s.id}

        def work(s: Step) -> Step:
            try:
                s.output, s.provider = ask(s.lane, WORKER_SYSTEM,
                                           _worker_prompt(s, by_id, context))
            except Exception as e:                       # one bad step must not sink the run
                s.error = str(e)[:200]
            return s

        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(wave)))) as pool:
            for s in pool.map(work, wave):
                yield {"type": "step_done", "id": s.id, "provider": s.provider,
                       "ok": not s.error, "error": s.error,
                       "preview": s.output[:160]}
    good = [s for s in steps if s.output and not s.error]
    if not good:
        yield {"type": "pipeline_error", "error": "every subtask failed: "
               + "; ".join(f"{s.id}: {s.error}" for s in steps)[:300]}
        return
    if len(steps) == 1 or not merge:
        final, who = good[-1].output, good[-1].provider
    else:
        yield {"type": "merge_start"}
        body = "\n\n".join(f"### {s.id} [{s.kind}]: {s.task[:120]}\n{s.output}"
                           for s in good)
        missing = [s.id for s in steps if s not in good]
        note = (f"\n\nNote: subtasks {', '.join(missing)} failed; say what is missing."
                if missing else "")
        try:
            final, who = ask(router.SMART, MERGE_SYSTEM,
                             f"Original request:\n{prompt}\n\nSubtask results:\n{body}{note}")
        except Exception:
            final = "\n\n".join(s.output for s in good)   # merging failed: show the parts
            who = good[-1].provider
    yield {"type": "pipeline_result", "text": final, "provider": who,
           "steps": [{"id": s.id, "kind": s.kind, "lane": s.lane,
                      "provider": s.provider, "ok": not s.error} for s in steps]}
