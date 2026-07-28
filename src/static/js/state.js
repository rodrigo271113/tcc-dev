// Shared mutable state read/written across modules (render, search).
export const state = {
    sizeMode: 'linear',       // 'linear' = faithful areas, 'log' = per-level compression
    searchHighlight: null,    // path of the tile to highlight after a search jump
};
