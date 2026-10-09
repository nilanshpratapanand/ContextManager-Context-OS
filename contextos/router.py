"""Two-lane model routing: hard prompts go to the strongest free models, easy ones
to the fastest.

The difficulty score is a deterministic heuristic, not an LLM call - classifying a
prompt with a model would spend the very quota the routing is meant to save. The
same score feeds the handoff's difficulty gate (D7), so a lane switch mid-task is
an ordinary escalate/downshift handoff.

Lane choice can be forced per message with a leading "/smart " or "/fast ", or for
the whole session with LLM_ROUTING=smart|fast in .env.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

SMART, FAST = "smart", "fast"
DEFAULT_THRESHOLD = 0.5

_HARD = re.compile(
    r"\b(prove|derive|design|architect\w*|debug\w*|optimi[sz]\w*|refactor\w*|"
    r"analy[sz]\w*|compare|trade-?offs?|why|step[- ]by[- ]step|plan|planning|"
    r"strateg\w*|algorithm\w*|implement\w*|calculat\w*|comput\w*|estimat\w*|"
    r"evaluat\w*|diagnos\w*|migrat\w*|schema|complexity|bug|traceback|exception|"
    r"reason\w*|solve|critique|review)\b", re.I)
_CODE = re.compile(r"```|^\s*(def|class|import|from|public|function|const|let)\s|"
                   r"\bSELECT\b.+\bFROM\b|=>|;\s*$", re.I | re.M)
_CODE_ASK = re.compile(r"\b(function|script|program|code|method|api|endpoint|query|"
                       r"regex|sql|python|javascript|typescript|java|c\+\+|rust|"
                       r"golang|react|linked list|recursion)\b", re.I)
_MAKE = re.compile(r"\b(write|create|build|make|generate|fix|convert|code|port|"
                   r"add|update)\b", re.I)
# Small models got a two-step discount+tax question wrong in testing, so any
# multi-number arithmetic goes to the smart lane.
_MATH = re.compile(r"%|\b(percent|tax|discount|interest|total|average|ratio|"
                   r"probability|profit|loss|price|cost)\b", re.I)
_NUM = re.compile(r"\d+(?:\.\d+)?")
_CHITCHAT = re.compile(r"^\s*(hi|hello|hey|thanks|thank you|ok|okay|yes|no|cool|"
                       r"nice|great|good (morning|night|evening))\b", re.I)
_LIGHT = re.compile(r"\b(rephrase|reword|translate|summari[sz]e|tl;?dr|shorten|"
                    r"format|grammar|spell\w*|synonym|define|capital of)\b", re.I)
_OVERRIDE = re.compile(r"^\s*/(smart|fast)\b\s*", re.I)


@dataclass
class Decision:
    lane: str
    score: float
    reasons: list[str] = field(default_factory=list)
    forced: bool = False


def parse_override(prompt: str) -> tuple[Optional[str], str]:
    """'/smart explain X' -> ('smart', 'explain X')."""
    m = _OVERRIDE.match(prompt or "")
    if not m:
        return None, prompt
    return m.group(1).lower(), prompt[m.end():]


def score(prompt: str) -> tuple[float, list[str]]:
    text = prompt or ""
    reasons: list[str] = []
    if _CHITCHAT.match(text) and len(text) < 60:
        return 0.05, ["chit-chat"]

    s = 0.2
    hard = sorted({m.lower() for m in _HARD.findall(text)})
    if hard:
        s += min(0.45, 0.2 * len(hard))
        reasons.append("hard words: " + ", ".join(hard[:4]))
    if _CODE.search(text):
        s += 0.3
        reasons.append("code")
    elif _CODE_ASK.search(text) and _MAKE.search(text):
        s += 0.3
        reasons.append("code request")
    if len(_NUM.findall(text)) >= 2 and _MATH.search(text):
        s += 0.3
        reasons.append("multi-step arithmetic")
    if len(text) > 300:
        s += 0.1
        reasons.append("long prompt")
    if len(text) > 1200:
        s += 0.15
    if text.count("?") >= 2 or len(re.findall(r"^\s*\d+[.)]\s", text, re.M)) >= 2:
        s += 0.1
        reasons.append("several parts")
    if _LIGHT.search(text) and not hard:
        s -= 0.2
        reasons.append("light edit/lookup")
    return round(max(0.0, min(1.0, s)), 2), reasons or ["simple"]


def decide(prompt: str, mode: str = "auto",
           threshold: float = DEFAULT_THRESHOLD) -> Decision:
    sc, reasons = score(prompt)
    if mode in (SMART, FAST):
        return Decision(mode, sc, reasons + [f"forced {mode}"], forced=True)
    return Decision(SMART if sc >= threshold else FAST, sc, reasons)


def build_chain(lane: str, smart: list[str], fast: list[str],
                cooling: Callable[[str], bool] = lambda _n: False) -> list[str]:
    """Primary lane first, then spill into the other lane. Routes on cooldown go
    to the very end rather than disappearing: if everything is cooling, a stale
    429 is still worth one more try over returning nothing."""
    primary, secondary = (smart, fast) if lane == SMART else (fast, smart)
    seen: list[str] = []
    for n in list(primary) + list(secondary):
        if n not in seen:
            seen.append(n)
    return [n for n in seen if not cooling(n)] + [n for n in seen if cooling(n)]


# ---------------------------------------------------------------- cooldown
_DEAD = ("401", "402", "403", "404", "payment", "model_not_found", "does not exist",
         "no longer available", "invalid api key", "unauthorized")
_BUSY = ("503", "502", "overloaded", "high demand", "unavailable", "timed out",
         "timeout", "network")


def cooldown_for(error: str) -> int:
    """Seconds to bench a route. A retired model or a paywall will not fix itself
    this session; a 429 or a 503 usually clears within a minute."""
    e = (error or "").lower()
    if any(s in e for s in _DEAD):
        return 3600
    if "429" in e or "rate limit" in e or "quota" in e:
        return 60
    if any(s in e for s in _BUSY):
        return 30
    return 15


_HINT = re.compile(r"(?:try again|retry) in\s+(?:(\d+)m)?\s*(\d+(?:\.\d+)?)\s*s", re.I)
_DAILY = re.compile(r"per day|daily|RPD|requests per day|free-models-per-day", re.I)


def retry_hint(error: str) -> Optional[int]:
    """Providers often say exactly when to come back ("try again in 7.5s")."""
    m = _HINT.search(error or "")
    if not m:
        return None
    return int((int(m.group(1) or 0) * 60) + float(m.group(2))) + 1


class Cooldown:
    """How long to rest each route. Repeat failures rest progressively longer, so a
    provider that is out of quota stops being retried on every request."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._until: dict[str, float] = {}
        self._strikes: dict[str, int] = {}
        self._clock = clock

    def hit(self, name: str, error: str) -> int:
        strikes = self._strikes[name] = self._strikes.get(name, 0) + 1
        base = cooldown_for(error)
        hint = retry_hint(error)
        if ("429" in error or "quota" in error.lower()) and _DAILY.search(error):
            secs = 3600                             # daily allowance used up
        elif hint:
            secs = min(hint, 3600)
        else:
            secs = min(base * 2 ** (strikes - 1), max(base, 1800))
        self._until[name] = self._clock() + secs
        return secs

    def ok(self, name: str) -> None:
        self._strikes.pop(name, None)

    def remaining(self, name: str) -> int:
        return max(0, int(self._until.get(name, 0) - self._clock()))

    def active(self, name: str) -> bool:
        return self.remaining(name) > 0

    def clear(self, names: Optional[Iterable[str]] = None) -> None:
        if names is None:
            self._until.clear()
            self._strikes.clear()
        else:
            for n in names:
                self._until.pop(n, None)
                self._strikes.pop(n, None)
