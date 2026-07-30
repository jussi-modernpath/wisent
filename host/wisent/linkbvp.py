"""LinkBVP v0 — velocity inference from unsigned multi-link pseudo-Doppler.

The novel bit. Full spec and honesty notes: docs/linkbvp.md. Core identity:

    f_l = | v . g_l(p) | / lambda,   g_l(p) = unit(p - tx_l) + unit(p - rx_l)

Amplitude sensing folds the sign, so every window's solution comes as a +/-v pair
(cost(v) == cost(-v) identically). Temporal tracking is what disambiguates; v0
returns one representative and flags the ambiguity.
"""

import numpy as np

from . import LAMBDA0


def bistatic_gradient(tx, rx, p) -> np.ndarray:
    """g(p) = unit(p-tx) + unit(p-rx). |g| in [0, 2]; ~0 exactly on the LOS segment."""
    tx, rx, p = (np.asarray(a, float) for a in (tx, rx, p))
    u1 = (p - tx) / (np.linalg.norm(p - tx) + 1e-9)
    u2 = (p - rx) / (np.linalg.norm(p - rx) + 1e-9)
    return u1 + u2


def _default_speeds(fs, lam):
    """Speed grid capped by sampling: f = |v.g|/lam <= 2v/lam must stay under fs/2,
    so unaliased head-on speed is fs*lam/4 (1.54 m/s at 50 Hz TDM). Searching beyond it
    lets an aliased tone fit a CONFIDENT wrong speed — the worst failure mode. Without
    fs the old 2.5 m/s cap is kept for backward compatibility, but callers that know
    their rate should pass it. (node.ino's "3.08 m/s" is the |v.g| bound, i.e. head-on
    |v| of half that — easy to misread.)"""
    vmax = 2.5 if fs is None else min(2.5, 0.9 * fs * lam / 4.0)
    return np.linspace(0.1, vmax, 49)


def infer_velocity(observations, p, lam: float = LAMBDA0,
                   speeds=None, headings_deg=None, fs=None):
    """Grid-search the folded forward model against observed Doppler magnitudes.

    observations: list of (tx_xy, rx_xy, f_hz, weight) for links that passed the
    motion/prominence gates. p: scatterer position estimate (from VRTI).

    Returns dict with v (m/s, one of the +/- pair), speed, heading_deg,
    cost, n_links, and sign_ambiguous=True (inherent to folded measurements).
    """
    if speeds is None:
        speeds = _default_speeds(fs, lam)
    if headings_deg is None:
        headings_deg = np.arange(0.0, 360.0, 3.0)

    G = np.array([bistatic_gradient(tx, rx, p) for tx, rx, _, _ in observations])
    f_meas = np.array([o[2] for o in observations], float)
    w = np.array([o[3] for o in observations], float)
    w = w / (w.sum() + 1e-12)

    H = np.deg2rad(headings_deg)
    S, Th = np.meshgrid(speeds, H, indexing="ij")
    vx, vy = S * np.cos(Th), S * np.sin(Th)
    # predicted |v.g|/lambda for every grid point and link: (ns, nh, L)
    pred = np.abs(vx[..., None] * G[:, 0] + vy[..., None] * G[:, 1]) / lam
    cost = (w * (pred - f_meas) ** 2).sum(axis=-1)

    i, j = np.unravel_index(int(np.argmin(cost)), cost.shape)
    v = np.array([vx[i, j], vy[i, j]])
    return {
        "v": v,
        "speed": float(np.linalg.norm(v)),
        "heading_deg": float(np.degrees(np.arctan2(v[1], v[0])) % 360.0),
        "cost": float(cost[i, j]),
        "n_links": len(observations),
        "sign_ambiguous": True,
        "cost_grid": (speeds, headings_deg, cost),
    }


def infer_velocity_joint(observations, p_init, lam: float = LAMBDA0,
                         p_radius: float = 1.5, p_step: float = 0.25,
                         speeds=None, headings_deg=None, fs=None):
    """Joint (p, v) refinement: search positions near p_init, pick the (p, v) whose
    folded forward model best explains ALL Doppler magnitudes.

    Rationale: the coarse position estimate (VRTI matched-field) is the weakest
    input — a 1-2 m error bends every g_l and biases v. But the Doppler magnitudes
    themselves constrain position: with L >= 4 links there are L observations for
    4 unknowns (px, py, vx, vy). Seeding the p-search from VRTI keeps the joint
    problem well-conditioned. Same +/-v ambiguity as infer_velocity.
    """
    if speeds is None:
        speeds = _default_speeds(fs, lam)
    if headings_deg is None:
        headings_deg = np.arange(0.0, 360.0, 3.0)

    p_init = np.asarray(p_init, float)
    offs = np.arange(-p_radius, p_radius + 1e-9, p_step)
    best = None
    for dx in offs:
        for dy in offs:
            p = p_init + np.array([dx, dy])
            res = infer_velocity(observations, p, lam=lam,
                                 speeds=speeds, headings_deg=headings_deg)
            if best is None or res["cost"] < best["cost"]:
                res = dict(res)
                res["p"] = p
                best = res
    best.pop("cost_grid", None)
    return best


def infer_velocity_signed(observations, p):
    """Velocity from SIGNED per-link Doppler — no ±v ambiguity, no grid search.

    observations: list of (tx_xy, rx_xy, f_signed_hz, weight) where f_signed follows
    ratios.py's convention (f = -d_dot/lambda; approach ⇒ positive). Each link gives one
    LINEAR equation  g_l(p) · v = -lambda * f_l,  so v comes from weighted least squares.
    The fold that made infer_velocity search a ±-symmetric cost is gone — this is why
    the signed channel exists (docs/signed-doppler.md).

    Returns dict like infer_velocity (v, speed, heading_deg, cost, n_links) with
    sign_ambiguous=False, or None with < 2 usable links / degenerate geometry.
    """
    if len(observations) < 2:
        return None
    G = np.array([bistatic_gradient(tx, rx, p) for tx, rx, _, _ in observations])
    f = np.array([o[2] for o in observations], float)
    w = np.sqrt(np.maximum([o[3] for o in observations], 0.0))
    A = G * w[:, None]
    if np.linalg.matrix_rank(A) < 2:   # all gradients parallel: v not identifiable
        return None
    v, *_ = np.linalg.lstsq(A, -LAMBDA0 * f * w, rcond=None)
    resid = A @ v + LAMBDA0 * f * w
    return {
        "v": v,
        "speed": float(np.linalg.norm(v)),
        "heading_deg": float(np.degrees(np.arctan2(v[1], v[0])) % 360.0),
        "cost": float((resid ** 2).sum()),
        "n_links": len(observations),
        "sign_ambiguous": False,
    }


def infer_velocity_signed_joint(observations, p_init,
                                p_radius: float = 1.5, p_step: float = 0.25):
    """Joint (p, v) refinement for the signed inversion — same rationale as
    infer_velocity_joint (a 1-2 m p error bends every g_l), but each candidate p is a
    closed-form solve instead of a grid search, so this is cheap."""
    p_init = np.asarray(p_init, float)
    offs = np.arange(-p_radius, p_radius + 1e-9, p_step)
    best = None
    for dx in offs:
        for dy in offs:
            p = p_init + np.array([dx, dy])
            res = infer_velocity_signed(observations, p)
            if res is not None and (best is None or res["cost"] < best["cost"]):
                res["p"] = p
                best = res
    return best


def heading_error_deg(true_heading_deg: float, est_heading_deg: float) -> float:
    """Plain circular heading error — NO fold. Use with the signed inversion."""
    d = abs((est_heading_deg - true_heading_deg) % 360.0)
    return min(d, 360.0 - d)


def folded_heading_error_deg(true_heading_deg: float, est_heading_deg: float) -> float:
    """Heading error acknowledging the inherent 180-degree (+/-v) ambiguity."""
    d = abs((est_heading_deg - true_heading_deg) % 360.0)
    d = min(d, 360.0 - d)
    return min(d, abs(180.0 - d))
