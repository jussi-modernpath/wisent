"""Node placement: score a layout with the VRTI kernel, and search for a better one.

The placement numbers in config/room.yaml ("worst-voxel sensitivity", "cond(G) median",
"least-observable spot") were computed by hand on 2026-07-30 and nothing could repeat
them. This module makes them reproducible from the two kernels the engines already use:

  s_l(p) = |g_l(p)| / (r_tx * r_rx)   the VRTI matched-field kernel (observability.py,
                                        vrti._log_kernel) — how much link l sees at p
  g_l(p) = unit(p-tx) + unit(p-rx)      the LinkBVP bistatic gradient (linkbvp.py)

Definitions, chosen because they reproduce the hand numbers (0.1168 at (6.2, 4.2),
cond median 1.85, 100% of the room under cond 10) and the alternatives (sum/mean over
links) do not:

  worst_voxel   min over grid points of max over unordered node pairs of s_l(p) — the
                spot where even the best-placed link sees least
  cond          cond(G(p)) with G(p) the (L, 2) stack of g_l(p): how well the pairs pin
                a velocity/position at p (roadmap: "cond(G) median 24" was the clustered
                star, "2.7" the spread mesh)
  grid          cell centres strictly inside the room, cell/2 + k*cell (0.4 m default)

Dependency-light: numpy only. Never imports sim (the live path is the package).
"""

import numpy as np


def link_sensitivity(tx, rx, pts, eps=0.36):
    """Vectorised s(p) = |g(p)| / (r_tx * r_rx) at (n, 2) points.

    Pointwise identical to observability.bistatic_sensitivity — validate_synthetic.py
    asserts it, so calibration, localization and placement keep one geometry model.
    """
    tx, rx, pts = np.asarray(tx, float), np.asarray(rx, float), np.asarray(pts, float)
    d1, d2 = pts - tx, pts - rx
    r1, r2 = np.linalg.norm(d1, axis=1), np.linalg.norm(d2, axis=1)
    g = d1 / np.maximum(r1, 1e-9)[:, None] + d2 / np.maximum(r2, 1e-9)[:, None]
    return np.linalg.norm(g, axis=1) / np.maximum(r1 * r2, eps)


def link_gradient(tx, rx, pts):
    """Vectorised g(p) = unit(p-tx) + unit(p-rx) at (n, 2) points -> (n, 2).

    Same +1e-9 floor as linkbvp.bistatic_gradient (asserted pointwise equal)."""
    tx, rx, pts = np.asarray(tx, float), np.asarray(rx, float), np.asarray(pts, float)
    d1, d2 = pts - tx, pts - rx
    u1 = d1 / (np.linalg.norm(d1, axis=1) + 1e-9)[:, None]
    u2 = d2 / (np.linalg.norm(d2, axis=1) + 1e-9)[:, None]
    return u1 + u2


def _grid(room, cell):
    xs = np.arange(cell / 2, room[0], cell)
    ys = np.arange(cell / 2, room[1], cell)
    X, Y = np.meshgrid(xs, ys)
    return np.stack([X.ravel(), Y.ravel()], axis=1), (len(ys), len(xs))


def layout_metrics(nodes, room, cell=0.4):
    """Score a layout. nodes: {node_id: (x, y)} metres; room: (X, Y) extent.

    Returns worst_voxel, worst_xy (where it occurs), cond_median, cond_frac_ok (share of
    grid points with cond < 10), grid_shape and n_links. Fewer than two unordered pairs
    cannot pin two unknowns: cond is reported infinite and cond_frac_ok is 0.
    """
    ids = sorted(nodes)
    pairs = [(a, b) for i, a in enumerate(ids) for b in ids[i + 1:]]
    if not pairs:
        raise ValueError("layout_metrics needs at least two nodes")
    pts, shape = _grid(room, cell)
    S = np.array([link_sensitivity(nodes[a], nodes[b], pts) for a, b in pairs])   # (L, V)
    best_link = S.max(axis=0)
    j = int(np.argmin(best_link))
    if len(pairs) >= 2:
        G = np.stack([link_gradient(nodes[a], nodes[b], pts) for a, b in pairs], axis=1)  # (V, L, 2)
        sv = np.linalg.svd(G, compute_uv=False)                                    # (V, 2)
        cond = np.minimum(sv[:, 0] / np.maximum(sv[:, -1], 1e-12), 1e12)
    else:
        cond = np.full(len(pts), np.inf)
    return {"worst_voxel": float(best_link[j]),
            "worst_xy": (float(pts[j, 0]), float(pts[j, 1])),
            "cond_median": float(np.median(cond)),
            "cond_frac_ok": float(np.mean(cond < 10.0)),
            "grid_shape": shape, "n_links": len(pairs)}
