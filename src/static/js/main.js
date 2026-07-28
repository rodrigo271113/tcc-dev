import { HEADER_H } from './config.js';
import { initRenderer } from './render.js';
import { setupSearch } from './search.js';

const container = document.getElementById("chart");
const width = container.parentElement.clientWidth;
const height = container.parentElement.clientHeight - 46;

const svg = d3.select("#chart").append("svg")
    .attr("viewBox", [0, 0, width, height])
    .attr("width", width)
    .attr("height", height)
    .style("overflow", "hidden");

d3.json('/api/tree').then(data => {
    document.getElementById('loading').style.display = 'none';

    // Build the full hierarchy once; .sum() over leaf values gives correct
    // areas for every ancestor (directory node_len_lines == sum of leaves).
    const rootHierarchy = d3.hierarchy(data)
        .sum(d => d.value || 0)
        .sort((a, b) => (b.value || 0) - (a.value || 0));

    const treemap = d3.treemap()
        .paddingOuter(2)
        .paddingTop(HEADER_H)
        .paddingInner(2)
        .round(true);

    const { render, setSizeMode } = initRenderer(rootHierarchy, svg, treemap, width, height);
    render(rootHierarchy);

    // Toggle between faithful and compressed sizing; re-render the current view.
    document.querySelectorAll('input[name="sizemode"]').forEach(el => {
        el.addEventListener('change', ev => {
            if (ev.target.checked) setSizeMode(ev.target.value);
        });
    });

    // ---- Search ----------------------------------------------------
    // Every node is already in memory; pre-lowercase name/path for matching.
    const allNodes = rootHierarchy.descendants();
    for (const n of allNodes) {
        n._sname = (n.data.name || "").toLowerCase();
        n._spath = (n.data.path || "").toLowerCase();
    }

    setupSearch(allNodes, render);
});
