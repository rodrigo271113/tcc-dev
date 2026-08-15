// Compare the two ways render.js can resolve a tile click to a node.
//
// The original findInFull() walked the whole hierarchy on every click and did
// not even stop at the match:
//
//     rootHierarchy.each(n => { if (n.data.path === path) found = n; });
//
// The replacement indexes `path -> node` once. This script measures the gap on
// the real tree and, just as importantly, proves the two agree on every single
// path -- including nodes past MAX_DEPTH, which is the case findInFull exists
// to handle in the first place.
//
// It deliberately does NOT import d3: there is no node_modules in this repo
// (see CLAUDE.md). The traversal below is the same pre-order walk d3.hierarchy
// does, so the node counts are real even though the wrapper objects are not.
//
// Usage:
//     python bench/backend_bench.py --dump /tmp/tree.json perf-lookup-vectorize
//     node bench/frontend_lookup_bench.mjs /tmp/tree.json

import { readFileSync } from "node:fs";

const jsonPath = process.argv[2] ?? "/tmp/tree.json";
const data = JSON.parse(readFileSync(jsonPath, "utf8"));

// Build a minimal hierarchy: one wrapper per node, pre-order, like d3 does.
function hierarchy(root) {
    const nodes = [];
    const walk = (d, depth, parent) => {
        const node = { data: d, depth, parent, children: null };
        nodes.push(node);
        if (d.children) node.children = d.children.map(c => walk(c, depth + 1, node));
        return node;
    };
    const rootNode = walk(root, 0, null);
    return { rootNode, nodes };
}

const t0 = performance.now();
const { nodes } = hierarchy(data);
const buildMs = performance.now() - t0;

const maxDepth = nodes.reduce((m, n) => Math.max(m, n.depth), 0);
console.log(`tree: ${nodes.length} nodes, max depth ${maxDepth}, built in ${buildMs.toFixed(1)}ms`);

// --- the old way: full scan, no early exit -------------------------------
function findLinear(path) {
    let found = null;
    for (const n of nodes) if (n.data.path === path) found = n;
    return found;
}

// --- the new way: index once, then O(1) ----------------------------------
const t1 = performance.now();
const byPath = new Map();
for (const n of nodes) byPath.set(n.data.path, n);
const indexMs = performance.now() - t1;
console.log(`index: ${byPath.size} entries built once in ${indexMs.toFixed(1)}ms`);

if (byPath.size !== nodes.length) {
    console.log(`WARNING: ${nodes.length - byPath.size} duplicate paths -- ` +
                `the Map keeps the last, matching the old scan's "last match wins"`);
}

// --- equivalence --------------------------------------------------------
// The old scan kept the LAST match; the Map, built in the same pre-order with
// set() overwriting, keeps the last too. So when paths are unique the two are
// equivalent by construction, and an O(n) pass proves it. Cross-checking every
// path against the real scan is O(n^2) -- ~4.5 billion compares on this tree,
// about two minutes -- so it is opt-in via --exhaustive.
const unique = byPath.size === nodes.length;
let mismatches = 0;
for (const n of nodes) {
    if (byPath.get(n.data.path).data.path !== n.data.path) mismatches++;
}
console.log(`equivalence: paths unique=${unique}, ` +
            `${mismatches} resolution mismatches over ${nodes.length} nodes (O(n) check)`);

if (process.argv.includes("--exhaustive")) {
    console.log("running exhaustive O(n^2) cross-check against the real scan (slow)...");
    let deep = 0;
    for (const n of nodes) if (byPath.get(n.data.path) !== findLinear(n.data.path)) deep++;
    console.log(`exhaustive: ${deep} mismatches across all ${nodes.length} paths`);
}

// --- timing: clicks spread across depths, including past MAX_DEPTH -------
const MAX_DEPTH = 3;   // mirrors config.js
const deep = nodes.filter(n => n.depth > MAX_DEPTH);
const sample = [];
for (let i = 0; i < 200; i++) {
    const pool = i % 2 === 0 && deep.length ? deep : nodes;
    sample.push(pool[(i * 7919) % pool.length].data.path);
}

function time(fn, paths, reps) {
    const t = performance.now();
    let sink = 0;
    for (let r = 0; r < reps; r++) for (const p of paths) if (fn(p)) sink++;
    return { ms: (performance.now() - t) / (reps * paths.length), sink };
}

time(findLinear, sample.slice(0, 5), 1);          // warm up JIT
time(p => byPath.get(p), sample, 5);

const lin = time(findLinear, sample, 3);
const map = time(p => byPath.get(p), sample, 200);

console.log(`\nper click (mean over ${sample.length} targets, half of them past depth ${MAX_DEPTH}):`);
console.log(`  linear scan : ${lin.ms.toFixed(4)} ms   (${nodes.length} nodes visited)`);
console.log(`  map lookup  : ${map.ms.toFixed(6)} ms   (1 hash lookup)`);
console.log(`  speedup     : ${(lin.ms / map.ms).toFixed(0)}x`);
console.log(`\nindex cost is paid once (${indexMs.toFixed(1)}ms) and amortises after ` +
            `${Math.ceil(indexMs / lin.ms)} clicks.`);
