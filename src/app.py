import os
import numpy as np
import pandas as pd
from flask import Flask, render_template, jsonify, request

app = Flask(__name__, template_folder='templates')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.abspath(os.path.join(BASE_DIR, '../data'))
DEFAULT_REPO = "tokenFilesFull"
TOKEN_CSV_PATH = os.path.join(DATA_DIR, DEFAULT_REPO + ".csv")

# Not a repository: an ~860MB aggregate of every project, which would only
# stall the server if someone picked it from the dropdown.
EXCLUDED_CSVS = {"_all_projects.csv"}

# Columns the tree builder actually needs. Present in both CSV families, so
# they double as the signal for "did we split this header on the right char?".
REQUIRED_COLUMNS = ("identifier", "pony_factor", "node_len_lines", "node_name")

# The two CSV families in this project use different separators: the token
# dumps are plain comma-separated, while the per-project exports use an
# INTERPUNCT (U+00B7) -- a character that essentially never shows up in real
# path/file content. (An earlier version tried ',' and fell back to '.', a
# typo: a dot separator would shred every filename. Worse, the fallback could
# never fire -- reading an interpunct file with sep=',' does not raise, it
# just yields one fat column -- so detection has to look at the header.)
CSV_SEPARATORS = ('·', ',')

# Built trees, keyed by csv path -> {"mtime", "tree"}; a slot is rebuilt only
# when that file changed. This started as a single global slot, but with a repo
# selector consecutive requests routinely hit different CSVs, so one slot per
# repo is what actually avoids the rebuild.
#
# Bounded because the cached value is the finished Response: ~8.8MB for the
# kernel and several times that for the largest projects in the sample, so an
# unbounded map over 80+ repos would be a memory leak in slow motion. Insertion
# order makes eviction of the oldest entry a one-liner.
_tree_cache = {}
TREE_CACHE_MAX = 4


def tf_to_color(tf):
    """Bucket a whole column of Pony Factors into colours at once.

    Same thresholds (and same order) as the legend in index.html -- keep both
    in sync. NaN compares False against every bound, so unusable values land on
    grey exactly like the old per-row float()/except fallback did.
    """
    val = np.asarray(tf, dtype=float)
    return np.select(
        [val <= 1, val <= 2, val <= 5],
        ['#ef4444',   # Red (High Risk)
         '#eab308',   # Yellow
         '#22c55e'],  # Green
        default='#94a3b8',  # Grey (Low Risk)
    )


def numeric_column(df, column, fallback):
    """Vectorised stand-in for the per-row `float(row.get(column, default))`.

    Missing column -> the whole column is the default. Cells that aren't
    numbers at all fall back to `fallback`, like the old try/except; genuinely
    empty cells stay NaN, which is what float(nan) produced before.
    """
    if column not in df.columns:
        return pd.Series(float(fallback), index=df.index, dtype=float)
    raw = df[column]
    val = pd.to_numeric(raw, errors='coerce')
    return val.mask(val.isna() & raw.notna(), float(fallback)).astype(float)


def format_lines(size):
    """Render a line count as a grouped string, e.g. 12345 -> '12,345 lines'."""
    try:
        return f"{int(size):,} lines"
    except Exception:
        return f"{size} lines"


def list_repos():
    """Names (basename minus '.csv') of every repository CSV in DATA_DIR."""
    try:
        entries = os.listdir(DATA_DIR)
    except OSError:
        return []
    return sorted(
        (f[:-4] for f in entries
         if f.endswith('.csv') and f not in EXCLUDED_CSVS
         and os.path.isfile(os.path.join(DATA_DIR, f))),
        key=str.lower,
    )


def resolve_repo_path(repo):
    """Map a user-supplied repo name to its CSV path, or None if not allowed.

    The name arrives straight from the query string and ends up in a file
    path, so it is never concatenated in blind: it must match -- exactly, by
    string equality -- one of the names the directory scan actually found.
    Anything with a path separator, a '..' segment, or an absolute/drive-ish
    shape is rejected up front as well, so a traversal attempt can't even
    reach the whitelist comparison.
    """
    if not repo:
        return TOKEN_CSV_PATH
    if os.path.sep in repo or (os.path.altsep and os.path.altsep in repo):
        return None
    if '/' in repo or '\\' in repo or '..' in repo or repo.startswith('.'):
        return None
    if os.path.isabs(repo) or os.path.basename(repo) != repo:
        return None
    if repo not in list_repos():
        return None
    path = os.path.join(DATA_DIR, repo + '.csv')
    # Belt and braces: the joined path must still live directly in DATA_DIR.
    # (abspath, not realpath -- data files are legitimately symlinks here.)
    if os.path.dirname(os.path.abspath(path)) != DATA_DIR:
        return None
    return path


def read_repo_csv(csv_path):
    """Read a node CSV, picking the separator by inspecting the header.

    Sniffing beats a try/except chain here: pandas happily parses an
    interpunct-separated file with sep=',' (it just returns a single column
    whose name is the whole header), so a failed parse is not something an
    exception would report.
    """
    with open(csv_path, 'r', encoding='utf-8', errors='replace') as fh:
        header = fh.readline()

    for sep in CSV_SEPARATORS:
        fields = [f.strip() for f in header.rstrip('\r\n').split(sep)]
        if all(col in fields for col in REQUIRED_COLUMNS):
            # '·' is 2 bytes in UTF-8, which pandas' C parser cannot use as a
            # separator; asking for the python engine avoids its warning.
            engine = 'python' if len(sep.encode('utf-8')) > 1 else 'c'
            return pd.read_csv(csv_path, sep=sep, engine=engine)

    raise ValueError(
        f"unrecognised CSV header in {os.path.basename(csv_path)}: "
        f"expected columns {', '.join(REQUIRED_COLUMNS)} separated by "
        f"one of {CSV_SEPARATORS}"
    )


@app.route('/')
def index():
    return render_template('index.html')

# populates the selector w/ the available repositories
@app.route('/api/repos')
def get_repos():
    """The repositories the selector can offer, plus the one loaded by default."""
    return jsonify({"repos": list_repos(), "default": DEFAULT_REPO})

# builds the tree from the csv file
def build_tree(csv_path):
    df = read_repo_csv(csv_path)

    # The CSV already contains one row per node (directories AND files). Row
    # order does not matter (the token dumps put the root row first, the
    # per-project exports put it last): every node is materialised before any
    # linking happens. Each node's parent id is the
    # identifier with the trailing "/node_name" removed -- the same rule the
    # legacy Plotly backend uses. We build the tree directly from that, instead
    # of re-deriving ancestry by string-splitting (which mismatched the leading
    # slash and produced phantom duplicate directories).
    #
    # Everything that can be expressed a column at a time (stringification,
    # numeric coercion, colour bucketing) is done in pandas/numpy; the rows are
    # then handed to plain Python as lists. iterrows() built a Series per row
    # and was ~92% of the old build time -- the work below is the same work
    # without that per-row object churn.

    root = {
        "name": "Project Root",
        "children": [],
        "path": "",
        "color": "#334155",
    }

    # str() of a missing cell yields the literal "nan"; .astype(str) reproduces
    # that exactly, so the empty/"nan" root-row check below still matches.
    if "identifier" in df.columns:
        identifier = df["identifier"].astype(str)
    else:
        identifier = pd.Series("", index=df.index, dtype=object)

    # The root row (empty identifier) contributes its metadata to `root` only.
    is_root_row = identifier.isin(["", "nan"])
    if is_root_row.any():
        if "pony_factor" in df.columns:
            root_tf = df.loc[is_root_row, "pony_factor"].iloc[-1]
        else:
            root_tf = 10
        try:
            root["tf"] = float(root_tf)
        except Exception:
            pass
        keep = ~is_root_row
        df, identifier = df[keep], identifier[keep]

    # `nodes[identifier] = ...` let a repeated id overwrite the earlier node;
    # dropping all but the last row keeps that "one node per path" guarantee
    # (the frontend keys its data-join on path).
    repeated = identifier.duplicated(keep="last")
    if repeated.any():
        keep = ~repeated
        df, identifier = df[keep], identifier[keep]

    if "node_name" in df.columns:
        node_name = df["node_name"].astype(str)
    else:
        node_name = identifier.str.rsplit('/', n=1).str[-1]

    tf = numeric_column(df, "pony_factor", 10)
    size = numeric_column(df, "node_len_lines", 1)

    # .tolist() hands back native Python str/float (not numpy scalars, which
    # jsonify cannot serialise) and iterates far faster than a Series.
    paths = identifier.tolist()
    names = node_name.tolist()
    colors = tf_to_color(tf).tolist()
    tfs = tf.tolist()
    sizes = size.tolist()
    actual_sizes = [format_lines(s) for s in sizes]

    nodes = [
        {
            "name": name,
            "path": path,
            "color": color,
            "tf": node_tf,
            "value": node_size,
            "actual_size": actual_size,
        }
        for name, path, color, node_tf, node_size, actual_size
        in zip(names, paths, colors, tfs, sizes, actual_sizes)
    ]

    # parent id = identifier minus the trailing "/<node_name>" segment. Sliced
    # by node_name's length rather than split on "/": node_name is what makes
    # this rule exact, and re-deriving the segment from the string is what
    # produced phantom duplicate directories before. (Measured: pandas'
    # .str.rsplit is ~2x slower here anyway -- .str is a Python loop too.)
    parent_ids = [path[:max(0, len(path) - len(name) - 1)]
                  for path, name in zip(paths, names)]

    by_path = dict(zip(paths, nodes))
    by_path[""] = root

    # Attach each node to its parent (unknown parent -> root, as before). A node
    # only grows a "children" list when it actually gets one, so leaves never
    # carry an empty array; and the first child is where a node is revealed to
    # be a directory, so that is where its (redundant, subtree-total) "value"
    # goes away -- D3's .sum() must derive internal-node area from leaves alone.
    for parent_id, node in zip(parent_ids, nodes):
        parent = by_path.get(parent_id, root)
        children = parent.get("children")
        if children is None:
            parent.pop("value", None)
            children = parent["children"] = []
        children.append(node)

    if not root["children"]:
        del root["children"]

    # serializing the json here (rather than in the route) keeps this the single unit cache can memoize
    return jsonify(root)


@app.route('/api/tree')
def get_tree():
    repo = request.args.get('repo', '', type=str).strip()
    csv_path = resolve_repo_path(repo)
    if csv_path is None:
        return jsonify({"error": f"unknown repository: {repo!r}"}), 400
    if not os.path.isfile(csv_path):
        return jsonify({"error": f"no data file for repository: {repo!r}"}), 404

    # serve the cached Response when this repo's CSV has not changed. caching
    # the finished Response rather than the dict matters: serialising the tree
    # is ~0.3s on its own, so memoising only the dict would leave that on every
    # request.
    mtime = os.path.getmtime(csv_path)
    cached = _tree_cache.get(csv_path)
    if cached is not None and cached["mtime"] == mtime:
        return cached["tree"]

    try:
        tree = build_tree(csv_path)
    except Exception as exc:
        # A CSV whose header matches neither family (or is otherwise unusable)
        # is a data problem, not a crash: report it as JSON so the frontend can
        # show it instead of hanging on the loading overlay.
        app.logger.exception("failed to build tree for %s", csv_path)
        return jsonify({"error": f"could not read repository {repo or DEFAULT_REPO!r}: {exc}"}), 500

    _tree_cache.pop(csv_path, None)          # re-insert at the newest end
    _tree_cache[csv_path] = {"mtime": mtime, "tree": tree}
    while len(_tree_cache) > TREE_CACHE_MAX:
        _tree_cache.pop(next(iter(_tree_cache)))
    return tree


if __name__ == '__main__':
    # running on 5001 so it does not conflict with the existing app on 5000
    app.run(debug=True, port=5001)
