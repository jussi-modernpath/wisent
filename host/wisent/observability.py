"""Link observability check — does each link's motion energy track a walker's KNOWN
position? This decides, from data already in hand, whether the VRTI negative is a
link-count problem (front end works, inversion starved) or a front-end/geometry problem
(more links won't help).

Two quantities per link, deliberately kept apart:
  signal_db — did this link move AT ALL? (peak vs median energy)
  ratio     — WHEN it moved, was the walker in its sensitive region?

`ratio` is a *conditional* statistic, not a waveform correlation: take the link's most
active moments and compare the median expected sensitivity there against the median over
all moments. That makes it immune to the monotonic "walker generally near/far" trend which
inflates plain cross-correlation for links the walker merely approached.

A link that barely moved is `quiet` — uninformative, and it can neither convict nor acquit
the front end. Only links that carried real signal and still show ratio ~ 1 are evidence
that the front end is not tracking position.

Dependency-light: numpy only. Never imports sim.
"""

import numpy as np


def bistatic_sensitivity(tx, rx, p, eps=0.36):
    """s(p) = |g(p)| / (r_tx * r_rx), the VRTI matched-field kernel.

    |g| = |unit(p-tx) + unit(p-rx)| vanishes on the LOS segment — a link is blind to
    motion on its own line of sight — so this is the right visibility weight.
    """
    tx, rx, p = np.asarray(tx, float), np.asarray(rx, float), np.asarray(p, float)
    d1, d2 = p - tx, p - rx
    r1, r2 = np.linalg.norm(d1), np.linalg.norm(d2)
    g = d1 / max(r1, 1e-9) + d2 / max(r2, 1e-9)
    return np.linalg.norm(g) / max(r1 * r2, eps)


def walker_path(path_xy, times, t0, t1, leg_bounds=None):
    """Piecewise-linear walker position at each time in `times`.

    path_xy: node coordinates in the ORDER walked. leg_bounds: measured leg-boundary
    times if you have them; otherwise legs are assumed equal-duration across [t0, t1],
    which is the honest default when leg timings were not recorded. Times outside
    [t0, t1] clamp to the nearest endpoint.
    """
    path_xy = np.asarray(path_xy, float)
    n_legs = len(path_xy) - 1
    bounds = (np.linspace(t0, t1, n_legs + 1) if leg_bounds is None
              else np.array([t0, *leg_bounds, t1], float))
    out = np.zeros((len(times), 2))
    for i, t in enumerate(times):
        if t <= t0:
            out[i] = path_xy[0]
        elif t >= t1:
            out[i] = path_xy[-1]
        else:
            leg = int(np.searchsorted(bounds, t, side="right") - 1)
            leg = min(max(leg, 0), n_legs - 1)
            frac = (t - bounds[leg]) / max(bounds[leg + 1] - bounds[leg], 1e-9)
            out[i] = path_xy[leg] + frac * (path_xy[leg + 1] - path_xy[leg])
    return out


def _active_ratio(energy, s, active_frac=0.25):
    """Trend-free position-tracking evidence: median expected sensitivity during the
    link's most-active moments, over the median across all moments.

    The denominator is floored RELATIVE to the link's own sensitivity scale. An absolute
    epsilon is not enough: when the assumed path keeps the walker far from a link the
    median sensitivity underflows and the ratio explodes (observed: 2.4e10), which reads
    as overwhelming evidence when it is actually a divide-by-nothing.
    """
    energy = np.asarray(energy, float)
    k = max(4, int(len(energy) * active_frac))
    active_idx = np.argsort(energy)[-k:]
    denom = max(np.median(s), 1e-3 * np.max(s), 1e-12)
    return float(np.median(s[active_idx]) / denom)


def link_observability(times, energies, links_xy, path_xy, t0, t1,
                       leg_bounds=None, active_frac=0.25,
                       good_thresh=1.3, weak_thresh=1.1, quiet_db=6.0):
    """Per-link verdict: [{link, ratio, signal_db, verdict}]."""
    times = np.asarray(times, float)
    energies = np.asarray(energies, float)
    p = walker_path(path_xy, times, t0, t1, leg_bounds)
    out = []
    for l, (tx, rx) in enumerate(links_xy):
        s = np.array([bistatic_sensitivity(tx, rx, p[i]) for i in range(len(times))])
        e = energies[:, l]
        signal_db = 10.0 * np.log10((np.max(e) + 1e-12) / (np.median(e) + 1e-12))
        ratio = _active_ratio(e, s, active_frac)
        if signal_db < quiet_db:
            verdict = "quiet"                     # barely moved — uninformative
        elif ratio >= good_thresh:
            verdict = "position-sensitive"
        elif ratio >= weak_thresh:
            verdict = "weak"
        else:
            verdict = "flat/insensitive"          # moved, but not with position
        out.append({"link": l, "ratio": round(ratio, 2),
                    "signal_db": round(float(signal_db), 1), "verdict": verdict})
    return out


def timing_robust(times, energies, links_xy, path_xy, t0, t1, good_links=None,
                  schemes=None):
    """Does the verdict survive not knowing the leg timings?

    `walker_path` assumes equal-duration legs unless told otherwise. If the identity of
    the best position-tracking link changes when that assumption changes, the verdict is
    an artefact of the assumption and the honest answer is 'inconclusive, go time the
    legs'. Returns (stable: bool, winners: list, ratios: dict).
    """
    T = t1 - t0
    if schemes is None:
        schemes = {"equal": None,
                   "slow first": [t0 + 0.45 * T, t0 + 0.65 * T],
                   "slow last": [t0 + 0.25 * T, t0 + 0.45 * T],
                   "front-loaded": [t0 + 0.20 * T, t0 + 0.40 * T],
                   "back-loaded": [t0 + 0.55 * T, t0 + 0.75 * T]}
    winners, ratios = [], {}
    for name, lb in schemes.items():
        r = link_observability(times, energies, links_xy, path_xy, t0, t1, leg_bounds=lb)
        ratios[name] = [d["ratio"] for d in r]
        pool = good_links if good_links else list(range(len(r)))
        pool = [i for i in pool if r[i]["verdict"] != "quiet"] or pool
        winners.append(max(pool, key=lambda i: r[i]["ratio"]))
    return len(set(winners)) == 1, winners, ratios


def summarize(results, good_links=None):
    """One-line fork verdict, keyed only on links that carried real signal."""
    idx = dict(enumerate(results))
    live = [i for i, d in idx.items() if d["verdict"] != "quiet"]
    pool = [i for i in (good_links if good_links else list(idx)) if i in live]
    if not pool:
        return ("Inconclusive: no trusted link carried real signal on this path. "
                "Re-run with a walk that crosses their sensitive regions.")
    best = max(pool, key=lambda i: idx[i]["ratio"])
    if idx[best]["ratio"] >= 1.3:
        return (f"Front end IS position-sensitive (best trusted live link {best}, "
                f"ratio={idx[best]['ratio']:.2f}). VRTI failure is starvation — "
                f"more/better-placed links is the lever.")
    return (f"Trusted links carried signal but it does not track position "
            f"(best link {best}, ratio={idx[best]['ratio']:.2f}). Front-end/geometry "
            f"problem — more links would replicate it. Fix placement first.")
