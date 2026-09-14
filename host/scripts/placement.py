#!/usr/bin/env python3
"""Score the node layout in config/room.yaml and, with --optimize, find a better one.

The placement numbers in room.yaml (worst-voxel sensitivity and where, cond(G) median,
share of the room under cond 10) were computed by hand on 2026-07-30; this repeats them
from the kernels the engines already use (wisent/placement.py) and searches for a layout
that scores higher. Run it BEFORE placing boards, or before moving one.

    python scripts/placement.py                          # score the layout in config/room.yaml
    python scripts/placement.py --optimize --fix 0,1,2,3 # move only node 4
    python scripts/placement.py --optimize --seed 3      # every node free (mid-room answers:
                                                         #   the search knows nothing about walls,
                                                         #   power or furniture — --fix is how you
                                                         #   tell it what cannot move)

A half-filled room file is REFUSED, not scored: live_ui.load_room silently substitutes a
fallback room size and drops a node without coordinates, and a fallback geometry scores
the same numbers as the real one — so this script inspects the file text itself first.
room.yaml's header says it: positions never written down are just bytes.
"""

import argparse
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from wisent.placement import layout_metrics, optimize
from live_ui import load_room

DEFAULT_ROOM = pathlib.Path(__file__).resolve().parents[2] / "config" / "room.yaml"
# load_room's own patterns (live_ui.py), so the refusal and the read agree on what a
# size / node / coordinate line is.
SIZE_RE = re.compile(r"size_m:\s*\[([\d.]+),\s*([\d.]+)\]")
NODE_RE = re.compile(r"node_id:\s*(\d+)")
XY_RE = re.compile(r"xy_m:\s*\[\s*([\d.+-]+)\s*,\s*([\d.+-]+)\s*\]")


def refuse(path, what):
    print(f"placement: {path} {what}", file=sys.stderr)
    sys.exit(2)


def check_room_text(path):
    """Refuse before scoring: missing file, no size_m, no node at all, or any listed node
    without an xy_m line — in that order, one line, exit 2."""
    try:
        raw = pathlib.Path(path).read_text()
    except OSError:
        refuse(path, "not found")
    lines = [line.split("#")[0] for line in raw.splitlines()]
    if not SIZE_RE.search("\n".join(lines)):
        refuse(path, "has no size_m — fill in config/room.yaml")
    listed, missing, cur = [], [], None
    for line in lines:
        nm = NODE_RE.search(line)
        if nm:
            if cur is not None:
                missing.append(cur)
            cur = int(nm.group(1))
            listed.append(cur)
        elif cur is not None and XY_RE.search(line):
            cur = None
    if cur is not None:
        missing.append(cur)
    if not listed:
        refuse(path, "has no nodes — fill in config/room.yaml")
    if missing:
        refuse(path, f"has no xy_m for node(s) {', '.join(map(str, missing))} — fill in config/room.yaml")


def main():
    ap = argparse.ArgumentParser(description="score a node layout, optionally improve it")
    ap.add_argument("--room", default=str(DEFAULT_ROOM), help="room.yaml (default: config/room.yaml)")
    ap.add_argument("--cell", type=float, default=0.4, help="grid cell in metres (default 0.4)")
    ap.add_argument("--optimize", action="store_true", help="search for a better layout")
    ap.add_argument("--seed", type=int, default=0, help="search seed (same seed, same answer)")
    ap.add_argument("--candidates", type=int, default=400, help="random layouts to draw")
    ap.add_argument("--fix", default="", help="comma-separated node ids kept where they are")
    args = ap.parse_args()

    check_room_text(args.room)
    nodes, room, src = load_room(args.room)
    if "fallback" in src:                        # cannot happen after the scan; refuse anyway
        refuse(args.room, "could not be parsed — fill in config/room.yaml")

    m = layout_metrics(nodes, room, args.cell)
    print(f"nodes: {len(nodes)} from {pathlib.Path(args.room).name}")
    print(f"worst-voxel sensitivity: {m['worst_voxel']:.4f} at ({m['worst_xy'][0]:.1f}, {m['worst_xy'][1]:.1f})")
    print(f"cond(G) median: {m['cond_median']:.4f}")
    print(f"room under cond<10: {100 * m['cond_frac_ok']:.0f}%")

    if args.optimize:
        fixed = {}
        for tok in args.fix.split(","):
            if tok.strip():
                i = int(tok)
                if i not in nodes:
                    refuse(args.room, f"has no node {i} to --fix")
                fixed[i] = nodes[i]
        best, bm, _ = optimize(room, len(nodes), fixed=fixed, current=nodes,
                               n_candidates=args.candidates, seed=args.seed, cell=args.cell)
        factor = bm["worst_voxel"] / max(m["worst_voxel"], 1e-12)
        print(f"optimized worst-voxel sensitivity: {bm['worst_voxel']:.4f} at "
              f"({bm['worst_xy'][0]:.1f}, {bm['worst_xy'][1]:.1f})  ({factor:.2f}x current)")
        for i in sorted(best):
            print(f"node {i}: [{best[i][0]:.1f}, {best[i][1]:.1f}]")


if __name__ == "__main__":
    main()
