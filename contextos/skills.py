"""Agent Skills in the open SKILL.md format (agentskills.io).

A skill is a folder holding SKILL.md: YAML front matter with `name` and
`description`, then Markdown instructions. Loading is progressive: the agent
sees only name + description up front and reads the body with use_skill when a
task calls for it, so unused skills cost almost no context.

Skills are found in, in order (earlier wins on a name clash):
    <workspace>/skills/   project-specific
    ./skills/             next to where ContextOS runs
    ~/.contextos/skills/  personal
    contextos/skills/     bundled with ContextOS
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .tools import Tool, ToolError, clip

BUNDLED = Path(__file__).parent / "skills"
_NAME = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


@dataclass
class Skill:
    name: str
    description: str
    path: Path

    def body(self) -> str:
        text = (self.path / "SKILL.md").read_text(encoding="utf-8")
        return _split(text)[1].strip()

    def files(self) -> list[str]:
        return sorted(p.relative_to(self.path).as_posix() for p in self.path.rglob("*")
                      if p.is_file() and p.name != "SKILL.md")


def _split(text: str) -> tuple[dict[str, str], str]:
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.S)
    if not m:
        return {}, text
    meta: dict[str, str] = {}
    for line in m.group(1).splitlines():
        k, sep, v = line.partition(":")
        if sep and not line.startswith((" ", "\t")):
            meta[k.strip()] = v.strip().strip('"').strip("'")
    return meta, m.group(2)


def discover(*roots: Optional[str]) -> dict[str, Skill]:
    found: dict[str, Skill] = {}
    dirs = [Path(r) for r in roots if r] + [Path.home() / ".contextos" / "skills", BUNDLED]
    for root in dirs:
        if not root.is_dir():
            continue
        for sk in sorted(root.glob("*/SKILL.md")):
            meta, _ = _split(sk.read_text(encoding="utf-8", errors="replace"))
            name, desc = meta.get("name", ""), meta.get("description", "")
            # The spec: lowercase-hyphen name that matches its folder, plus a
            # description. Anything else is skipped rather than half-loaded.
            if not (_NAME.match(name) and name == sk.parent.name and desc):
                continue
            found.setdefault(name, Skill(name, desc[:1024], sk.parent))
    return found


def skills_tool(skills: dict[str, Skill]) -> Tool:
    def use_skill(name: str, file: str = "") -> str:
        sk = skills.get(name.strip())
        if not sk:
            raise ToolError(f"no skill '{name}'. Available: {', '.join(skills) or 'none'}")
        if file:
            p = (sk.path / file).resolve()
            if sk.path.resolve() not in p.parents or not p.is_file():
                raise ToolError(f"{file} is not a file in skill {name}")
            return clip(p.read_text(encoding="utf-8", errors="replace"), 8000)
        extra = sk.files()
        return clip(sk.body() + (f"\n\nReference files (read with file=...): "
                                 f"{', '.join(extra)}" if extra else ""), 8000)
    return Tool("use_skill", "Load a skill's full instructions before doing that kind "
                "of work.", {"name": "skill name", "file?": "a reference file inside it"},
                use_skill)


def catalog(skills: dict[str, Skill]) -> str:
    return "\n".join(f"- {s.name}: {s.description}" for s in skills.values()) or "(none)"
