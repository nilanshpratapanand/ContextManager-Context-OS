"""Tools the build agent can use.

Design follows the SWE-agent findings on agent-computer interfaces: small, specific
actions; bounded output; edits by exact search/replace with a syntax check that
rejects a broken edit instead of saving it.

Safety is containment, not detection (OWASP AI Agent Security):
  * file tools cannot leave the workspace folder
  * commands run only after approval, in the workspace, without API keys in env
  * fetch_url refuses loopback / private / link-local addresses, so a web page
    cannot steer the agent into this machine or the local network
  * everything fetched is data; the agent prompt says never to obey it
"""
from __future__ import annotations

import html
import html.parser
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

MAX_OUT = 4000                     # characters of any single tool result
SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules", ".mypy_cache",
             ".pytest_cache", "dist", "build", ".idea", ".vscode"}
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


class ToolError(Exception):
    """A tool could not do what was asked. The message goes back to the model."""


def clip(text: str, limit: int = MAX_OUT) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n… [{len(text) - limit} more characters not shown]"


@dataclass
class Tool:
    name: str
    description: str
    params: dict[str, str]           # name -> description; "?" suffix = optional
    fn: Callable[..., str]
    risk: str = "read"               # read | write | exec | network | external

    def spec(self) -> str:
        """One line per tool: sent every agent step, so every token counts."""
        args = ", ".join(f"{k}" for k in self.params)
        hints = "; ".join(f"{k.rstrip('?')}={v}" for k, v in self.params.items()
                          if len(v) < 60 and not v.startswith(("file path", "full file")))
        return f"- {self.name}({args}): {self.description}" + (f" [{hints}]" if hints else "")


# ---------------------------------------------------------------- workspace
class Workspace:
    def __init__(self, root: str) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    # Paths that run code later without anyone approving it (git hooks), or that
    # hold secrets. OWASP's agent guidance calls out implicit-execution paths.
    _PROTECTED = re.compile(r"(^|/)(\.git(/|$)|\.env($|\.)|[^/]*\.(pem|key|p12|pfx)$|id_rsa|"
                            r"id_ed25519|\.contextos_memory\.db)", re.I)

    def path(self, rel: str) -> Path:
        p = (self.root / (rel or ".")).resolve()
        if p != self.root and self.root not in p.parents:
            raise ToolError(f"'{rel}' is outside the workspace; only files under "
                            f"{self.root.name}/ can be used")
        if p != self.root and self._PROTECTED.search(p.relative_to(self.root).as_posix()):
            raise ToolError(f"'{rel}' is protected (git internals, secrets or keys) and "
                            "can't be read or changed by the agent")
        return p

    def rel(self, p: Path) -> str:
        return p.relative_to(self.root).as_posix() or "."

    # ------------------------------------------------------------ file tools
    def list_files(self, path: str = ".", depth: str = "3") -> str:
        base = self.path(path)
        if not base.exists():
            raise ToolError(f"{path} does not exist")
        max_depth = max(1, min(int(depth or 3), 6))
        out: list[str] = []
        for p in sorted(base.rglob("*")):
            parts = p.relative_to(base).parts
            if (any(x in SKIP_DIRS for x in parts) or len(parts) > max_depth
                    or self._PROTECTED.search(p.relative_to(self.root).as_posix())):
                continue
            out.append(self.rel(p) + ("/" if p.is_dir() else f"  ({p.stat().st_size} B)"))
            if len(out) >= 300:
                out.append("… more files not shown")
                break
        return "\n".join(out) or "(empty)"

    def read_file(self, path: str, start: str = "1", lines: str = "200") -> str:
        p = self.path(path)
        if not p.is_file():
            raise ToolError(f"{path} is not a file")
        text = p.read_text(encoding="utf-8", errors="replace").splitlines()
        a = max(1, int(start or 1))
        n = max(1, min(int(lines or 200), 400))
        chunk = text[a - 1:a - 1 + n]
        body = "\n".join(f"{i:>4}| {ln}" for i, ln in enumerate(chunk, a))
        more = f"\n… lines {a + n}-{len(text)} not shown" if a - 1 + n < len(text) else ""
        return clip(f"{path} ({len(text)} lines)\n{body}{more}")

    def write_file(self, path: str, content: str) -> str:
        p = self.path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        warn = _syntax_error(p, content)
        return f"wrote {path} ({len(content)} chars)" + (f"\nWARNING: {warn}" if warn else "")

    def edit_file(self, path: str, old: str, new: str) -> str:
        p = self.path(path)
        if not p.is_file():
            raise ToolError(f"{path} does not exist; use write_file to create it")
        text = p.read_text(encoding="utf-8", errors="replace")
        n = text.count(old) if old else 0
        if n == 0:
            raise ToolError("old text not found - read_file first and copy it exactly, "
                            "including indentation")
        if n > 1:
            raise ToolError(f"old text appears {n} times - include more surrounding "
                            "lines so it is unique")
        updated = text.replace(old, new, 1)
        err = _syntax_error(p, updated)
        if err:
            raise ToolError(f"edit NOT applied, it would break the file: {err}")
        p.write_text(updated, encoding="utf-8")
        return f"edited {path}"

    def search_code(self, pattern: str, path: str = ".") -> str:
        try:
            rx = re.compile(pattern)
        except re.error as e:
            raise ToolError(f"bad regex: {e}") from None
        base = self.path(path)
        hits: list[str] = []
        files = [base] if base.is_file() else base.rglob("*")
        for p in files:
            if not p.is_file() or any(x in SKIP_DIRS for x in p.parts) or p.stat().st_size > 500_000:
                continue
            try:
                for i, ln in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                    if rx.search(ln):
                        hits.append(f"{self.rel(p)}:{i}: {ln.strip()[:160]}")
                        if len(hits) >= 60:
                            return "\n".join(hits) + "\n… more matches not shown"
            except (UnicodeDecodeError, OSError):
                continue
        return "\n".join(hits) or "no matches"

    def run_command(self, command: str, timeout: str = "120") -> str:
        """Runs in the workspace. Approval happens before this is called."""
        env = {k: v for k, v in os.environ.items()
               if not re.search(r"(API_KEY|TOKEN|SECRET|PASSWORD|ACCOUNT_ID)$", k)}
        env["PYTHONIOENCODING"] = "utf-8"
        # "python" means this interpreter, so commands work even where the bare
        # name isn't on PATH (e.g. Windows with only the py launcher).
        cmd = re.sub(r"^\s*(python3?|py)(?=\s|$)", lambda m: f'"{sys.executable}"', command)
        try:
            # shell=True is deliberate: the string is exactly what the user approved
            # (or a plain test runner that passed builder.safe_test_command).
            r = subprocess.run(cmd, shell=True, cwd=self.root, env=env, capture_output=True,
                               text=True, encoding="utf-8", errors="replace",
                               timeout=max(5, min(int(timeout or 120), 600)))
        except subprocess.TimeoutExpired:
            raise ToolError(f"command timed out after {timeout}s") from None
        out = (r.stdout or "") + (("\n[stderr]\n" + r.stderr) if r.stderr.strip() else "")
        # The end of the output (the error, the test summary) matters most.
        tail = out[-MAX_OUT:]
        cut = f"… {len(out) - MAX_OUT} earlier characters not shown\n" if len(out) > MAX_OUT else ""
        return f"exit code {r.returncode}\n{cut}{tail}".strip()


def _syntax_error(p: Path, text: str) -> Optional[str]:
    if p.suffix == ".py":
        try:
            compile(text, str(p.name), "exec")
        except SyntaxError as e:
            return f"Python syntax error line {e.lineno}: {e.msg}"
    elif p.suffix == ".json":
        try:
            json.loads(text)
        except json.JSONDecodeError as e:
            return f"invalid JSON line {e.lineno}: {e.msg}"
    return None


# ------------------------------------------------------------------- web
def _public_url(url: str) -> str:
    u = urllib.parse.urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ToolError("only http(s) URLs can be fetched")
    try:
        infos = socket.getaddrinfo(u.hostname, None)
    except socket.gaierror:
        raise ToolError(f"cannot resolve {u.hostname}") from None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            raise ToolError(f"{u.hostname} is a local or private address - blocked")
    return url


class _NoRedirectToPrivate(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _public_url(newurl)          # a public page must not bounce us inward
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_NoRedirectToPrivate)


def _http(url: str, data: Optional[bytes] = None, timeout: int = 20,
          accept: str = "text/html,application/xhtml+xml,*/*",
          headers: Optional[dict[str, str]] = None) -> str:
    hdrs = headers if headers is not None else {
        "User-Agent": UA, "Accept": accept, "Accept-Language": "en-US,en;q=0.9"}
    req = urllib.request.Request(_public_url(url), data=data, headers=hdrs)
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            raw = r.read(2_000_000)
            charset = r.headers.get_content_charset() or "utf-8"
            return raw.decode(charset, "replace")
    except urllib.error.HTTPError as e:
        raise ToolError(f"HTTP {e.code} from {urllib.parse.urlparse(url).hostname}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ToolError(f"could not fetch: {getattr(e, 'reason', e)}") from None


class _Text(html.parser.HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "nav", "footer", "header", "form"}
    BLOCK = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "tr", "section", "article",
             "pre", "blockquote"}

    NAV_ROLES = {"navigation", "banner", "contentinfo", "search", "complementary"}
    VOID = {"br", "img", "input", "meta", "link", "hr", "wbr", "source", "col", "area"}

    def __init__(self) -> None:
        super().__init__()
        self.out: list[str] = []
        self.main: list[str] = []            # text inside <main>/<article>
        self.in_main = 0
        self.skip = 0
        self.stack: list[tuple[str, str]] = []
        self.title = ""
        self._in_title = False

    def _emit(self, s: str) -> None:
        self.out.append(s)
        if self.in_main:
            self.main.append(s)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "title":
            self._in_title = True
        if tag in self.VOID:
            if tag == "br":
                self._emit("\n")
            return
        if tag in ("main", "article") or a.get("role") == "main":
            self.in_main += 1
            self.stack.append(("main", tag))
        elif tag in self.SKIP or a.get("role") in self.NAV_ROLES:
            self.skip += 1
            self.stack.append(("skip", tag))
        else:
            self.stack.append(("", tag))
        if not self.skip and (tag in self.BLOCK or tag in ("h1", "h2", "h3")):
            self._emit("\n## " if tag in ("h1", "h2", "h3") else "\n")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        # Unwind to the matching open tag; tolerant of sloppy HTML.
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][1] == tag:
                for kind, _ in self.stack[i:]:
                    if kind == "skip" and self.skip:
                        self.skip -= 1
                    elif kind == "main" and self.in_main:
                        self.in_main -= 1
                del self.stack[i:]
                break

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self.skip:
            self._emit(data)

    def text(self) -> str:
        main = "".join(self.main)
        t = main if len(main.strip()) > 200 else "".join(self.out)
        t = re.sub(r"[ \t\r\f\v]+", " ", t)
        t = re.sub(r"^## ?\s*$", "", t, flags=re.M)          # empty headings
        return re.sub(r"\n\s*\n+", "\n\n", t).strip()


def html_to_text(page: str) -> tuple[str, str]:
    p = _Text()
    p.feed(page)
    return p.title.strip(), p.text()


def web_search(query: str, max_results: str = "6") -> str:
    """DuckDuckGo's no-JavaScript results page: free, no key."""
    n = max(1, min(int(max_results or 6), 10))
    page = _http("https://html.duckduckgo.com/html/",
                 data=urllib.parse.urlencode({"q": query}).encode())
    results = []
    strip = lambda s: html.unescape(re.sub(r"<[^>]+>", "", s)).strip()
    # One chunk per result, so a missing snippet can't shift the pairing.
    for block in re.split(r'<div[^>]+class="[^"]*\bresult\b[^"]*"', page)[1:]:
        m = re.search(r'class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not m:
            continue
        href, title = m.group(1), m.group(2)
        sn = re.search(r'class="result__snippet"[^>]*>(.*?)</a>', block, re.S)
        snippet = sn.group(1) if sn else ""
        q = urllib.parse.parse_qs(urllib.parse.urlparse(html.unescape(href)).query)
        url = q.get("uddg", [html.unescape(href)])[0]
        if "duckduckgo.com/y.js" in url:            # sponsored result
            continue
        results.append(f"{len(results) + 1}. {strip(title)}\n   {url}\n   {strip(snippet)[:240]}")
        if len(results) >= n:
            break
    if not results:
        raise ToolError("no results (the search service may be rate-limiting; try "
                        "wikipedia or arxiv_search, or rephrase)")
    return "\n".join(results)


def fetch_url(url: str, max_chars: str = "6000") -> str:
    page = _http(url)
    title, text = html_to_text(page) if "<" in page[:500] else ("", page)
    limit = max(500, min(int(max_chars or 6000), 12000))
    return clip(f"# {title or url}\n{url}\n\n{text}", limit)


def wikipedia(query: str) -> str:
    base = "https://en.wikipedia.org/w/api.php?"
    s = json.loads(_http(base + urllib.parse.urlencode(
        {"action": "query", "list": "search", "srsearch": query, "srlimit": 3,
         "format": "json"}), accept="application/json"))
    hits = s.get("query", {}).get("search", [])
    if not hits:
        raise ToolError("no Wikipedia article found")
    title = hits[0]["title"]
    x = json.loads(_http(base + urllib.parse.urlencode(
        {"action": "query", "prop": "extracts", "explaintext": 1, "exintro": 0,
         "titles": title, "format": "json", "exchars": 5000}), accept="application/json"))
    page = next(iter(x.get("query", {}).get("pages", {}).values()), {})
    others = ", ".join(h["title"] for h in hits[1:])
    return clip(f"# {title}\nhttps://en.wikipedia.org/wiki/{urllib.parse.quote(title)}\n\n"
                f"{page.get('extract', '')}" + (f"\n\nRelated: {others}" if others else ""))


_ATOM = {"a": "http://www.w3.org/2005/Atom"}


_ARXIV_LOCK = threading.Lock()
_ARXIV_LAST = [0.0]


def arxiv_search(query: str, max_results: str = "5") -> str:
    """arXiv's API asks for >= 3 s between calls and answers faster callers with
    HTTP 406, so calls are paced and a throttled one is retried."""
    n = max(1, min(int(max_results or 5), 10))
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9.-]*", query)[:8]
    if not words:
        raise ToolError("give some search words")
    # Literal "all:" - arXiv rejects the percent-encoded colon.
    q = "+AND+".join("all:" + urllib.parse.quote(w) for w in words)
    url = (f"https://export.arxiv.org/api/query?search_query={q}"
           f"&max_results={n}&sortBy=relevance")
    with _ARXIV_LOCK:
        wait = 3.5 - (time.time() - _ARXIV_LAST[0])
        if wait > 0:
            time.sleep(wait)
        _ARXIV_LAST[0] = time.time()
        try:
            xml = _http(url, headers={"User-Agent": "ContextOS/0.2 (research agent)"})
        except ToolError:
            # The API throttles with HTTP 406 and retrying only prolongs it, so
            # find papers through the web search instead.
            return _arxiv_via_search(query, n)
    root = ET.fromstring(xml)
    out = []
    for e in root.findall("a:entry", _ATOM):
        aid = (e.findtext("a:id", "", _ATOM) or "").rsplit("/abs/", 1)[-1]
        title = " ".join((e.findtext("a:title", "", _ATOM) or "").split())
        summ = " ".join((e.findtext("a:summary", "", _ATOM) or "").split())
        date = (e.findtext("a:published", "", _ATOM) or "")[:10]
        out.append(f"{len(out) + 1}. [{aid}] {title} ({date})\n   {summ[:350]}…")
    if not out:
        raise ToolError("no arXiv papers found")
    return "\n".join(out)


def _arxiv_via_search(query: str, n: int) -> str:
    hits = web_search(f"site:arxiv.org {query}", str(min(10, n * 2)))
    out: list[str] = []
    seen: set[str] = set()
    for block in re.split(r"\n(?=\d+\. )", hits):
        m = re.search(r"arxiv\.org/(?:abs|pdf|html)/(\d{4}\.\d{4,5})", block)
        if not m or m.group(1) in seen:
            continue
        seen.add(m.group(1))
        first, _, rest = block.partition("\n")
        title = re.sub(r"^\d+\. ", "", first)
        snippet = rest.strip().split("\n", 1)[-1].strip()[:300]
        out.append(f"{len(out) + 1}. [{m.group(1)}] {title}\n   {snippet}")
        if len(out) >= n:
            break
    if not out:
        raise ToolError("no arXiv papers found (the arXiv API is rate-limiting right now)")
    return "\n".join(out) + "\n(found via web search; the arXiv API was busy)"


def arxiv_read(paper_id: str, max_chars: str = "8000") -> str:
    pid = re.sub(r"^.*?(\d{4}\.\d{4,5}(v\d+)?).*$", r"\1", paper_id.strip())
    if not re.fullmatch(r"\d{4}\.\d{4,5}(v\d+)?", pid):
        raise ToolError("give an arXiv id like 2405.15793")
    limit = max(1000, min(int(max_chars or 8000), 15000))
    try:                                   # full text, when arXiv has an HTML version
        return fetch_url(f"https://arxiv.org/html/{pid}", str(limit))
    except ToolError:
        return fetch_url(f"https://arxiv.org/abs/{pid}", str(limit))


# -------------------------------------------------------------- security
_RULES: list[tuple[str, str, str]] = [
    # (severity, regex, explanation)
    ("high", r"""(?i)(api[_-]?key|secret|password|token)\s*[:=]\s*['"][^'"\s]{12,}['"]""",
     "hard-coded secret; read it from an environment variable"),
    ("high", r"\b(AKIA[0-9A-Z]{16}|sk-[A-Za-z0-9]{20,}|gsk_[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{30,})",
     "credential-shaped string committed to code"),
    ("high", r"\beval\s*\(|\bexec\s*\(", "eval/exec runs arbitrary code"),
    ("high", r"shell\s*=\s*True", "subprocess with shell=True allows command injection"),
    ("high", r"\bos\.system\s*\(|\bos\.popen\s*\(", "shell command from Python; use subprocess with a list"),
    ("high", r"""\.execute\s*\(\s*(f['"]|['"][^'"]*['"]\s*(%|\+|\.format))""",
     "SQL built from strings; use parameters (?, %s)"),
    ("medium", r"\bpickle\.loads?\s*\(", "unpickling untrusted data runs code"),
    ("medium", r"yaml\.load\s*\((?![^)]*SafeLoader)", "yaml.load without SafeLoader"),
    ("medium", r"verify\s*=\s*False", "TLS certificate checks disabled"),
    ("medium", r"hashlib\.(md5|sha1)\s*\(", "weak hash; don't use for passwords or security"),
    ("medium", r"debug\s*=\s*True", "debug mode left on"),
    ("low", r"""['"]0\.0\.0\.0['"]""", "listens on every network interface"),
    ("low", r"\brandom\.(random|randint|choice)\s*\(", "not cryptographically secure; use secrets for tokens"),
]


def security_scan(ws: Workspace, path: str = ".") -> str:
    base = ws.path(path)
    findings: list[str] = []
    for p in ([base] if base.is_file() else base.rglob("*")):
        if (not p.is_file() or any(x in SKIP_DIRS for x in p.parts)
                or p.suffix not in (".py", ".js", ".ts", ".env", ".json", ".yml", ".yaml",
                                    ".toml", ".cfg", ".ini", ".sh", ".html")):
            continue
        try:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for i, ln in enumerate(lines, 1):
            if ln.lstrip().startswith("#"):
                continue
            for sev, rx, why in _RULES:
                if re.search(rx, ln):
                    findings.append(f"[{sev}] {ws.rel(p)}:{i}: {why}\n        {ln.strip()[:140]}")
    extra = []
    for mod, args in (("bandit", ["-q", "-r", ".", "-f", "txt", "-x", ".venv,venv,tests"]),
                      ("pip_audit", ["-r", "requirements.txt"] if (ws.root / "requirements.txt").exists() else None)):
        if args is None:
            continue
        try:
            import importlib.util
            if importlib.util.find_spec(mod) is None:
                continue
            r = subprocess.run([sys.executable, "-m", mod, *args],
                               cwd=ws.root, capture_output=True, text=True, timeout=180)
            extra.append(f"--- {mod} ---\n{(r.stdout or r.stderr).strip()[-1500:]}")
        except (subprocess.TimeoutExpired, OSError):
            pass
    head = (f"{len(findings)} finding(s) from the built-in rules"
            + ("" if extra else " (install bandit / pip-audit for deeper checks)"))
    return clip("\n".join([head, *findings, *extra]) if findings or extra else head + " - clean")


# ----------------------------------------------------------------- registry
def builtin_tools(ws: Workspace) -> dict[str, Tool]:
    t = [
        Tool("list_files", "List files in the workspace.",
             {"path?": "folder, default .", "depth?": "levels, default 3"}, ws.list_files),
        Tool("read_file", "Read a file with line numbers, 200 lines at a time.",
             {"path": "file path", "start?": "first line, default 1",
              "lines?": "how many, default 200"}, ws.read_file),
        Tool("search_code", "Regex search across workspace files.",
             {"pattern": "regex", "path?": "folder or file"}, ws.search_code),
        Tool("write_file", "Create or overwrite a whole file.",
             {"path": "file path", "content": "full file content"}, ws.write_file, "write"),
        Tool("edit_file", "Replace one exact, unique piece of text in a file. Rejected if "
             "it would break Python/JSON syntax.",
             {"path": "file path", "old": "exact existing text", "new": "replacement"},
             ws.edit_file, "write"),
        Tool("run_command", "Run a shell command in the workspace (tests, scripts). Needs "
             "the user's approval.", {"command": "command line", "timeout?": "seconds, default 120"},
             ws.run_command, "exec"),
        Tool("web_search", "Search the web (DuckDuckGo).",
             {"query": "search terms", "max_results?": "default 6"}, web_search, "network"),
        Tool("fetch_url", "Read a public web page as text.",
             {"url": "http(s) URL", "max_chars?": "default 6000"}, fetch_url, "network"),
        Tool("wikipedia", "Read the most relevant Wikipedia article.",
             {"query": "topic"}, wikipedia, "network"),
        Tool("arxiv_search", "Search research papers on arXiv.",
             {"query": "topic", "max_results?": "default 5"}, arxiv_search, "network"),
        Tool("arxiv_read", "Read an arXiv paper by id (full text when available).",
             {"paper_id": "e.g. 2405.15793", "max_chars?": "default 8000"}, arxiv_read, "network"),
        Tool("security_scan", "Scan workspace code for common vulnerabilities.",
             {"path?": "folder or file, default ."}, lambda path=".": security_scan(ws, path)),
    ]
    return {x.name: x for x in t}


def call_tool(tool: Tool, args: dict[str, Any]) -> str:
    """Validate args against the tool's params and run it. Errors become text."""
    allowed = {k.rstrip("?"): k.endswith("?") for k in tool.params}
    missing = [k for k, opt in allowed.items() if not opt and k not in args]
    if missing:
        raise ToolError(f"{tool.name} needs: {', '.join(missing)}")
    unknown = [k for k in args if k not in allowed]
    if unknown:
        raise ToolError(f"{tool.name} has no argument(s): {', '.join(unknown)}")
    kwargs = {k: (v if isinstance(v, str) else json.dumps(v) if not isinstance(v, (int, float))
                  else str(v)) for k, v in args.items()}
    return clip(tool.fn(**kwargs))
