#!/usr/bin/env python3
"""Run the observability diagnostic on a real wisent recording.

The free test that gates the hardware-spend decision: is the VRTI negative starvation
(front end works, too few links) or a front-end/geometry problem (more links will not
help)? Method and its two confounds: wisent/observability.py.

    python scripts/observability_check.py ../recordings/walk_vrti.npz
"""
import argparse
import pathlib
import sys

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from wisent.csi_io import FrameParser, frames_to_segments
from wisent.sanitize import sanitize
from wisent.features import motion_energy
from wisent.observability import link_observability, summarize

# Measured node geometry (config/room.yaml, 2026-07-29)
NODES = {0: (9.0, 4.5), 1: (9.0, 0.5), 2: (2.0, 0.3), 3: (0.0, 4.0), 4: (6.5, 2.5)}


def load_links(npz, fs):
    """-> (times, energies[T,L], links) on a common wall-clock grid, per (tx,rx) pair."""
    z = np.load(npz)
    acc, t0 = {}, None
    for pi in range(8):
        key = f"port{pi}"
        if f"{key}/raw" not in z.files:
            continue
        wall, seqs = z[f"{key}/wall_clock"], z[f"{key}/seq"]
        t0 = wall.min() if t0 is None else min(t0, wall.min())
        fr = FrameParser().feed(z[f"{key}/raw"].tobytes())
        for seg in frames_to_segments(fr):
            if len(seg) < 200:
                continue
            sub = sorted([f for f in fr if f.tx_id == seg.tx_id and f.node_id == seg.node_id],
                         key=lambda f: f.seq)
            if len(sub) < 10:
                continue
            w = np.interp(seg.index, [f.seq for f in sub],
                          np.interp([f.seq for f in sub], seqs, wall))
            t, E = motion_energy(sanitize(seg.amp, fs), fs, win_s=2.0, hop_s=1.0)
            if len(E) < 2:
                continue
            pair = tuple(sorted((seg.tx_id, seg.node_id)))
            acc.setdefault(pair, [[], []])
            acc[pair][0].extend(np.interp(t * fs, np.arange(len(w)), w))
            acc[pair][1].extend(E)
    links = sorted(acc)
    lo = max(min(acc[p][0]) for p in links)
    hi = min(max(acc[p][0]) for p in links)
    grid = np.arange(lo, hi, 1.0)                      # 1 s common grid
    cols = [np.interp(grid, np.array(acc[p][0]), np.array(acc[p][1])) for p in links]
    return grid - lo, np.column_stack(cols), links


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("npz")
    ap.add_argument("--fs", type=float, default=50.0, help="link rate (TDM round rate)")
    ap.add_argument("--path", default="3,2,1,0", help="node ids in the order walked")
    ap.add_argument("--exclude-from-verdict", default="2",
                    help="node ids known to be in an RF hole; their links cannot acquit "
                         "or convict the front end")
    args = ap.parse_args()

    times, energies, links = load_links(args.npz, args.fs)
    path_nodes = [int(x) for x in args.path.split(",")]
    path_xy = [NODES[n] for n in path_nodes]
    links_xy = [(NODES[a], NODES[b]) for a, b in links]
    res = link_observability(times, energies, links_xy, path_xy,
                             t0=float(times[0]), t1=float(times[-1]))

    print(f"{len(links)} links, {len(times)} one-second windows, "
          f"path {'->'.join(map(str, path_nodes))}\n")
    print(f"{'link':<9}{'ratio':>7}{'sig_dB':>8}   verdict")
    for (a, b), r in zip(links, res):
        print(f"({a},{b}){'':<3}{r['ratio']:>7}{r['signal_db']:>8}   {r['verdict']}")

    bad = {int(x) for x in args.exclude_from_verdict.split(",") if x.strip()}
    good = [i for i, (a, b) in enumerate(links) if not (bad & {a, b})]
    print("\n" + summarize(res, good_links=good))
    print("\nLeg timings were not recorded for this walk, so legs are assumed "
          "equal-duration — a real source of smear in `ratio`. Timed legs would sharpen it.")


if __name__ == "__main__":
    main()
