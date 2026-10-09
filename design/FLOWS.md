# ContextOS flows, wireframes and state matrix

Wireframes are ASCII, low fidelity. **Please approve or pick per screen before hi-fi.** Real copy is used throughout; sentence case, one word per action ("Copy", "Delete", "Stop").

## Sitemap
```
ContextOS
├─ Chat (default)          conversation + memory ledger + handoff seams
├─ Build                   goal → research → plan → features → security → report
├─ Setup (dialog → page)   Models · API keys · Connectors & skills
└─ Continue elsewhere      export memory (dialog)
```

## Flows
1. **First run:** open app → empty state with sample handoff → (no key? checklist) → first message → memory entries appear → success.
2. **Handoff (peak moment):** send → model fails mid-reply → seam appears → next model continues → user can open the seam to see carried entries. Ending: a quiet "Continued on <model>" with no alarm colours.
3. **Continue elsewhere:** Export → choose size → preview → Copy → toast "Copied. Paste it into any AI."
4. **Build:** New build → goal + options → research → plan (approve/edit) → features one at a time with tests → security review → report → Get project / Ask for changes. Peak: "Needs you" card. Ending: report with tests passed, project download.

## Screen 1: Chat (three options)

**A. Thread + right ledger (chosen).** Familiar chat, ledger docked right, collapsible.
```
+--------+---------------------------------------+------------------+
| ContextOS | Chat title                [Smart|Fast] Model v  ⇪  ▤ 12 | LEDGER  · 12 ×  |
| + New chat|                                      |------------------|
| + Build   |  You                                 | GOAL  Ship v1... |
| search    |     Explain the retry logic  ▢       | RULE  No deps    |
| Today     |                                      | DECISION SQLite  |
|  chat 1 ● |  Assistant (unboxed, 68ch)           | ...              |
|  chat 2   |  Retry uses exponential...           | [Preview handoff]|
|           |  ── Groq ⟶ Gemini · 9 carried ──     |                  |
| ● Models  |  ...continues                        |                  |
+-----------+ [ Message ContextOS…            ▲ ]  +------------------+
```
**B. Thread only, ledger as sheet.** Cleaner, but hides the differentiator. Rejected.
**C. Split: thread + persistent timeline strip of handoffs.** Interesting for long chats; adds a second scan path. Kept as a later idea.

Squint test (A): thread text, composer and the seam rule stand out; ledger recedes until written. Passes.

## Screen 2: Build (chosen: project board, not a log)
```
+--------+--------------------------------------------------------+
| sidebar| ◧ Habit tracker API              [running]   Stop      |
| builds |  Research ✓  Plan ✓  Build ●  Review ○  Report ○       |
| ● Habit|--------------------------+-----------------------------|
|   Todo |  FEATURES                | ACTIVITY (current feature)  |
|        |  ✓ Auth        6/6 tests | ▸ wrote routes.py           |
|        |  ● Habits CRUD 3/5 tests | ▸ ran pytest  2 failed      |
|        |  ○ Streak calc           | ▸ fixing…                   |
|        |  ○ Export                |                             |
|        |--------------------------+-----------------------------|
|        | ┌ Needs you: approve the plan  [Approve] [Edit] ┐      |
+--------+--------------------------------------------------------+
```
Phase stepper on top, feature list left (status by icon + word + test count), activity right. "Needs you" is the only card with an accent ring, one per screen.

## Screen 3: Setup
Single "Setup" dialog with three tabs: **Models** (health dot + last check), **API keys** (per provider checklist: 1 sign up, 2 paste, 3 test; state chip Saved/Missing/Failed), **Connectors & skills**. First-run with no keys opens Keys with the three fastest-to-get providers first.

## State matrix
| Component | Empty | Loading | Error | Success | Offline / limit |
|---|---|---|---|---|---|
| Thread | sample handoff + 4 starter prompts | skeleton lines + "Routing…" | what failed + Retry + which model is next | reply + lane pill + model | banner: "No connection. Ollama still works." |
| Reply | n/a | streaming caret, Stop | inline error card, Retry, "Try another model" | Copy / Regenerate | switches to next model, seam shown |
| Ledger | "Nothing remembered yet. Facts, rules and decisions show up here." | shimmer rows | "Couldn't read memory" + Retry | entry tint 1.2 s | works offline (local) |
| Handoff seam | n/a | "Handing off…" | "No model could continue" + Retry all | `A ⟶ B · n carried` | n/a |
| Models | "No keys yet" + checklist | test progress per row | row: reason + fix link | green check + latency | grey "Offline" chip |
| Export | n/a | preview skeleton | "Couldn't build export" + Retry | toast on copy | works offline |
| Build feature | pending ○ | running ● with pulse (static under reduce) | ✕ failed tests + Fix / Skip | ✓ n/n tests | "Paused: waiting for a model" |
| Long content | wrap, table scroll, code scroll, 68ch measure | | | | |
| Permission denied | key rejected: "Provider says this key is invalid. Paste a new one." | | | | |

## Responsive plan (mobile first)
- < 720 px: sidebar becomes a drawer; ledger becomes a bottom sheet; Build feature list stacks above activity with a segmented switch.
- Targets 44 px; composer sticks above the keyboard (`100dvh`).
- 720-1100 px: ledger overlays as a sheet. ≥ 1100 px: docked.

## Keyboard
`Ctrl+K` search, `Ctrl+Shift+O` new chat, `Ctrl+.` toggle ledger, `Esc` closes sheets, `/smart` `/fast` stay. Every icon button has a name; focus ring 2 px accent, 2 px offset.
