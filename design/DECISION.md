# ContextOS UI/UX: decision record

Status: **proposal, awaiting approval.** No UI code changed yet. Approve `FLOWS.md` and `DESIGN.md`, then the build starts one screen at a time.

## Inputs (assumed, please correct)
| Question | Assumption |
|---|---|
| Who, what job? | Students and solo builders on free AI tiers. Job: keep one task going while models fail, rate-limit and swap. Later: hand a whole project to Build mode. |
| Context of use | Daily, long sessions, local browser app, often a laptop or phone, patchy network, free-tier failures are normal. Stakes low to medium (lost work, wasted time). |
| Feel and constraints | Calm, trustworthy, "my memory, on my machine". Zero dependencies, no build step, works offline, single `dashboard.html`. Light and dark. |

## Profile (from the decision engine)
Project type: **AI chat / agent workspace** (matrix row). Family **1 editorial minimalism** with a **4 data-dense** remix for the memory ledger and Build timeline. Dials **VARIANCE 2 / MOTION 3 / DENSITY 6**.

## What the current UI gets wrong (audit of `contextos/dashboard.html`)
- **Looks like every chat clone.** Blue `#2563eb` accent, purple "smart" and cyan "fast" chips, system-ui only, one grey 1px border on everything. It is not distinguishable from any other AI chat at thumbnail size.
- **The product's one differentiator is hidden.** Memory and handoff are a small brain icon with a count and a warning-coloured notice line. Users cannot *see* that the conversation survived a model change.
- **Lane is encoded by colour alone** (purple vs cyan). Fails "never rely on colour alone".
- **Build mode is a log, not a project.** Phases and events are a flat list with a left border; feature progress, test results and the "needs you" moment do not stand out.
- **Settings surfaces are modal lists** (models, keys, connectors) with no sense of what is healthy right now.

## Chosen direction: "The Ledger"
Concept from the product's own world: *the transcript is for display, the store is the memory.* So the UI treats memory as a **ledger**: every fact, rule and decision is an entry that is visibly written as the chat goes on, and a model handoff is a visible **seam** in the thread showing what was carried across.

**Memorable element (one):** the **handoff seam**. When a model changes, a thin full-width rule appears in the thread: `model A  ⟶  model B · 9 entries carried`. Click it to open exactly those entries. It is the moment that proves the product works, and no other chat app has it.

- Type: serif display (titles, welcome, Build headings) + system sans for chat + mono for addresses, model ids and tests. Serif is a system-stack choice (Iowan Old Style / Palatino / Georgia) so nothing is downloaded.
- Colour: warm paper neutrals, ink text, **one accent**, deep teal-green `#0B6B5C` (dark: `#5FCDB5`). Teal is the "memory" colour: the seam, ledger entries, the accent on primary actions. No indigo, no purple gradients.
- Lane encoding: **shape + text**, not hue. Smart = filled pill "Smart", Fast = outlined pill "Fast".
- Depth: tonal layers, borders only where they separate regions. No coloured left-border stripes except test pass/fail rows in Build, where colour is doubled by icon and word.
- Motion: functional only. Ledger entry slides in and holds a 1.2 s teal tint when written; seam draws left to right once; Build feature rows fill on pass. Everything off under reduced motion and a **Hold still** toggle.

## Four directions considered
| # | Direction | Family | Verdict |
|---|---|---|---|
| A | **The Ledger** (chosen) | 1 x 4 | Best fit: daily use, reading + scanning, memory made visible, zero-cost to render. |
| B | Phosphor terminal | 2 | Strong identity, but hurts reading long answers and phone use; audience includes non-developers. Borrowed: mono for ids and tests. |
| C | Glass, soft-futurism | 7 | Blur and translucency cost on low-end laptops; generic "premium AI" look. Rejected. |
| D | Warm cream + terracotta | 3 | Reads as an imitation of the most common AI-app palette. Rejected. |

Scores (1-5): A first impression 4, distinctiveness 4, audience fit 5, familiarity 5, build cost 4, accessibility 5. B: 4, 5, 3, 3, 3, 3. C: 4, 2, 3, 4, 2, 2. D: 4, 1, 4, 5, 4, 4.

## Three voices, minority reports
- **Craft:** serif titles and tonal layers give calm authority; the seam is the only decoration. *Overruled:* wanted a custom display face; costs a download and breaks zero-dependency.
- **Researcher:** users will not read a memory panel, so the seam must show carried entries inline and be keyboard reachable. Adopted. *Concern kept:* teal + green success are close; success always carries a check icon and word.
- **Strategist:** the first-run screen must show the aha (memory surviving a switch), not a blank chat. Adopted as the "sample handoff" on the empty state.

## Ethics and hooks
No streaks, no fake urgency, no engagement nudges. The only "hook" is ability: first message works with zero setup when any key exists, and a guided key checklist when none does.

## Rejected and why (kept so we do not repeat it)
Custom web fonts (offline, dependencies), animated gradients (cost, slop tell), mascot (no fit), chat bubbles for the assistant (wastes width; assistant text stays unboxed).
