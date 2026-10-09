# Research, design and benchmark

[← Back to README](../README.md)

**Contents:** [Why this is not another memory system](#why-this-is-not-another-memory-system) · [Prior art](#against-the-prior-art) · [The benchmark](#the-benchmark) · [Design](#design) · [Live evaluation](#live-cross-model-evaluation) · [Honest limits](#honest-limits) · [References](#references)

---

## Why this is not another memory system

The original idea was *"never lose context — carry everything to the next model."*
The research says that is the worst available option.

**arXiv 2608.24358, *The Handoff Tax*** — 58,000 agent runs, 2M API calls, 36B tokens,
on SWE-bench Verified with Claude Haiku 4.5 / Opus 4.7 and GPT-5.6 Luna / Sol:

| Interface | Escalation (weak → strong) | Downshift (strong → weak) |
|---|---|---|
| Raw full trajectory | **47%** / 36% quality-gap recovery, at **4.0×** / 6.1× the cost of an LC-only run | 50–79% |
| `traj-drop` — no trajectory, working-tree edits kept | **64%** / **84%** | collapses to 28% / 53% |
| `compact_pre` — departing model summarises first (Claude) | cost $1.61 → $0.75, quality 47% → 60% | — |

Two things follow, and they are the whole design:

1. **Dropping the trajectory beats carrying it — when escalating.** It works because the
   *working tree* survives. Nobody had built the state layer that lets non-file state —
   decisions, constraints, blockers — survive the same way. That is this project.
2. **The right packet depends on direction.** A system that ships one format is provably
   wrong in one of the two directions.

**arXiv 2608.21208, *Specification Portability*** — 1,802 Oracle→PostgreSQL scripts across
Kiro, Gemini, Copilot: worst cross-agent transfer scored Token F1 **0.035** and 2.33% SQL
validity. Agent-authored artifacts are *not* agent-neutral. Retrieval-augmented ingestion
sat on the Pareto frontier; **compression gave no universal benefit**. So ContextOS
*selects* verbatim units rather than summarising them, and renders packets into a neutral
schema rather than one agent's idiom.

### Against the prior art

| System | What it does | Benchmarked on | Cross-model handoff |
|---|---|---|---|
| MemGPT / Letta | OS-style paging between main and external context | DMR, doc QA | no |
| Mem0 | LLM extraction + ADD/UPDATE/DELETE/NOOP consolidation | LOCOMO | no |
| Zep / Graphiti | bi-temporal knowledge graph, 115k → 1.6k tokens (LongMemEval context) | DMR, LongMemEval | no |

All three solve conversational recall and are measured on it. ContextOS borrows Zep's
bi-temporal invalidation and Mem0's NOOP-on-identical-write, and points them at a
different problem.

---

## The benchmark

Replicating the handoff-tax study needs SWE-bench and a five-figure API budget. This
harness measures the **necessary condition** underneath it, offline and deterministically:

> **State sufficiency @ budget** — after a handoff at step *k*, does the transferred
> context contain every unit the remaining steps provably depend on?

A required unit that is missing must be re-derived or guessed, which is the mechanism
behind the measured quality drop. Sufficiency is an upper bound on handoff quality and
needs no model calls. A unit counts as delivered only if its **content survives verbatim**
— counting a mention of its address would let a lossy summariser score full marks while
having thrown the fact away.

`python -m contextos.bench --direction lateral`, 36 runs per strategy across 3 tasks,
every interior handoff point, budgets 800/1500/3000:

| strategy | recall | precision | sufficiency | mean tokens | over budget |
|---|---|---|---|---|---|
| full_replay | 63.0% | 7.0% | 38.9% | 1614 | 5.6% |
| recency | 27.0% | 2.2% | 13.9% | 1609 | 2.8% |
| summary | 86.8% | 66.4% | 75.0% | 1422 | 0.0% |
| **contextos** | **100.0%** | 64.6% | **100.0%** | **464** | 0.0% |

Full sufficiency at roughly a third of the tokens of the best baseline.

The direction asymmetry reproduces, which is the point of the design:

| direction | recall | + loss manifest | precision | mean tokens |
|---|---|---|---|---|
| escalate | 90.0% | **100.0%** | 62.3% | 465 |
| downshift | 100.0% | 100.0% | 19.1% | 1602 |
| lateral | 100.0% | 100.0% | 64.6% | 464 |

Escalation drops low-importance residue and loses 10% of required units outright — and the
loss manifest recovers all of it, because the receiving model is *told* what is missing and
can fetch it by address. That gap is the manifest earning its place.

---

## Design

| # | Decision | Grounded in |
|---|---|---|
| D1 | Direction-aware packets (`escalate` / `downshift` / `lateral`) | traj-drop wins escalation, harms downshift |
| D2 | Write-time commit — agents `put()` as they work | `compact_pre` beat post-hoc summarising |
| D3 | Retrieval, not compression — units stored verbatim | "compression gave no universal benefit" |
| D4 | Artifacts first-class: path + sha256, never a prose summary | traj-drop works *because* the working tree survives |
| D5 | Loss manifest — every omitted address is declared, with a fetch handle | silent omission is selective retrieval's dominant failure |
| D6 | Bi-temporal provenance + conflict detection | Zep's invalidation model |
| D7 | Difficulty-gated migration | "on easy tasks all escalation interfaces underperform" |
| D8 | Packets render to a neutral schema | specs are not agent-neutral (F1 0.035) |

**Budgets are enforced on the rendered packet**, not on the sum of unit tokens. Headings,
notes and the manifest are real context the receiver pays for; budgeting the units alone
and then rendering is how a system quietly ships 2× its stated budget. Structural units
(goal, constraints, open blockers, pinned) are never evicted — if they alone exceed the
budget the packet says so instead of silently truncating.

### Address space

```
/user/…      preferences, profile          /agent/…     per-agent scratch state
/project/…   architecture, decisions        /tool/…      tool results (chatter)
/task/…      goal, progress, blockers       /artifact/…  real files: path + sha256
```

### Retrieval

Four independent rankers fused by Reciprocal Rank Fusion (k=60) — BM25 scores and
address-overlap scores live on incompatible scales, so fusing by *rank* avoids
calibration entirely:

1. **address router** — query terms matched against address segments (deterministic)
2. **BM25** — SQLite FTS5 over address + value
3. **intent** — query→kind mapping, so *"what is blocking us"* surfaces blockers even
   though no blocker's text contains the word "blocking"
4. **prior** — importance × confidence × recency (7-day half-life)

Embeddings are deliberately out of scope for v1: address routing covers the exact-match
path a vector index handles badly, and BM25 covers the rest. Add them only if the
benchmark shows a gap.

### Two-lane routing (added for the free-model chat app)

Every provider contributes two routes sharing one key: its strongest free model (smart
lane) and its quickest (fast lane). A deterministic 0–1 difficulty score — no model call
spent on it — picks the lane, and the same score is the `difficulty` fed to the D7 gate.
A lane with nothing working spills into the other, so a lane switch is an ordinary
escalate/downshift handoff. Settings: [CONFIGURATION.md](CONFIGURATION.md#routing).

---

## Pipeline mode, attachments, local-only

**Pipeline mode** (`/pipeline <prompt>` or the Pipeline lane) splits one prompt into at most
six subtasks, routes each to the lane that suits it, runs independent ones in parallel and
merges. It is a composition of published ideas, not a new result:

| Idea | Source | Used for |
|---|---|---|
| Plan, run in parallel, join | LLMCompiler, Kim et al., arXiv 2312.04511: up to 3.7x lower latency than ReAct | dependency waves; independent subtasks run concurrently |
| Send easy work to cheap models | FrugalGPT, Chen, Zaharia, Zou, arXiv 2305.05176: up to 98% cost cut at GPT-4 quality; RouteLLM, Ong et al., arXiv 2406.18665: over 2x cost cut in some settings | `extract/summarize/format` go to the fast lane; `reason/code/math/write` to the smart lane. Here the cost is free-tier quota |
| One aggregator reads several outputs | Mixture-of-Agents, Wang et al., arXiv 2406.04692: open-model MoA 65.1% vs GPT-4o 57.5% on AlpacaEval 2.0 | the merge step |

Each worker sees only the outputs of the subtasks it depends on, never the transcript: the
same carry-state-not-trajectory rule as the handoff packet. The numbers above are the papers'
own and were measured in their settings. They have **not** been reproduced on ContextOS, and
Pipeline mode has no benchmark yet; the tests check routing, ordering and failure handling
with scripted models, not answer quality. A bad planner reply falls back to a deterministic
split, and a failed subtask is reported rather than hidden.

**Attachments** are converted to text once, stored as `/artifact/uploads/...` (path + sha256,
D4) and carried like any other state, so an image described by one vision model is usable by
every other model in the pool. Images need a free Gemini key; PDFs need the optional `pypdf`
package; everything else is decoded directly. Files are capped (4 per message, 6 MB each,
24k characters kept) and fenced as data the model must not obey.

**Local-only mode** (`LLM_LOCAL_ONLY=1`) restricts routing to models served on this machine
(Ollama) and disables cloud image description, so nothing leaves the computer.

---

## Live cross-model evaluation

`bench.py` measures state sufficiency — a necessary condition, offline. `live.py`
measures what sufficiency only bounds: **does the task still come out right after the
model changes?**

Each task is built so a constraint established early *changes the correct answer*. Lose
it at the handoff and the model produces a specific, predictable wrong number — so "did
the context survive" is directly observable in the output, with no LLM judge.

| task | correct | answer when a constraint was lost |
|---|---|---|
| invoice-total | 2754.00 (discount then tax) | 3240.00 (discount dropped) |
| capacity-plan | 6 nodes (headroom + standby) | 4 nodes |
| release-date | 2026-03-09 (soak, no-Friday) | 2026-03-07 |

```bash
python -m contextos.live --list-providers        # which keys .env actually has
python -m contextos.live --dry-run               # verify the harness, no API calls
python -m contextos.live --a groq --b cloudflare --repeats 3
```

Any provider name from [CONFIGURATION.md](CONFIGURATION.md#providers-and-models) works for
`--a` / `--b`. Keys are read from `.env`; `--dry-run` needs none. Nothing that contains a
key is ever printed or logged.

Harness self-test (`--dry-run`, model B simulated as one that applies exactly the
constraints it receives — **not** a result, a check that the experiment is wired right):

| interface | solved | transfer tokens |
|---|---|---|
| raw (full trajectory) | 100% | 842 |
| traj_drop | **0%** | 28 |
| summary | 100% | 835 |
| contextos | 100% | **385** |

`traj_drop` scoring zero is the thesis in one line: it is the paper's *best* escalation
interface, and it fails here because it preserves only the working tree — and a
constraint is not a file. ContextOS matches full-trajectory correctness at 46% of the
tokens by carrying the constraint as addressable state instead.

### What real models showed in the chat app

Separately from `live.py`, the chat app was exercised against real free models:

- A three-turn price calculation stayed correct across a forced handoff (Groq → Cloudflare)
  and after the chat history was wiped, with the new model working from the store alone.
- A Compact "Continue in another AI" export, pasted into three other models with no
  ContextOS prompt, let each answer follow-up questions correctly from the export alone.

These are spot checks, not a benchmark.

---

## Honest limits

- Sufficiency is a **necessary, not sufficient** condition for handoff quality. It does not
  prove a task completes; it proves the next model was not starved of state.
- The three benchmark tasks are hand-authored with declared ground-truth dependencies.
  They are auditable but synthetic, and the suite is small.
- The `live.py` evaluation table above is the `--dry-run` self-test. A full multi-repeat
  live run against real models has not been published yet — run it with your own keys.
- The three live tasks are arithmetic/date reasoning, not software engineering. They
  isolate constraint survival cleanly, which is the point, but they are not SWE-bench.
- Some state genuinely does not survive a handoff: the departing model's half-formed plan,
  its implicit commitments, its reasoning trace. ContextOS carries declared state. It does
  not carry intent, and does not claim to.
- Retrieval is lexical. It will miss a paraphrase that shares no vocabulary with the stored
  unit.
- In the chat app, saving state depends on the model writing its `<context>` block. When
  a model skips it, the user's own message is saved instead so inputs are never lost.

## References

- [The Handoff Tax: Continuing Non-Native Trajectories in LLM Agents](https://arxiv.org/abs/2608.24358) — arXiv 2608.24358
- [Specification Portability Across LLM Development Agents](https://arxiv.org/abs/2608.21208) — arXiv 2608.21208
- [MemGPT: Towards LLMs as Operating Systems](https://arxiv.org/abs/2310.08560) — arXiv 2310.08560
- [Mem0: Building Production-Ready AI Agents with Scalable Long-Term Memory](https://arxiv.org/abs/2504.19413) — arXiv 2504.19413
- [Zep: A Temporal Knowledge Graph Architecture for Agent Memory](https://arxiv.org/abs/2501.13956) — arXiv 2501.13956
- [LLMCompiler](https://arxiv.org/abs/2312.04511), [FrugalGPT](https://arxiv.org/abs/2305.05176), [RouteLLM](https://arxiv.org/abs/2406.18665), [Mixture-of-Agents](https://arxiv.org/abs/2406.04692)
- [Model Context Protocol specification 2026-07-28](https://modelcontextprotocol.io/specification/2026-07-28/server/tools)
