import os
import pandas as pd
from flask import Flask, render_template, jsonify

app = Flask(__name__, template_folder='templates')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.abspath(os.path.join(BASE_DIR, '../data'))
TOKEN_CSV_PATH = os.path.join(DATA_DIR, "tokenFilesFull.csv")


def tf_to_color(tf):
    try:
        val = float(tf)
        if val <= 1: return '#ef4444'   # Red (High Risk)
        elif val <= 2: return '#eab308'  # Yellow
        elif val <= 5: return '#22c55e'  # Green
        else: return '#94a3b8'           # Grey (Low Risk)
    except Exception:
        return '#94a3b8'


def format_lines(size):
    """Render a line count as a grouped string, e.g. 12345 -> '12,345 lines'."""
    try:
        return f"{int(size):,} lines"
    except Exception:
        return f"{size} lines"


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/tree')
def get_tree():
    try:
        df = pd.read_csv(TOKEN_CSV_PATH, sep=',')
    except Exception:
        df = pd.read_csv(TOKEN_CSV_PATH, sep='.')

    # The CSV already contains one row per node (directories AND files), with
    # parents always listed before their children. Each node's parent id is the
    # identifier with the trailing "/node_name" removed -- the same rule the
    # legacy Plotly backend uses. We build the tree directly from that, instead
    # of re-deriving ancestry by string-splitting (which mismatched the leading
    # slash and produced phantom duplicate directories).

    root = {
        "name": "Project Root",
        "children": [],
        "path": "",
        "color": "#334155",
    }
    nodes = {"": root}

    # Pass 1: materialise every row as a node keyed by its identifier.
    for _, row in df.iterrows():
        identifier = str(row.get("identifier", ""))
        if identifier == "" or identifier == "nan":
            # The root row (empty identifier): keep its own metadata.
            try:
                root["tf"] = float(row.get("pony_factor", 10))
            except Exception:
                pass
            continue

        node_name = str(row.get("node_name", identifier.split('/')[-1]))

        try:
            tf = float(row.get("pony_factor", 10))
        except Exception:
            tf = 10

        try:
            size = float(row.get("node_len_lines", 1))
        except Exception:
            size = 1

        nodes[identifier] = {
            "name": node_name,
            "children": [],
            "path": identifier,
            "color": tf_to_color(tf),
            "tf": tf,
            "value": size,
            "actual_size": format_lines(size),
        }

    # Pass 2: link each node to its parent. parent_id = identifier without the
    # trailing "/node_name" segment (empty string -> the root node).
    for identifier, node in list(nodes.items()):
        if identifier == "":
            continue
        name = node["name"]
        parent_id = identifier[:-(len(name) + 1)]
        parent = nodes.get(parent_id, root)
        parent["children"].append(node)

    # Pass 3: D3's .sum() computes internal-node areas from leaf values. Drop the
    # (redundant, subtree-total) "value" from any node that has children so the
    # math is not skewed; keep colour/tf for display. Prune empty leaf arrays.
    def cleanup(node):
        if node.get("children"):
            if "value" in node:
                del node["value"]
            for c in node["children"]:
                cleanup(c)
        else:
            if "children" in node:
                del node["children"]

    cleanup(root)

    return jsonify(root)


if __name__ == '__main__':
    # Running on 5001 so it does not conflict with the existing app on 5000
    app.run(debug=True, port=5001)
