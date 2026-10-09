---
version: alpha
name: ContextOS Ultramarine Ledger
description: Calm editorial chat workspace that makes conversation memory visible.
colors:
  primary: "#2431E8"
  neutral: "#F4F2EC"
  surface: "#FFFFFF"
  sunken: "#EAE7DE"
  text: "#0E0F13"
  muted: "#52576A"
  border: "#DAD6CA"
  error: "#B42318"
  success: "#0B6B3A"
  warning: "#8A5300"
typography:
  display: { fontFamily: "Bricolage Grotesque, system-ui, sans-serif", fontSize: 32px, fontWeight: 600, lineHeight: 1.2, letterSpacing: -0.01em }
  title: { fontFamily: "Bricolage Grotesque, system-ui, sans-serif", fontSize: 24px, fontWeight: 600, lineHeight: 1.2 }
  heading: { fontFamily: "Geist, system-ui, sans-serif", fontSize: 18px, fontWeight: 600, lineHeight: 1.3 }
  body: { fontFamily: "Geist, system-ui, sans-serif", fontSize: 15px, fontWeight: 400, lineHeight: 1.6 }
  label: { fontFamily: "Geist, system-ui, sans-serif", fontSize: 13px, fontWeight: 600, lineHeight: 1.3 }
  caption: { fontFamily: "Geist, system-ui, sans-serif", fontSize: 12px, fontWeight: 400, lineHeight: 1.4 }
  mono: { fontFamily: "Geist Mono, ui-monospace, monospace", fontSize: 12px, fontWeight: 400, lineHeight: 1.5 }
rounded: { control: 8px, card: 12px, sheet: 16px, pill: 999px }
spacing: { xs: 4px, sm: 8px, md: 16px, lg: 24px, xl: 48px }
components:
  button-primary: { backgroundColor: "{colors.primary}", textColor: "#FFFFFF", rounded: "{rounded.control}", padding: 8px 16px }
  button-secondary: { backgroundColor: "{colors.surface}", textColor: "{colors.text}", rounded: "{rounded.control}", padding: 8px 16px }
  ledger-entry: { backgroundColor: "{colors.surface}", textColor: "{colors.text}", rounded: "{rounded.card}", padding: 8px 12px }
  handoff-seam: { textColor: "{colors.primary}", padding: 8px 0 }
---

> Colour and type were revised in the premium pass: see `PREMIUM.md`. Where this file and `PREMIUM.md` disagree, `PREMIUM.md` wins.

## Overview
Family 1 editorial minimalism with a family 4 data-dense ledger. Dials: variance 2, motion 3, density 6. Audience: students and solo builders using free AI tiers, daily, on laptops and phones. Feeling: calm, trustworthy, "my memory on my machine". **One memorable element: the handoff seam**, a full-width rule in the thread reading `Groq ⟶ Gemini · 9 entries carried`, clickable and keyboard reachable.

## Colors
- **Primary teal (`#0B6B5C`, dark `#5FCDB5`)** is the memory colour: primary actions, the seam, new ledger entries, focus ring. It is the only accent. One primary button per screen.
- **Neutrals** are warm paper (light) and warm graphite (dark). Depth comes from `neutral` → `surface` → `sunken`, not from borders.
- **Semantic** error, success, warning always pair with an icon and a word. Success is a check mark, so it is never confused with the teal accent.
- Contrast verified with `contrast.py`: text on background 17.0:1 light, 15.3:1 dark; muted 6.5:1; accent-ink on accent 6.4:1. Full set in `tokens.css`.

## Typography
Serif display for titles and the empty state gives an editorial, readable voice; system sans for chat body keeps it fast and offline; mono for entry addresses, model ids, tests. Deliberately all system stacks: nothing is downloaded, which is a product promise (zero dependencies). Body 15 px on desktop, 16 px under 720 px. Assistant text max 68 characters wide. Two weights per screen (400, 600).

## Layout
4/8 spacing scale. Thread column 780 px max. Sidebar 268 px, ledger 330 px docked ≥ 1100 px, sheet below. Inside a group 8 px, between groups 24 px. Assistant messages are unboxed; only user messages sit in a `user-bg` tonal block.

## Elevation & Depth
Tonal layers first, 1 px border only to separate regions (sidebar/main, ledger/main). Shadow only on floating things (dialogs, toast, scroll-to-bottom). No coloured left stripes, except Build test rows where colour is doubled by icon and word.

## Shapes
Controls 8 px, cards 12 px, sheets 16 px, pills full. Never mix sharp and rounded corners in one view.

## Components
- **Lane pill:** Smart = filled, Fast = outlined, always with text.
- **Ledger entry:** kind label (Goal, Rule, Decision, Blocker) in caps 11 px, value, mono address, delete on hover and focus. New entry: slide 8 px + teal tint fading over 1.2 s.
- **Handoff seam:** hairline + centred label; states handing off, done, failed. Opens the entries carried.
- **Composer:** rounded 16 px, single send/stop control that morphs, hint row hidden on touch.
- **Build feature row:** icon + name + `n/m tests` + status word; failed rows expose Fix and Skip.
- **States:** hover = tonal step, focus = 2 px accent ring, offset 2 px, disabled = 45% opacity with `aria-disabled`, error = text + icon + fix action.

## Motion
| Token | Value | Use |
|---|---|---|
| fast | 120 ms | hover, press |
| base | 220 ms | panels, entries |
| slow | 360 ms | sheet, seam draw (once) |
| easing | standard `.4,0,.2,1`, enter `0,0,.2,1`, exit `.4,0,1,1` | |
Animate only `transform` and `opacity`. Signature moments: ledger entry write, seam draw, Build feature fill. Streaming caret stays. Reduced motion (OS setting) and an in-app **Hold still** toggle remove everything except colour changes. No flashing, no looping decoration.

## Do's and Don'ts
- Do show the seam whenever the model changes, including mid-reply.
- Do use one primary button per screen and one accent.
- Do keep the assistant unboxed and the measure at 68 ch.
- Don't encode meaning by colour alone (lane, status, severity).
- Don't add purple/blue gradients, glass blur, mascots or streaks.
- Don't hard-code hex or px in components; reference tokens.
- Don't nest containers more than two levels.

## What we tried
Rejected directions: phosphor terminal, glass, cream + terracotta (see `DECISION.md`). Custom web fonts rejected for offline and zero-dependency reasons.
