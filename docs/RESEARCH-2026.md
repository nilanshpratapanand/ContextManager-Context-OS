# Pixel Dusk UI research notes

The redesign keeps ContextOS' existing interaction model and changes the visual language to a
quiet 16-bit command deck: deep night blue, a single sunset accent, hard-edged controls, and a
pixel-art horizon used only as atmosphere on the empty state.

## Useful evidence

- **Aesthetic-usability effect** — Kurosu & Kashimura's work, summarized by Nielsen Norman Group,
  shows that attractive interfaces are perceived as easier to use. This supports a strong visual
  identity, but the existing keyboard focus, labels, and semantic status words remain intact so
  beauty does not hide usability problems.
- **Prototypicality and aesthetics** — Miniukovich et al. (2023) found that visual prototypicality
  correlates with perceived aesthetics, pre-use usability, and trust. Repeated geometry, consistent
  tokens, and a single accent make the pixel language legible rather than decorative noise.
- **Motion as feedback** — Google's Material motion guidance and the UX in Motion principles treat
  motion as spatial explanation and feedback. The skin preserves the existing staggered entry,
  handoff-seam draw, and reduced-motion toggle; it adds no looping background animation.
- **Agent harnesses** — ContextOS' own `docs/RESEARCH.md` cites *The Handoff Tax* (arXiv
  2608.24358), *Specification Portability* (arXiv 2608.21208), MemGPT, Mem0, and Zep. The UI
  therefore keeps the ledger, lane selector, handoff seam, loss manifest, and explicit model
  metadata visible instead of hiding the harness behind a generic chat surface.

## Design principles applied

1. **One dominant signal:** sunset orange is reserved for action, route, and memory continuity.
2. **Calm density:** panels are tonal layers; borders are reserved for region boundaries.
3. **Pixel restraint:** scanlines and supplied art are atmospheric, not content; text stays native
   and readable, and `prefers-reduced-motion` remains respected.
4. **Inspectable state:** model lane, memory count, status words, and handoff copy remain present.
5. **Progressive disclosure:** the sidebar, ledger, build view, and dialog continue to open through
   the existing controls, preserving the product's tested task flows.

## References

- https://www.nngroup.com/articles/aesthetic-usability-effect/
- https://www.sciencedirect.com/science/article/pii/S107158192300112X
- https://m2.material.io/design/motion/understanding-motion.html
- https://arxiv.org/abs/2608.24358
- https://arxiv.org/abs/2608.21208
- `docs/RESEARCH.md` (ContextOS benchmark and harness decisions)
