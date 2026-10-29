"""Build the static web edition into web/ (what Netlify serves).

    python tools/build_web.py           write web/
    python tools/build_web.py --check   exit 1 if web/ is out of date (used by the tests)

web/ is generated from contextos/dashboard.html (the same UI the desktop app serves),
webapp/engine.js (the in-browser engine) and contextos/static/. Edit those, then rebuild.
"""
from __future__ import annotations

import argparse
import pathlib
import re
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "web"
MARKER = "<script>\n'use strict';"


def version() -> str:
    m = re.search(r'__version__\s*=\s*"([^"]+)"', (ROOT / "contextos" / "__init__.py").read_text("utf-8"))
    if not m:
        raise SystemExit("could not read the version from contextos/__init__.py")
    return m.group(1)


def render() -> dict[str, bytes]:
    html = (ROOT / "contextos" / "dashboard.html").read_text("utf-8")
    if html.count(MARKER) != 1:
        raise SystemExit("dashboard.html: expected exactly one main <script> block")
    # The engine goes first so its fetch shim is installed before the page's own script runs.
    html = html.replace(MARKER, '<script src="/engine.js"></script>\n' + MARKER)
    files = {"index.html": html.encode("utf-8"),
             "engine.js": (ROOT / "webapp" / "engine.js").read_text("utf-8").replace("__VERSION__", version()).encode("utf-8")}
    static = ROOT / "contextos" / "static"
    for p in sorted(static.rglob("*")):
        if p.is_file():
            files["static/" + p.relative_to(static).as_posix()] = p.read_bytes()
    return files


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    files = render()
    if args.check:
        stale = [n for n, b in files.items() if not (OUT / n).is_file() or (OUT / n).read_bytes() != b]
        extra = [str(p.relative_to(OUT)) for p in OUT.rglob("*") if p.is_file()
                 and p.relative_to(OUT).as_posix() not in files] if OUT.is_dir() else []
        if stale or extra:
            print("web/ is out of date; run: python tools/build_web.py\n  stale: %s\n  extra: %s" % (stale, extra))
            return 1
        print("web/ is up to date")
        return 0
    if OUT.exists():
        shutil.rmtree(OUT)
    for name, data in files.items():
        dest = OUT / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    print(f"wrote {len(files)} files to {OUT.relative_to(ROOT)}/ (version {version()})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
