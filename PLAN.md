# ContextOS — Technical Plan (research-grounded)

Version 2. This plan supersedes the original pitch. Two 2026 papers changed the design;
the changes are marked **[REVISION]** with the evidence that forced them.

---

## 1. What the research actually says

### 1.1 The Handoff Tax (arXiv 2608.24358)
58,000 agent runs, 2M API calls, 36B tokens, on SWE-bench Verified (+ LiC, BrowseComp).
Models: Claude Haiku 4.5 / Opus 4.7, GPT-5.6 Luna / Sol. Handoffs injected at
difficulty-calibrated percentiles (5th–50th) of step-count distribution.

Findings that bear directly on this project:

| Finding | Number | Consequence for ContextOS |
|---|---|---|
| Raw full-trajectory handoff, escalation (weak→strong) | recovers only **47%** (Claude) / **36%** (GPT) of the quality gap, at **4.0×** / **6.1×** the cost of an LC-only run | "Never lose context" — passing everything — is the **worst** interface. |
| `traj-drop` (discard trajectory, keep **working-tree edits**) | recovers **64%** (Claude) / **84%** (GPT) | Dropping the conversation but keeping durable artifacts is the best escalation interface. |
| `compact_pre` (outgoing model summarizes before handoff; Claude) | cost $1.61 → $0.75, quality 47% → 60% | State should be committed **by the departing agent, at write time**. |
| Downshift (strong→weak) with trajectory removed | drops to **28%** (Claude) / **53%** (GPT) vs 50–79% with trajectory intact | The correct packet is **direction-dependent**. One format is wrong. |
| Carrying a weak model's trajectory into a strong model | each post-handoff step costs **2.2×** (Claude) / **1.6×** (GPT) more than with compact_pre | Trajectory is not just useless on escalation — it is actively expensive. |
| On easy tasks, all escalation interfaces underperform | — | Migration must be **difficulty-gated**, not fired on every quota error. |
| For Claude escalation, restarting from scratch beat continuing | — | Honest baseline. ContextOS must beat *restart*, not just *raw handoff*. |

### 1.2 Specification Portability (arXiv 2608.21208)
1,802 Oracle→PostgreSQL scripts; Kiro, Gemini, Copilot (+ Claude Code, Cursor).

- Worst cross-agent transfer (Gemini reading Kiro's spec): Token F1 **0.035**, SQL validity **2.33%**, AST similarity **0.015**. Agent-authored artifacts are **not agent-neutral**.
- **Retrieval-augmented ingestion sat on the Pareto frontier** for both Gemini and Copilot.
- **Compression provided no universal benefit.**
- Rewriting/normalizing specifications substantially improved transfer.

### 1.3 Prior art — and the gap
| System | What it is | Benchmarks | Cross-model handoff? |
|---|---|---|---|
| MemGPT / Letta (2310.08560) | OS-style memory hierarchy: main context (RAM) vs external context (disk), paged by function calls under memory-pressure warnings. 32.1% → 92.5% on deep memory retrieval (GPT-4, Table 2) | DMR, doc QA | No |
| Mem0 (2504.19413) | LLM extraction + ADD/UPDATE/DELETE/NOOP consolidation; graph variant. ~1.8k vs ~26k tokens per query (Table 2); **91% lower p95 latency** | LOCOMO | No |
| Zep / Graphiti (2501.13956) | Bi-temporal knowledge graph (episode / entity / community); `t_valid`,`t_invalid` invalidation; cosine + BM25 + BFS, then rerank. 94.8% (gpt-4-turbo) / 98.2% (gpt-4o-mini) DMR; 115k → **1.6k** tokens (LongMemEval) | DMR, LongMemEval | No |

**All three are conversational-memory systems, evaluated on conversational recall.**
None targets agentic task handoff across models. LangGraph checkpoints persist state but
are coupled to a graph definition and a framework. *(Note: LangGraph checkpoint internals
could not be verified from primary docs — treat as unconfirmed.)*

> **The gap: `traj-drop` is the best escalation interface, and it works because
> working-tree edits survive. Nobody has built the state layer that makes traj-drop
> viable for state that isn't a file — decisions, constraints, blockers, tool results.**

---

## 2. Revised thesis

**ContextOS is not a memory system. It is the state layer that makes trajectory-drop
handoffs work.**

Old pitch: *"Never lose context — carry everything to the next model."*
The handoff-tax paper measures that as the worst option.

New pitch: **"Drop the trajectory. Keep the state. Hand over only what the next model
provably needs — and tell it what you didn't send."**

---

## 3. Design decisions (each traceable to evidence)

| # | Decision | Evidence |
|---|---|---|
| D1 | **Direction-aware packets.** `escalate` → goal + constraints + artifacts + blockers, no trajectory. `downshift` → adds decisions with rationale + prior guidance. `lateral` → middle. | traj-drop wins escalation (64/84%), harms downshift (28/53%) |
| D2 | **Write-time commit.** Agents `put()` state as they work; the packet is assembled from committed units, not reconstructed post-hoc. | `compact_pre` beat post-hoc: cost −53%, quality +13pts |
| D3 | **Retrieval, not compression.** Units stored verbatim and *selected*. Compression is a last-resort fallback and is flagged lossy. | "Compression provided no universal benefit"; retrieval on Pareto frontier |
| D4 | **Artifacts are first-class.** `/artifact/**` units reference real paths + content hashes, never prose summaries of files. | traj-drop works *because* working-tree edits persist |
| D5 | **Loss manifest.** Every packet lists addresses that existed and were **excluded**, with a `fetch` handle to pull them. The receiving model is told what it does not have. | Silent omission is the dominant failure mode of selective retrieval |
| D6 | **Bi-temporal provenance + conflict detection.** `valid_from`/`valid_to`, version, source; contradicting writes to one address raise a conflict rather than silently stacking. | Zep's invalidation model |
| D7 | **Difficulty-gated routing.** Escalate only when the task is hard enough to pay for it; never on easy tasks. | "On easy tasks, all escalation interfaces underperform" |
| D8 | **Normalize at handoff.** Packets render to a neutral schema, not the source agent's idiom. | Specs are not agent-neutral (F1 0.035 worst case) |

---

## 4. Data model

Address grammar: `/<root>/<segment>...`, roots fixed to
`user | project | task | agent | tool | artifact`.

```
Unit:
  address      TEXT PRIMARY KEY     -- /project/architecture/database
  value        TEXT                 -- verbatim, never pre-summarized
  kind         decision|constraint|fact|goal|blocker|artifact|tool_result|preference
  source       TEXT                 -- agent/model id that wrote it
  lifetime     permanent|task|ephemeral
  importance   REAL 0..1
  confidence   REAL 0..1
  valid_from   REAL   valid_to REAL NULL     -- semantic validity (bi-temporal)
  created_at   REAL   updated_at REAL
  version      INT
  tokens       INT
  pinned       INT
  meta         JSON   -- path, sha256, line range for artifacts
```
`unit_history` keeps every prior version (provenance is append-only).
`conflicts` records contradicting writes. `units_fts` is an FTS5 mirror for BM25.

## 5. Retrieval — hybrid, fused by RRF

Three independent rankers, fused with Reciprocal Rank Fusion (k=60):
1. **Address router** — query terms matched against address segments. Deterministic, exact.
2. **BM25** — SQLite FTS5 over `address + value`.
3. **Prior** — importance × recency × confidence.

`score = Σ 1/(k + rank_i)`, then hard rules: pinned always in; superseded (`valid_to` set)
never in unless explicitly asked.

## 6. Budgeting

Tiered fill, not greedy-by-density:
1. Tier 0 (never dropped): goal, active constraints, pinned.
2. Tier 1: artifacts touched this task.
3. Tier 2: retrieved units by fused score, by value/token density.
4. Overflow → **loss manifest**, not truncation.

Token counting: `tiktoken` when available, else a calibrated `chars/4` estimator.

## 7. Handoff packet

```json
{ "schema": "contextos/handoff/1",
  "direction": "escalate|downshift|lateral",
  "goal": "...", "constraints": [...], "artifacts": [...],
  "decisions": [...],            // downshift only
  "blockers": [...], "next_step": "...",
  "omitted": [{"address": "...", "kind": "...", "tokens": 812, "why": "budget"}],
  "fetch": "contextos.get(address)",
  "budget": {"stored": 48213, "selected": 3907, "sent": 3907} }
```
Rendered to neutral Markdown for the receiving model (D8).

## 8. Benchmark — the real contribution

Full SWE-bench replication is out of scope. We measure the **necessary condition** for a
successful handoff, without any API key:

> **State sufficiency @ budget** — after a handoff at step *k*, does the packet contain
> every unit the remaining steps provably depend on?

Each synthetic task declares ground-truth dependencies per step. Metrics:
`recall_required`, `precision`, `tokens_sent`, `sufficient` (all required present).

Baselines: **full replay** (send everything), **recency window**, **naive summary**
(lossy compressor), **ContextOS**. An optional `--live` mode scores real task completion
if API keys are present.

Claim we can defend: *not* "we beat SWE-bench", but *"under a fixed budget, ContextOS
delivers the required state where recency and summarization baselines drop it."*

## 9. Deliverables
`contextos/` package · MCP stdio server (spec **2026-07-28**) · CLI · benchmark harness ·
test suite · demo · README.

## 10. Explicitly out of scope (v1)
Embeddings/vector search (BM25 + address routing first; add only if benchmark shows a
gap), community summarization, distributed store, a UI.
