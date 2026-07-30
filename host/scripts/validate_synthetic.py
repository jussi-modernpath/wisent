#!/usr/bin/env python3
"""End-to-end synthetic validation of the wisent DSP pipeline (roadmap M0).

Checks, in order:
  0. Sim isolation: no live-path module imports wisent.sim (the anti-RuView rule).
  1. Breathing: 17 bpm subject at 2 m recovered within +/-1 bpm; empty room -> no reading.
  2. VRTI: fidgeting person localized to < 1.5 m in a 6x6 m room, 5 nodes / 10 links.
  3. LinkBVP: walker at 1.0 m/s, heading 45 deg — folded heading error < 25 deg,
     speed within 30%, using the VRTI position estimate (not ground truth).

Run: cd host && python scripts/validate_synthetic.py
"""

import pathlib
import re
import sys

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from wisent import LAMBDA0
from wisent.sanitize import sanitize
from wisent.features import motion_energy, doppler_spectrogram, dominant_doppler
from wisent.vrti import VRTI
from wisent.breathing import estimate_bpm
from wisent.linkbvp import infer_velocity_joint, folded_heading_error_deg
from wisent import sim

FS = 100.0
PASS, FAIL = "PASS", "FAIL"
results = []


def check(name, ok, detail):
    results.append((name, ok))
    print(f"[{PASS if ok else FAIL}] {name}: {detail}")


# ---------- 0. sim isolation ----------
# The live path is the package modules AND the recorder script that feeds them: a
# simulator reachable from live_capture.py would be exactly the RuView failure mode.
host = pathlib.Path(__file__).resolve().parents[1]
live = [host / "wisent" / f for f in
        ("csi_io.py", "sanitize.py", "features.py", "vrti.py", "breathing.py",
         "linkbvp.py", "ratios.py", "observability.py")]
live += [host / "scripts" / f for f in
         ("live_capture.py", "walk_test.py", "station_test.py")]
offenders = [p.name for p in live
             if re.search(r"^\s*(from|import)\s+.*\bsim\b", p.read_text(), re.MULTILINE)]
check("sim-isolation", not offenders,
      f"live path ({len(live)} modules incl. live_capture.py) imports no sim"
      if not offenders else f"OFFENDERS: {offenders}")

# ---------- room geometry (5 nodes, 6x6 m) ----------
nodes = [(0.3, 0.3), (5.7, 0.3), (5.7, 5.7), (0.3, 5.7), (3.0, 0.1)]
links = sim.all_links(nodes)  # 10 links (round-robin TDM topology)

# ---------- 1. breathing ----------
rng = np.random.default_rng(42)
tx, rx = np.array([0.3, 3.0]), np.array([4.3, 3.0])
p0 = np.array([2.3, 3.6])  # 2.3 m from TX, off-LOS
toward_link = (0.5 * (tx + rx) - p0)
TRUE_BPM = 17.0
traj = sim.breathing_traj(p0, toward_link, FS, 60.0, TRUE_BPM, rng=rng)
amp = sim.simulate_link_amp(tx, rx, traj, fs=FS, rng=rng)
clean = sanitize(amp, FS, detrend_win_s=8.0)  # slow signal: gentle detrend
bpm, conf = estimate_bpm([clean], FS)
ok = bpm is not None and abs(bpm - TRUE_BPM) <= 1.0
check("breathing-recovery", ok,
      f"true {TRUE_BPM} bpm, est {bpm if bpm else 'None'} (conf {conf:.1f})")

# empty-room control: static scatterer far away, should yield no confident reading
still = np.tile(np.array([[20.0, 20.0]]), (int(60 * FS), 1))
amp0 = sim.simulate_link_amp(tx, rx, still, fs=FS, rng=rng)
bpm0, conf0 = estimate_bpm([sanitize(amp0, FS, detrend_win_s=8.0)], FS)
check("breathing-no-false-positive", bpm0 is None,
      f"empty room -> {'no reading' if bpm0 is None else f'FALSE {bpm0:.1f} bpm'} (conf {conf0:.1f})")

# ---------- 2. VRTI localization ----------
P_TRUE = np.array([4.2, 3.8])
traj_j = sim.jitter_traj(P_TRUE, FS, 30.0, rng=rng)
energies = []
for k, (a, b) in enumerate(links):
    ampL = sim.simulate_link_amp(a, b, traj_j, fs=FS,
                                 rng=np.random.default_rng(100 + k))
    _, E = motion_energy(sanitize(ampL, FS), FS)
    energies.append(float(np.median(E)))
tomo = VRTI(links, (0, 6), (0, 6))
p_hat, img = tomo.locate(np.array(energies))
err = float(np.linalg.norm(p_hat - P_TRUE))
check("vrti-localization", err < 1.5,
      f"true {P_TRUE.tolist()}, est {np.round(p_hat, 2).tolist()}, err {err:.2f} m")

# ---------- 3. LinkBVP velocity from folded Doppler ----------
SPEED, HEADING = 1.0, 45.0
v_true = SPEED * np.array([np.cos(np.radians(HEADING)), np.sin(np.radians(HEADING))])
traj_w = sim.walk_traj((1.0, 1.0), v_true, FS, 4.0)
t_mid = 2.0
# 4g. VRTI gain calibration MECHANICS. Real-data validation lives in the roadmap
# (station2 LOO: 2/5 -> 5/5 quadrants); a synthetic channel model was tried here and
# DISAGREED with reality about which correction variant helps — so this check asserts
# only what must always hold: gains are recovered from data that follows the fit model,
# and zero gains leave locate() exactly unchanged.
rng_g = np.random.default_rng(77)
nodes_g = {0: (9.5, 4.5), 1: (9.0, 0.4), 2: (2.0, 0.4), 3: (0.2, 3.8), 4: (4.0, 2.2)}
pairs_g = [(a, b) for a in range(5) for b in range(a + 1, 5)]
tomo_g = VRTI([(np.array(nodes_g[a]), np.array(nodes_g[b])) for a, b in pairs_g],
              (0, 10), (0, 4.5), nx=44, ny=20)
spots_g = [(6.2, 4.2), (0.7, 3.5), (2.2, 0.9), (5.0, 2.2), (8.0, 1.0), (7.0, 3.0)]
g_true = rng_g.normal(0, 0.8, len(pairs_g))          # up to ~5x link-gain spread
K_spots = tomo_g._log_kernel(np.array(spots_g))
K_n = K_spots - K_spots.max(axis=0, keepdims=True)   # strongest link at each spot = 1
# floor-relative ratios, as locate() consumes them: 1 + gain * position-response
excess = 20.0 * np.exp(g_true)[:, None] * np.exp(K_n)          * np.exp(rng_g.normal(0, 0.10, K_n.shape))
R_g = (1.0 + excess).T
g_fit, gamma_fit = tomo_g.fit_gains(spots_g[:5], R_g[:5] - 1.0)
est_zero, _ = tomo_g.locate(R_g[5], baseline=np.ones(len(pairs_g)),
                            gains=np.zeros(len(pairs_g)))
est_none, _ = tomo_g.locate(R_g[5], baseline=np.ones(len(pairs_g)))
same_zero = (est_zero is None and est_none is None) or (
    est_zero is not None and est_none is not None and np.allclose(est_zero, est_none))
corr_g = np.corrcoef(g_true - g_true.mean(), g_fit)[0, 1]
check("vrti-gain-calibration",
      corr_g > 0.9 and same_zero,
      f"fitted gains correlate {corr_g:.2f} with injected truth; zero gains leave "
      f"locate() unchanged (identity holds: {same_zero})")

p_mid_true = traj_w[int(t_mid * FS)]

obs, energies_w = [], []
for k, (a, b) in enumerate(links):
    ampL = sim.simulate_link_amp(a, b, traj_w, fs=FS,
                                 rng=np.random.default_rng(200 + k))
    cleanL = sanitize(ampL, FS, detrend_win_s=1.0)  # fast signal: tight detrend
    # per-link motion energy in the mid-crossing second (for VRTI position)
    tE, E = motion_energy(cleanL, FS)
    sel = (tE > t_mid - 0.5) & (tE < t_mid + 0.5)
    energies_w.append(float(np.median(E[sel])) if sel.any() else 0.0)
    # dominant folded Doppler in the mid-crossing frame
    f, tt, mag = doppler_spectrogram(cleanL, FS)
    col = mag[:, int(np.argmin(np.abs(tt - t_mid)))]
    f_hat, c = dominant_doppler(f, col)
    if f_hat is not None:
        obs.append((a, b, f_hat, c))

p_est, _ = tomo.locate(np.array(energies_w))  # coarse position from VRTI
res = infer_velocity_joint(obs, p_est)        # joint (p, v) refinement
h_err = folded_heading_error_deg(HEADING, res["heading_deg"])
s_err = abs(res["speed"] - SPEED) / SPEED
ok = h_err < 25.0 and s_err < 0.30 and res["n_links"] >= 3
check("linkbvp-velocity", ok,
      f"heading err {h_err:.1f} deg (folded), speed {res['speed']:.2f} vs {SPEED} "
      f"({100*s_err:.0f}% err), {res['n_links']} links, "
      f"p refined err {np.linalg.norm(res['p'] - p_mid_true):.2f} m "
      f"(VRTI seed err {np.linalg.norm(p_est - p_mid_true):.2f} m)")

# ---------- 4. signed Doppler (docs/signed-doppler.md) ----------
from wisent.ratios import link_signed_doppler, spectral_lines, DEFAULT_DK
from wisent.linkbvp import infer_velocity_signed_joint, heading_error_deg

# 4a. motion SIGN survives the full corruption model, at 20x the measured STO jitter
ok_sign, n_sign = 0, 0
tx_s, rx_s = np.array([0.0, 0.0]), np.array([4.0, 0.0])
for trial in range(10):
    toward = bool(trial % 2)
    traj_s = (np.array([2.0, 2.5])[None, :]
              + (np.arange(int(3 * FS)) / FS)[:, None]
              * np.array([0.0, -1.0 if toward else 1.0])[None, :])
    Hs = sim.simulate_link_csi(tx_s, rx_s, traj_s, fs=FS, sto_ns=20.0,
                               rng=np.random.default_rng(300 + trial))
    f_s, c_s = link_signed_doppler(Hs, FS)
    if f_s is not None:
        n_sign += 1
        ok_sign += (f_s > 0) == toward  # approach => d shrinks => f_signed > 0
check("signed-doppler-sign", n_sign >= 8 and ok_sign / max(n_sign, 1) >= 0.9,
      f"approach/retreat sign correct {ok_sign}/{n_sign} at 20 ns STO "
      f"(20x the jitter measured on ESP32-S3)")

# 4c: oscillatory interference must NOT produce a signed reading. Models the ~36 Hz
# fan-class line found by the hardware null control (2026-07-29). The displacement is
# 10 mm, not 2 mm, so the 36 Hz line genuinely DOMINATES the ratio spectrum — with a
# weak vibration the detected peak is scattered noise and the check silently tests
# something else (found while raising DEFAULT_DK).
# 4c: the mirror-asymmetry MECHANISM. When an oscillatory scatterer DOMINATES the ratio
# spectrum, its ±symmetry must gate it out — that is the property the gate exists for, and
# the one the hardware null control exercised. Measured mirror ratios for the 36 Hz line:
# 1.3-1.8 against a 3.0 gate.
#
# KNOWN LIMITATION, reported rather than tuned away: on a minority of windows of this
# no-net-motion scene the dominant peak is instead an unrelated ONE-SIDED noise line,
# which clears both gates. The still-baseline notch is the deployed defence (and is what
# made the hardware null control pass 0/4), but it is not a cure in simulation: notching
# the vibration merely promotes the next-strongest noise peak. See
# docs/signed-doppler.md §Limitations before quoting any false-positive rate.
vib_dominant, vib_gated, spurious = 0, 0, 0
for trial in range(6):
    tt = np.arange(int(3 * FS)) / FS
    vib = (np.array([2.0, 2.5])[None, :]
           + 0.010 * np.sin(2 * np.pi * 36.0 * tt)[:, None] * np.array([0.0, 1.0])[None, :])
    Hv = sim.simulate_link_csi(tx_s, rx_s, vib, fs=FS, sto_ns=20.0,
                               rng=np.random.default_rng(600 + trial))
    f_raw, _ = link_signed_doppler(Hv, FS, prominence_ratio=0.0, mirror_ratio=0.0)
    f_v, _ = link_signed_doppler(Hv, FS)
    if f_raw is not None and abs(abs(f_raw) - 36.0) < 2.0:
        vib_dominant += 1
        vib_gated += f_v is None          # the mechanism under test
    elif f_v is not None:
        spurious += 1                     # unrelated one-sided noise line survived
check("signed-doppler-rejects-oscillation",
      vib_dominant >= 2 and vib_gated == vib_dominant,
      f"vibration line dominant in {vib_dominant}/6 windows, gated {vib_gated}/{vib_dominant} "
      f"by mirror symmetry; {spurious}/6 windows leaked an unrelated one-sided noise line "
      f"(known limitation, see docs/signed-doppler.md)")

# 4d: the delay-gradient regime (prior-art review §3). With a STRONG-LoS static path the
# static response is smooth in frequency, so the signed term is suppressed by
# 2|sin(pi*dk*df*dtau)| and a too-small subcarrier gap reads at chance. The default gap
# must survive this; measured cliff: dk=1 -> 25%, dk=2 -> 33%, dk=4 -> 75%, dk>=8 -> 100%.
ok_los, n_los = 0, 0
for trial in range(10):
    toward = bool(trial % 2)
    y0, y1 = (2.5, 1.2) if toward else (1.2, 2.5)
    ys = np.linspace(y0, y1, int(3 * FS))
    traj_los = np.stack([np.full(len(ys), 2.0), ys], axis=1)
    Hl = sim.simulate_link_csi(tx_s, rx_s, traj_los, fs=FS, n_static_paths=1,
                               refl_gain=0.3, noise=0.01,
                               rng=np.random.default_rng(700 + trial))
    f_l, c_l = link_signed_doppler(Hl, FS, fmin=1.0)
    if f_l is not None:
        n_los += 1
        ok_los += (f_l > 0) == toward
check("signed-doppler-delay-gradient", n_los >= 4 and ok_los / max(n_los, 1) >= 0.9,
      f"strong-LoS channel (smooth in frequency, dtau 2-8 ns): sign correct "
      f"{ok_los}/{n_los} at the default dk={DEFAULT_DK} "
      f"({DEFAULT_DK * 312.5e3 / 1e6:.1f} MHz gap)")

# 4e: MAGNITUDE-SIGN CONSISTENCY. The amplitude pipeline yields |f| (folded) and the
# ratio channel yields f (signed) for the SAME motion. They must agree in magnitude — a
# disagreement means one of them is reading an artifact, and unlike the sign itself this
# needs no ground truth, so it works on live data where truth is unavailable.
from wisent.features import doppler_spectrogram as _dspec, dominant_doppler as _dom
cons_ok, cons_n, cons_err = 0, 0, []
for trial in range(6):
    toward = bool(trial % 2)
    traj_c = (np.array([2.0, 2.5])[None, :]
              + (np.arange(int(3 * FS)) / FS)[:, None]
              * np.array([0.0, -1.0 if toward else 1.0])[None, :])
    Hc = sim.simulate_link_csi(tx_s, rx_s, traj_c, fs=FS,
                               rng=np.random.default_rng(800 + trial))
    f_signed, _ = link_signed_doppler(Hc, FS)
    fq, tt, mag = _dspec(sanitize(np.abs(Hc), FS, detrend_win_s=1.0), FS)
    f_folded, _ = _dom(fq, mag[:, mag.shape[1] // 2])
    if f_signed is None or f_folded is None:
        continue
    cons_n += 1
    rel = abs(abs(f_signed) - f_folded) / max(f_folded, 1e-9)
    cons_err.append(rel)
    cons_ok += rel < 0.35
check("signed-folded-magnitude-consistency", cons_n >= 3 and cons_ok >= cons_n - 1,
      f"|f_signed| vs folded |f|: agreed within 35% on {cons_ok}/{cons_n} windows "
      f"(median rel. error {np.median(cons_err) if cons_err else float('nan'):.0%}); "
      f"a free cross-check needing no ground truth")

# 4f: the HARDWARE buffer layout (two wrapped LTF blocks, measured nulls). Every other
# signed check uses n_sc=52, which takes the simulator's linear fallback — so the code
# path hardware actually exercises (wrapped map, guard-null filtering, block-boundary
# exclusion) had zero synthetic coverage, and a boundary off-by-one would have passed.
ok_hw, n_hw = 0, 0
for trial in range(8):
    toward = bool(trial % 2)
    traj_h = (np.array([2.0, 2.5])[None, :]
              + (np.arange(int(3 * FS)) / FS)[:, None]
              * np.array([0.0, -1.0 if toward else 1.0])[None, :])
    # sto_ns=1.0 = the MEASURED silicon jitter: this check covers the LAYOUT code path
    # (wrapped map, guard nulls, block boundaries), not STO stress — 4a covers stress.
    # Documented property, not hidden: at 20 ns stress the hw128 layout runs ~90% vs
    # linear's 100%, because both LTF blocks estimate the SAME channel, so cross-block
    # pair twins add averaging rather than coefficient diversity (mitigated by the
    # unique-subcarrier preference in select_pairs; measured 84% before it).
    Hh = sim.simulate_link_csi(tx_s, rx_s, traj_h, fs=FS, sto_ns=1.0, layout="hw128",
                               rng=np.random.default_rng(900 + trial))
    f_h, c_h = link_signed_doppler(Hh, FS)
    if f_h is not None:
        n_hw += 1
        ok_hw += (f_h > 0) == toward
check("signed-doppler-hw-layout", n_hw >= 6 and ok_hw / max(n_hw, 1) >= 0.85,
      f"sign correct {ok_hw}/{n_hw} on the REAL 128-pair wrapped-block layout with "
      f"measured guard nulls (previously untested by any synthetic check)")

# 4b. the fold is RESOLVED: heading 245 deg sits exactly where the folded pipeline
# reports 65 deg. Full pipeline: complex sim -> amplitude path for VRTI seed ->
# signed ratios -> joint (p, v). Passing requires being near 245, NOT near the mirror.
HEADING_S = 245.0
v_true_s = 1.0 * np.array([np.cos(np.radians(HEADING_S)), np.sin(np.radians(HEADING_S))])
traj_ws = np.array([4.0, 4.0])[None, :] + (np.arange(int(4 * FS)) / FS)[:, None] * v_true_s[None, :]
t_mid_s = 2.0
obs_s, energies_s = [], []
for k, (a, b) in enumerate(links):
    Hk = sim.simulate_link_csi(a, b, traj_ws, fs=FS,
                               rng=np.random.default_rng(400 + k))
    ampk = np.abs(Hk)                       # the amplitude path sees the same channel
    cleank = sanitize(ampk, FS, detrend_win_s=1.0)
    tE, E = motion_energy(cleank, FS)
    sel = (tE > t_mid_s - 0.5) & (tE < t_mid_s + 0.5)
    energies_s.append(float(np.median(E[sel])) if sel.any() else 0.0)
    f_k, c_k = link_signed_doppler(Hk, FS)
    if f_k is not None and c_k > 3.0:
        obs_s.append((a, b, f_k, c_k))
p_est_s, _ = tomo.locate(np.array(energies_s))
res_s = infer_velocity_signed_joint(obs_s, p_est_s)
h_err_s = heading_error_deg(HEADING_S, res_s["heading_deg"]) if res_s else 999.0
mirror_gap = heading_error_deg((HEADING_S + 180.0) % 360.0,
                               res_s["heading_deg"]) if res_s else 0.0
ok = res_s is not None and h_err_s < 20.0 and mirror_gap > 90.0 and res_s["n_links"] >= 4
check("signed-doppler-unfold", ok,
      f"true {HEADING_S} deg, est {res_s['heading_deg']:.1f} deg UNFOLDED "
      f"(err {h_err_s:.1f}, mirror rejected by {mirror_gap:.0f} deg, "
      f"{res_s['n_links']} links, speed {res_s['speed']:.2f})" if res_s
      else "inversion returned None")

# ---------- summary ----------
n_ok = sum(ok for _, ok in results)
print(f"\n{n_ok}/{len(results)} checks passed")
sys.exit(0 if n_ok == len(results) else 1)
