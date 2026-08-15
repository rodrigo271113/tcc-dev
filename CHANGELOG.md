# Experiment Treemap — Change Log

A record of the changes made to the D3 nested treemap experiment
(`server/experiment/`) while reworking it from the original proof-of-concept
into a complete, interactive nested treemap.

Each entry maps to the git commit that contains it (identified by content). All
changes are in `server/experiment/templates/index.html` unless noted otherwise.

**Baseline — `6b3b974` "First commit"** *(original PoC, not part of this rework)*
The starting point: the backend built a broken tree (phantom duplicate
directories, real directories rendered as leaf "files") and the frontend only
ever drew a single level (no visible nesting). This is what the rework below
fixes and builds on.

---

## `3b466db` — Backend tree fix + nested renderer + tooltip/legend
*Files: `server/experiment/app.py`, `templates/index.html`*

The foundational rework, in three parts:

- **Backend (`app.py`):** replaced the string-splitting ancestry logic (which
  mismatched the leading `/` and created phantom gray duplicate directories,
  leaving the real directories rendered as files) with the same rule the legacy
  Plotly backend uses — a node's parent id is its identifier minus the trailing
  `/name`. Every CSV row becomes one node linked to its real parent, so there
  are no duplicates and directories keep their real Pony Factor/colour. Internal
  nodes drop their (redundant subtree-total) `value` so D3's `.sum()` computes
  areas cleanly; each node also carries a formatted `actual_size` for tooltips.
- **Nested renderer:** replaced the single-level "context-culling" view with a
  true nested layout that draws the focused subtree down to `MAX_DEPTH` levels
  at once — directory tiles get a header band (`paddingTop`) with children tiled
  inside, matching the legacy Plotly look. Click a folder to drill in
  (re-laying out the subtree to fill the canvas); click the breadcrumb to zoom
  out.
- **Parity polish:** hover tooltip (name, kind, path, formatted lines, TF),
  a Pony-Factor colour legend, and CSS fixes.

## `614e53d` — Linear / log per-level sizing toggle
A "Directory sizing" toggle switching between two area models, both driven by
one `applyWeights()` doing **true per-level normalization**:

- **Faithful (linear):** areas are exactly proportional to line counts.
- **Compressed (log, per-level):** at each directory, its box is split among
  children in proportion to `log(child_true_size)`, normalized so children sum
  to the parent exactly.

This keeps the tiling gap-free (preserves the parent = Σchildren invariant) and
encodes *size, not file count* — a cleaner version of the legacy Plotly log
sizing, which distorted by file count and needed `branchvalues="remainder"`.
True line counts (breadcrumb/tooltips) always come from a preserved `_true`
value, never the compressed layout weight.

## `83a6b6d` — Progressive (level-by-level) rendering
*Also touches `server/src/app.py` (minor)*

Splits the ~15k-tile DOM build across animation frames instead of one blocking
pass: depth 1 paints immediately, then depth 2, then depth 3 on successive
`requestAnimationFrame`s. The top-level structure appears ~236ms sooner and the
main thread stays responsive. A `renderToken` generation counter lets a
superseded progressive pass bail out. The join was restructured so persisting
nodes morph in place (no flicker on toggle) while only brand-new nodes are
appended per level.

## `7041e3b` — Sub-pixel tile culling
Skips rendering tiles smaller than `MIN_PX` (2px) — invisible, unclickable
slivers that dominate the tile count at big nodes. Cuts the root view from
~15,015 to ~2,795 tiles (−81%) with zero visible difference, since a child is
always smaller than its parent (no orphans) and drilling in re-reveals them.
Fewer tiles to build *and* animate.

## `9b1a444` — Adaptive animation + edge-aware tooltip
Two changes:

- **Adaptive animation:** on heavy views (`> HEAVY_TILES` tiles) geometry is set
  directly instead of tweened, with only a quick opacity fade — tweening
  thousands of rects is where the jank lives, and it's imperceptible at that
  scale. Small drills keep the full 500ms ease. Exit is immediate on heavy
  renders. Cuts a heavy re-render's settle time from ~500ms to ~25ms.
- **Edge-aware tooltip:** the hover tooltip now measures itself and flips to the
  left / above the cursor (with a clamp) when the default below-right placement
  would overflow the viewport, so it never clips off-screen near the bottom or
  right edges.

## `c7b31b1` — Adaptive label font size + multi-line wrapping
Two related label improvements:

- **Adaptive font size:** instead of a binary show-at-fixed-size / hide,
  `fitFontSize()` renders each label at the largest size that fits its tile
  (down to `MIN_FONT`), else hides it. Surfaces names on many more medium tiles
  and stops mid-word clipping of longer names. Uses canvas `measureText` (no SVG
  reflow) so it's cheap per tile.
- **Line wrapping (break-line):** for leaf tiles that are tall/narrow,
  `fitWrapped()`/`wrapName()` wrap the name across lines (breaking at
  `/ _ - .` separators, hard-breaking over-long tokens) so the tile's height is
  used and the font stays readable. A `MIN_LINE_CHARS` guard rejects "shredded"
  multi-line results on very narrow tiles (they fall back to a single small line
  or stay blank).

## `75bcfb9` — Stale-render interrupt guards (zoom in/out artifact fix)
Fixes artifacts when zooming into a node and back out before the drill finishes.
A superseded render's in-flight transitions kept overwriting the geometry the
new render set (especially on the heavy path, which assigns attributes directly)
— leaving half-loaded child tiles stuck at their old canvas-filling size over
the parent. The fix interrupts all pending transitions (`""`/`fade`/`move`/
`size`) at the start of each render, before re-binding. This also cancels a
pending exit `.remove()` so a node that becomes valid again isn't deleted
mid-flight. Normal drilling still animates; only stale tweens are killed.

## `c970a33` — Search bar
A search input over the in-memory hierarchy (no backend change):

- Live, debounced, ranked results (exact → prefix → name-contains →
  path-contains), each showing name, kind · line count, and full path.
- Selecting a **folder** zooms into it; selecting a **file** reveals its
  containing folder and highlights the file tile with a pulsing outline. The
  highlighted target is never culled, so it always renders.
- Keyboard support: ↑/↓ to move, Enter to jump, Esc to close.

---

## Tunable constants (top of `index.html`)

| Constant | Purpose |
|---|---|
| `MAX_DEPTH` | Nested levels drawn at once (3) |
| `HEADER_H` | Directory header band height (20px) |
| `MIN_PX` | Sub-pixel culling threshold (2px) |
| `MIN_FONT` | Smallest label font before hiding |
| `STD_DIR_FONT` / `STD_LEAF_FONT` | Preferred label sizes |
| `MIN_LINE_CHARS` | Min chars per wrapped line (anti-shred) |
| `HEAVY_TILES` | Tile count above which geometry snaps (1250) |
| `LAYOUT_CACHE_MAX` | Computed subtree layouts kept in the LRU cache (24) |
