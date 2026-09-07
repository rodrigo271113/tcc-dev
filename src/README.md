# Knowledge Distribution Treemap

A Flask + D3.js tool that renders a repository's file tree as a nested,
drill-down treemap coloured by **Pony Factor** — a bus-factor-style metric
counting the smallest number of authors whose combined contribution exceeds
50% of a node's content. Red means the knowledge about that file or directory
sits with one person; grey means it is widely shared.

Built for a TCC (USP undergraduate thesis) on visualisations for this metric.
It is a rework of an earlier Plotly proof-of-concept; see `../CHANGELOG.md`
for how the rendering evolved.

## Requirements

- Python 3.10+
- `flask`, `pandas`, `numpy`

No build step on the frontend: it is plain ES modules loaded straight by the
browser, and D3 v7 comes from a CDN `<script>` tag in `templates/index.html`.

## Data

The app reads CSVs from `data/` at the **repository root** (one directory up
from here). That folder is gitignored and not checked in — you need to put the
data there yourself. One CSV per repository, named `<repo>.csv`; the name
without the extension is what shows up in the selector.

Each row is one tree node (a file *or* a directory) and must contain the
columns `identifier`, `node_name`, `node_len_lines` and `pony_factor`. Two
separator families are auto-detected from the header: `,` (token dumps) and
`·` U+00B7 (per-project exports).

> `node_len_lines` holds **tokens** in the token dumps and **lines** in the
> per-project exports — the column name lies for the former. The app resolves
> the real unit per repo and labels the UI accordingly.

## Running it

```bash
# from the repository root
python -m venv .venv
source .venv/bin/activate
pip install flask pandas numpy

mkdir -p data
cp /path/to/tokenFilesFull.csv data/      # plus any other <repo>.csv you have

cd src
python app.py
```

Then open <http://localhost:5001>. (Port 5001, deliberately, so it does not
clash with the legacy app this one reworks.)

## Endpoints

| Route        | What it returns                                              |
| ------------ | ------------------------------------------------------------ |
| `/`          | The page.                                                     |
| `/api/repos` | The repositories found in `data/`, plus the default one.       |
| `/api/tree`  | The whole tree for `?repo=<name>` as a single JSON blob.       |

Built trees are cached per repo (keyed on the CSV's mtime), so the first
request for a repo pays the build cost and later ones are served from memory.

## Using it

- **Click** a tile to drill into it; use the breadcrumb to come back up.
- **Search** to jump to a file or folder anywhere in the tree.
- **Directory sizing** toggles between faithful linear areas and log-compressed
  areas, which keep small siblings visible next to giant ones.

## Layout

```
app.py                 data shaping: read CSV -> build tree -> JSON
templates/index.html   the page, the legend and the controls
static/js/
  main.js              wiring: fetch -> hierarchy -> renderer + search
  config.js            every tunable constant
  state.js             shared mutable state (size mode, search, unit)
  weights.js           per-level area normalisation (linear / log)
  render.js            the renderer: drill-down, culling, animation
  text-fit.js          canvas-based label measurement and wrapping
  tooltip.js           edge-aware hover tooltip
  search.js            ranked search over the in-memory node list
```
