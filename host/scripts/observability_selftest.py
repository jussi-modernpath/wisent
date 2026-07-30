#!/usr/bin/env python3
"""Synthetic self-test for observability.py — proves the diagnostic separates the three
cases it must never confuse, before it is trusted on real data:

  A. strong signal driven by the WALKER      -> 'position-sensitive'
  B. strong signal driven by an INTERFERER   -> flagged, NOT position-sensitive
  C. barely moved (walker never near it)     -> 'quiet' (uninformative, not convicted)

B vs A is the whole point: can it tell a position-driven swing from an equally large
position-INDEPENDENT one (a wandering pet, or the real run's links that swung 1-14x
seemingly independent of where the person was)? If yes, a 'flat/insensitive' verdict on a
live link is real evidence about the front end.

Run: cd host && python scripts/observability_selftest.py
"""
import pathlib
import sys

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from wisent.sanitize import sanitize
from wisent.features import motion_energy
from wisent.observability import link_observability, summarize
from wisent import sim

FS, DUR = 100.0, 60.0
ts = np.linspace(0, 1, int(DUR * FS))[:, None]
line = lambda a, b: np.asarray(a, float) + ts * (np.asarray(b, float) - np.asarray(a, float))

walker = line((1.0, 1.0), (9.0, 5.0))      # ground truth target
phantom = line((9.5, 0.8), (9.5, 5.2))     # interferer moving elsewhere

LINKS_XY = [((5.0, 3.0), (0.5, 0.5)),      # A: endpoint on the walker's mid-path
            ((9.5, 0.5), (9.5, 5.5)),      # B: spans where the PHANTOM walks
            ((0.3, 5.7), (0.7, 5.7))]      # C: tiny, far corner, walker never near
DRIVER = [walker, phantom, walker]

cols, times = [], None
for k, ((tx, rx), drv) in enumerate(zip(LINKS_XY, DRIVER)):
    amp = sim.simulate_link_amp(tx, rx, drv, fs=FS, refl_gain=2.0, noise=0.03,
                                rng=np.random.default_rng(1100 + k))
    times, E = motion_energy(sanitize(amp, FS, detrend_win_s=1.0), FS)
    cols.append(E)
E = np.column_stack(cols)

res = link_observability(times, E, LINKS_XY, [(1.0, 1.0), (9.0, 5.0)],
                         t0=0.0, t1=float(times[-1]))
labels = ["A walker-driven", "B interferer-driven", "C off-path"]
print(f"{'case':<22}{'ratio':>7}{'sig_dB':>8}   verdict")
for lab, d in zip(labels, res):
    print(f"{lab:<22}{d['ratio']:>7}{d['signal_db']:>8}   {d['verdict']}")
print("\n" + summarize(res, good_links=[0, 1]))

a_ok = res[0]["verdict"] == "position-sensitive"
b_ok = res[1]["signal_db"] >= 6.0 and res[1]["ratio"] < res[0]["ratio"] \
       and res[1]["verdict"] != "position-sensitive"
c_ok = res[2]["verdict"] == "quiet"
print(f"\nA walker->sensitive: {'PASS' if a_ok else 'FAIL'}  |  "
      f"B interferer->flagged: {'PASS' if b_ok else 'FAIL'}  |  "
      f"C off-path->quiet: {'PASS' if c_ok else 'FAIL'}")
sys.exit(0 if (a_ok and b_ok and c_ok) else 1)
