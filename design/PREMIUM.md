# Premium pass: research, plan, build, audit

Trigger: the first Ledger pass was judged bland and generic: no logo (blank tab icon), no motion, default fonts. That is fair. This document supersedes the colour and type choices in `DESIGN.md`; the structure (seam, ledger, Build board) stays.

## 1. Audit of the previous pass (against the anti-slop kit)
| Tell | Found | Verdict |
|---|---|---|
| Default teal accent | yes, `#0B6B5C` | fail |
| System font stack as primary face | yes | fail |
| Serif headline + sans body by default | yes | fail |
| No logo, blank favicon | yes (tab shows an empty square) | fail |
| Motion limited to caret and dots | yes | fail (brief asked for feel) |
| Identical chips and cards, one radius | mostly | needs work |
| Meaning by colour alone | fixed (lane pills) | pass |

## 2. Research (what is verified, what is my judgement)
- `@starting-style` is Baseline since August 2024: Chrome 117, Edge 117, Firefox 129, Safari 17.5. Lets dialogs, toasts and new rows animate in with plain CSS. ([web.dev digest](https://web.dev/blog/baseline-digest-may-2026), [Can I use](https://caniuse.com/mdn-css_at-rules_starting-style))
- Same-document View Transitions are Baseline newly available since Firefox 144 (16 Oct 2025): Chrome 111, Edge 111, Firefox 144, Safari 18. Used for chat switching, Build mode and the theme change, with a plain fallback. ([web.dev](https://web.dev/blog/same-document-view-transitions-are-now-baseline-newly-available))
- Favicons: one SVG (can carry its own colours), an ICO or PNG fallback, a 180 px apple-touch-icon, and a manifest for installable apps. ([DEV: favicon guide](https://dev.to/vannsl/the-surprisingly-weird-world-of-favicons-and-how-to-survive-it-5a2l), [favicon.im](https://favicon.im/blog/svg-favicon-complete-guide))
- Motion timings from the design skill's sources: about 100 ms for feedback, 200-300 ms for panels, 400 ms upper bound, exits about 65% of entrances (NN/g "Executing UX Animations"). Animate transform and opacity only.
- Fonts cannot come from a CDN (the app is offline-first and zero-dependency), so three OFL variable fonts are bundled from the Fontsource npm packages (about 94 KB total, latin subset).
- Judgement, not evidence: that flat ultramarine on warm paper reads as more distinctive than teal, and that a state-driven logo mark is a stronger signature than ambient decoration.

## 3. Direction: "Ultramarine Ledger"
- **Identity:** an ink-and-paper ledger with one flat ultramarine accent. No gradients, no glass, no glow.
- **Logo:** a rounded tile holding a "C" ring with an open side; a dot (one carried memory entry) sits in the gap. On load the dot slides from inside the ring to the gap. While a reply streams, the small mark in the status row slides the dot back and forth, meaning "carrying context". The favicon is the static mark.
- **Type:** Bricolage Grotesque (wordmark, titles, big numbers), Geist (body), Geist Mono (addresses, ids, tests).
- **Colour:** paper `#F4F2EC` / ink `#0E0F13` / ultramarine `#2431E8`; dark: `#0A0B0F` / `#ECEBE6` / `#8797FF`. All text pairs pass AA (see `tokens.css`).
- **Signature:** the handoff seam (kept) plus the living mark.

## 4. Motion plan (each has a job)
| Where | What | Job | Time |
|---|---|---|---|
| Load | mark dot slides out, sidebar rows rise in, 40 ms stagger, once | orientation | 360 ms |
| Switch chat, open Build | View Transition crossfade with 8 px rise | continuity | 200 ms |
| Theme toggle | circle reveal from the click | delight, rare | 400 ms |
| New message | fade and rise 8 px, only for new ones | focus | 220 ms |
| Sidebar hover | one highlight that glides between rows | orientation | 160 ms |
| Composer | focus ring grows, send button presses to .94, icon turns to stop | feedback | 120 ms |
| Dialog, toast | scale .98 + fade via `@starting-style`, exit faster | orientation | 220 / 140 ms |
| Ledger entry, count | slide + tint (kept), count bumps | focus | 220 ms |
| Welcome | headline, sample seam and chips stagger once | first impression | 60 ms steps, max 400 ms |
| Build | stepper fill, feature bars, cards rise | status | 360 ms |
Not animated: text being read, streaming body, anything looped. Hold still and `prefers-reduced-motion` switch all of it off.

## 5. Build checklist
Static route + bundled fonts, logo symbol, favicon, `theme-color`, tokens, type, motion layer, view transitions, dialog/toast, sidebar glide, welcome, states, thin scrollbars.

## 6. Audit checklist
Contrast script, slop scan, screenshots light/dark/mobile, console errors, a frame check on every animation, reduced-motion and Hold still verified, fonts load from the app (no external requests).

## 7. Audit results (after build)
| Check | Result |
|---|---|
| Contrast (`contrast.py --css design/tokens.css`) | all pairs pass; text 17.1:1 light, 16.5:1 dark; accent-ink on accent 7.8:1 / 7.4:1 |
| Slop scan | fail 0, warn 3 (below) |
| Fonts | Bricolage Grotesque, Geist, Geist Mono load from `/static/fonts/`; no font is fetched from outside |
| Reduced motion (OS setting) | animations resolve to `none` |
| Hold still | persists across reload; page animations and the logo dot stop |
| View Transitions | switching chats and new chat run without page errors; theme toggle shows a circle reveal from the click (mid-frame captured) |
| Path traversal on `/static` | `..` variants return 404 |
| Console | no page errors |

Slop-scan warnings, kept on purpose: (1) coloured left stripes remain only on Build pass/fail cards (one semantic role) and on quotes and reasoning text; (2) infinite animations are the typing dots, the caret and the logo dot, all shown only while a reply is streaming and all stopped by Hold still; (3) 38 hex values are token definitions for both themes plus the code-block palette.

Timing audit: feedback 120 ms, panels and messages 220 ms, welcome and boot 360 ms with 45-60 ms stagger (under 400 ms total), theme reveal 460 ms (once, on demand), view transition out 120 ms / in 220 ms. Exits are shorter than entrances. Only transform, opacity and clip-path animate.

Not verified: real-device performance on a low-end laptop, and the Build board against a live build. Existing CDN scripts (highlight.js, KaTeX) still load from cdnjs when online; that predates this work.
