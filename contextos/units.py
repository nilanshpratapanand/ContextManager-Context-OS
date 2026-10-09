"""Core value types: addresses, units, token accounting."""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

# Fixed address roots. A closed vocabulary is what makes the router deterministic
# (PLAN D8: packets must normalise to a neutral schema, not an agent's idiom).
ROOTS = ("user", "project", "task", "agent", "tool", "artifact")

KINDS = (
    "goal",         # what we are trying to do
    "constraint",   # a rule the solution must respect
    "decision",     # a choice made, with rationale
    "fact",         # something established about the world
    "blocker",      # something preventing progress
    "artifact",     # a real file/diff on disk (path + sha256 in meta)
    "tool_result",  # output of a tool call
    "preference",   # user preference
)

LIFETIMES = ("permanent", "task", "ephemeral")

_SEG = re.compile(r"^[a-z0-9][a-z0-9_.\-]*$")
MAX_DEPTH = 8


class AddressError(ValueError):
    pass


def normalize_address(address: str) -> str:
    """Validate and canonicalise an address. Raises AddressError on bad input."""
    if not isinstance(address, str) or not address.strip():
        raise AddressError("address must be a non-empty string")
    a = "/" + address.strip().strip("/").lower()
    parts = [p for p in a.split("/") if p]
    if not parts:
        raise AddressError("address must have at least a root segment")
    if parts[0] not in ROOTS:
        raise AddressError(f"root {parts[0]!r} not in {ROOTS}")
    if len(parts) > MAX_DEPTH:
        raise AddressError(f"address deeper than {MAX_DEPTH} segments")
    for p in parts:
        if not _SEG.match(p):
            raise AddressError(f"illegal segment {p!r}")
    return "/" + "/".join(parts)


def address_segments(address: str) -> list[str]:
    return [p for p in address.split("/") if p]


# --------------------------------------------------------------------------
# Token accounting
# --------------------------------------------------------------------------
_ENCODER = None
_ENCODER_TRIED = False


def _encoder():
    global _ENCODER, _ENCODER_TRIED
    if not _ENCODER_TRIED:
        _ENCODER_TRIED = True
        try:  # pragma: no cover - depends on optional dependency
            import tiktoken

            _ENCODER = tiktoken.get_encoding("cl100k_base")
        except Exception:
            _ENCODER = None
    return _ENCODER


def count_tokens(text: str) -> int:
    """Exact with tiktoken when installed, else a chars/4 estimate.

    The estimator is deliberately conservative: budgets are enforced against it,
    so an under-count would silently overflow the receiving model's window.
    """
    if not text:
        return 0
    enc = _encoder()
    if enc is not None:  # pragma: no cover
        return len(enc.encode(text))
    return max(1, (len(text) + 3) // 4)


def tokens_exact() -> bool:
    return _encoder() is not None


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# Unit
# --------------------------------------------------------------------------
@dataclass
class Unit:
    address: str
    value: str
    kind: str = "fact"
    source: str = "unknown"
    lifetime: str = "task"
    importance: float = 0.5
    confidence: float = 1.0
    valid_from: float = field(default_factory=time.time)
    valid_to: Optional[float] = None          # set => superseded / no longer true
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    version: int = 1
    tokens: int = 0
    pinned: bool = False
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.address = normalize_address(self.address)
        if self.kind not in KINDS:
            raise ValueError(f"kind {self.kind!r} not in {KINDS}")
        if self.lifetime not in LIFETIMES:
            raise ValueError(f"lifetime {self.lifetime!r} not in {LIFETIMES}")
        self.importance = min(1.0, max(0.0, float(self.importance)))
        self.confidence = min(1.0, max(0.0, float(self.confidence)))
        if not self.tokens:
            self.tokens = count_tokens(self.render())

    @property
    def live(self) -> bool:
        return self.valid_to is None

    def render(self) -> str:
        """Neutral rendering used both for budgeting and for the packet (D8)."""
        head = f"{self.address} [{self.kind}]"
        if self.kind == "artifact" and self.meta.get("path"):
            head += f" path={self.meta['path']}"
            if self.meta.get("sha256"):
                head += f" sha256={str(self.meta['sha256'])[:12]}"
        return f"{head}\n{self.value}"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["pinned"] = bool(self.pinned)
        return d

    def to_row(self) -> tuple:
        return (
            self.address, self.value, self.kind, self.source, self.lifetime,
            self.importance, self.confidence, self.valid_from, self.valid_to,
            self.created_at, self.updated_at, self.version, self.tokens,
            int(self.pinned), json.dumps(self.meta, separators=(",", ":")),
        )

    @staticmethod
    def from_row(row) -> "Unit":
        u = Unit.__new__(Unit)
        (u.address, u.value, u.kind, u.source, u.lifetime, u.importance,
         u.confidence, u.valid_from, u.valid_to, u.created_at, u.updated_at,
         u.version, u.tokens, pinned, meta) = row
        u.pinned = bool(pinned)
        u.meta = json.loads(meta) if meta else {}
        return u


COLUMNS = (
    "address, value, kind, source, lifetime, importance, confidence, "
    "valid_from, valid_to, created_at, updated_at, version, tokens, pinned, meta"
)
