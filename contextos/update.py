"""Self-update: bring this copy of ContextOS up to the latest GitHub release.

Run by RUN.bat and run.sh on every start (``python -m contextos.update``).

* Looks up the latest published release of the repository (5 s timeout; being
  offline or rate limited is never an error, the app just starts as it is).
* A git checkout is fast-forwarded to the release tag, and only when that is
  clean and safe. Anything else is left alone.
* Any other copy (the installer's zip route) gets the release's files written
  over it. Your ``.env``, ``chat_data/``, databases, ``mcp.json`` and ``.venv``
  are never touched, files the previous update wrote that the release dropped
  are removed, and every file that gets replaced is backed up first.

Exit code 10 means "updated, please restart"; the launchers do that. Anything
else means carry on. Opt out with ``CONTEXTOS_NO_UPDATE=1`` or ``--no-update``.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from typing import Optional

from . import __version__

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_REPO = "nilanshpratapanand/ContextManager-Context-OS"
UPDATED = 10                                   # exit code: restart me
MANIFEST = ".contextos-manifest.json"
BACKUP_DIR = ".update_backup"
MAX_ZIP = 60 * 1024 * 1024
_TAG = re.compile(r"^v?(\d+(?:\.\d+){0,3})$")
_SLUG = re.compile(r"^[\w.-]+/[\w.-]+$")

# Never overwritten and never deleted, whatever a release contains.
_PROTECTED_TOP = {".env", ".venv", "venv", "chat_data", ".git", "mcp.json", BACKUP_DIR, MANIFEST}
_PROTECTED_SUFFIX = (".db", ".db-wal", ".db-shm", ".sqlite", ".sqlite3", ".log")


def parse_version(tag: str) -> Optional[tuple[int, ...]]:
    m = _TAG.match((tag or "").strip())
    return tuple(int(x) for x in m.group(1).split(".")) if m else None


def is_newer(remote: str, local: str) -> bool:
    r, l = parse_version(remote), parse_version(local)
    if r is None or l is None:
        return False
    n = max(len(r), len(l))
    return r + (0,) * (n - len(r)) > l + (0,) * (n - len(l))


def protected(rel: str) -> bool:
    parts = pathlib.PurePosixPath(rel).parts
    return (not parts or parts[0] in _PROTECTED_TOP
            or rel.lower().endswith(_PROTECTED_SUFFIX) or "__pycache__" in parts)


def _get(url: str, timeout: float = 5.0, limit: int = MAX_ZIP) -> bytes:
    if not url.startswith("https://"):
        raise ValueError("refusing a non-https URL")
    req = urllib.request.Request(url, headers={"User-Agent": f"ContextOS/{__version__}",
                                               "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read(limit + 1)
    if len(data) > limit:
        raise ValueError("download is larger than expected")
    return data


def latest_release(repo: str) -> Optional[str]:
    """Tag of the newest published (non-draft, non-prerelease) release, or None."""
    try:
        info = json.loads(_get(f"https://api.github.com/repos/{repo}/releases/latest", limit=1 << 20))
        return str(info.get("tag_name") or "") or None
    except (OSError, ValueError, urllib.error.URLError):
        return None


# ------------------------------------------------------------------- zip route
def _members(zf: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    """Release files keyed by path relative to the project root, validated."""
    out: dict[str, zipfile.ZipInfo] = {}
    for zi in zf.infolist():
        if zi.is_dir():
            continue
        if (zi.external_attr >> 16) & 0o170000 == 0o120000:      # symlink
            continue
        _, _, rel = zi.filename.partition("/")                  # drop "<repo>-<ver>/"
        p = pathlib.PurePosixPath(rel)
        if not rel or p.is_absolute() or ".." in p.parts or ":" in rel or "\\" in rel:
            raise ValueError(f"unsafe path in release: {zi.filename!r}")
        if not protected(rel):
            out[rel] = zi
    if "contextos/server.py" not in out:
        raise ValueError("release archive does not look like ContextOS")
    return out


def _sha(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def apply_zip(data: bytes, root: pathlib.Path, old_version: str, new_version: str) -> dict[str, int]:
    """Write a release archive over ``root``. Returns counts of what changed."""
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        files = _members(zf)
        manifest_path = root / MANIFEST
        try:
            previous = set(json.loads(manifest_path.read_text("utf-8")).get("files", []))
        except (OSError, ValueError):
            previous = set()
        backup = root / BACKUP_DIR / f"v{old_version}"
        stats = {"written": 0, "removed": 0, "backed_up": 0}
        for rel, zi in sorted(files.items()):
            dest = root / rel
            new_bytes = zf.read(zi)
            if dest.is_file():
                if hashlib.sha256(new_bytes).hexdigest() == _sha(dest):
                    continue
                b = backup / rel
                b.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(dest, b)
                stats["backed_up"] += 1
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(dest.name + ".new")
            tmp.write_bytes(new_bytes)
            os.replace(tmp, dest)               # new inode: safe under a running launcher
            stats["written"] += 1
        for rel in sorted(previous - set(files)):
            dest = root / rel
            if not protected(rel) and dest.is_file():
                b = backup / rel
                b.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(dest, b)
                dest.unlink()
                stats["removed"] += 1
        manifest_path.write_text(json.dumps({"version": new_version, "files": sorted(files)}, indent=1), "utf-8")
    # keep only the newest backup
    bdir = root / BACKUP_DIR
    if bdir.is_dir():
        for d in sorted(bdir.iterdir(), key=lambda p: p.stat().st_mtime)[:-1]:
            shutil.rmtree(d, ignore_errors=True)
    return stats


# ------------------------------------------------------------------- git route
def _git(*args: str, cwd: pathlib.Path = ROOT) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=60)


def update_git(tag: str) -> tuple[bool, str]:
    """Fast-forward the current branch to the release tag. (ok, message)"""
    if not shutil.which("git"):
        return False, "git is not installed"
    if _git("fetch", "--quiet", "--tags", "origin").returncode != 0:
        return False, "could not reach the remote"
    if _git("rev-parse", "-q", "--verify", f"refs/tags/{tag}").returncode != 0:
        return False, f"tag {tag} not found"
    if _git("status", "--porcelain", "--untracked-files=no").stdout.strip():
        return False, "you have local changes to tracked files"
    if _git("merge-base", "--is-ancestor", "HEAD", f"refs/tags/{tag}").returncode != 0:
        return False, "this branch is not behind the release (it has its own commits)"
    r = _git("merge", "--ff-only", "--quiet", f"refs/tags/{tag}")
    return (r.returncode == 0, r.stderr.strip() or "fast-forwarded")


# ------------------------------------------------------------------------ main
def _requirements_hash() -> str:
    p = ROOT / "requirements.txt"
    return _sha(p) if p.is_file() else ""


def _refresh_requirements() -> None:
    """Optional packages only; a failure never blocks the app."""
    if sys.prefix == getattr(sys, "base_prefix", sys.prefix):
        return                                   # not in a private venv: leave the system alone
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "-r", str(ROOT / "requirements.txt")],
                   capture_output=True, timeout=180)


def run(check_only: bool = False, force: bool = False) -> int:
    repo = os.environ.get("CONTEXTOS_UPDATE_REPO", DEFAULT_REPO)
    if not _SLUG.match(repo):
        return 0
    tag = latest_release(repo)
    if not tag:
        return 0                                  # offline, rate limited or no release: carry on
    if not (force or is_newer(tag, __version__)):
        if check_only:
            print(f"ContextOS {__version__} is the latest release.")
        return 0
    if check_only:
        print(f"A newer release is available: {tag} (you have {__version__}). Run RUN.bat / run.sh to update.")
        return 0
    print(f"Updating ContextOS {__version__} -> {tag} ...")
    req_before = _requirements_hash()
    try:
        if (ROOT / ".git").exists():
            ok, msg = update_git(tag)
            if not ok:
                print(f"  Not updated automatically: {msg}. Staying on {__version__}.")
                return 0
        else:
            data = _get(f"https://github.com/{repo}/archive/refs/tags/{tag}.zip", timeout=30)
            stats = apply_zip(data, ROOT, __version__, tag.lstrip("v"))
            print(f"  {stats['written']} files updated, {stats['removed']} removed. "
                  f"Your .env, chats and keys were not touched"
                  + (f"; replaced files are saved in {BACKUP_DIR}/." if stats["backed_up"] else "."))
        # A rewritten .py of the same size and second would keep a stale .pyc alive.
        for d in (ROOT / "contextos", ROOT / "tests"):
            shutil.rmtree(d / "__pycache__", ignore_errors=True)
        if _requirements_hash() != req_before:
            _refresh_requirements()
    except (OSError, ValueError, zipfile.BadZipFile, urllib.error.URLError, subprocess.SubprocessError) as e:
        print(f"  Update failed ({e}). Staying on {__version__}.")
        return 0
    print(f"  Now on {tag}.")
    return UPDATED


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Update ContextOS to the latest release")
    ap.add_argument("--check", action="store_true", help="only report whether an update exists")
    ap.add_argument("--force", action="store_true", help="reinstall the latest release even if up to date")
    ap.add_argument("--no-update", action="store_true", help="do nothing (used by the launchers)")
    a = ap.parse_args(argv)
    if a.no_update or os.environ.get("CONTEXTOS_NO_UPDATE") == "1":
        return 0
    return run(check_only=a.check, force=a.force)


if __name__ == "__main__":
    raise SystemExit(main())
