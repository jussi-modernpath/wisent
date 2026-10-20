#!/usr/bin/env python3
"""End-to-end synthetic validation of the wisent DSP pipeline (roadmap M0).

Checks, in order:
  0. Sim isolation: no live-path module imports wisent.sim (the anti-RuView rule).
  1. Breathing: 17 bpm subject at 2 m recovered within +/-1 bpm; empty room -> no reading.
  2. VRTI: fidgeting person localized to < 1.5 m in a 6x6 m room, 5 nodes / 10 links.
  3. LinkBVP: walker at 1.0 m/s, heading 45 deg — folded heading error < 25 deg,
     speed within 30%, using the VRTI position estimate (not ground truth).
  4. Signed Doppler (docs/signed-doppler.md): sign, oscillation gate, strong-LoS delay
     gradient, magnitude consistency, hardware layout, unfolded heading.
  5. Multi-rate breathing lines (Epic 282): estimate_rates on simulated rooms (seeds 1-5)
     and constructed spectra — lines, harmonic and single-link labels, disagreement, the
     documented drift limitation — and estimate_bpm pinned to a frozen 52ddd8d copy.

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

# ---------- 5. multi-rate breathing lines (Epic 282: SR-1592, UR-256) ----------
# estimate_rates reports every supported respiratory-band spectral line across links, with
# harmonic and single-link labels and a disagreement flag. Lines are NOT sources or people.
# Room checks: the 10-link room above, 60 s, seeds 1-5. Breather j's trajectory uses
# default_rng(seed*10 + j) and is shared by all links; link k uses default_rng(seed*1000 + k)
# (the Epic 282 feasibility layout). Seeds are fixed and never tuned: a failing seed is a
# real FAIL. Harmonic labels are checked on CONSTRUCTED spectra only — in Epic 282's scratch
# feasibility runs (not in this repo), no link's dominant peak was a harmonic in 120
# simulated link runs.
import inspect

from scipy.signal import welch
from wisent.breathing import estimate_rates, _psd_peaks

ROOM_SEEDS = (1, 2, 3, 4, 5)
CENTRE = np.array([3.0, 3.0])


def breathing_room(breathers, seed, dur_s=60.0, static_drift=0.0):
    """Sanitised amplitude of the 10 room links; breathers are breathing_traj_shaped kwargs."""
    trajs = [sim.breathing_traj_shaped(fs=FS, dur_s=dur_s,
                                       rng=np.random.default_rng(seed * 10 + j), **b)
             for j, b in enumerate(breathers)]
    if not trajs:  # empty room: one static scatterer far away, as in the control above
        trajs = [np.tile(np.array([[20.0, 20.0]]), (int(dur_s * FS), 1))]
    return [sanitize(sim.simulate_link_amp_multi(a, b, trajs, fs=FS, static_drift=static_drift,
                                                 rng=np.random.default_rng(seed * 1000 + k)),
                     FS, detrend_win_s=8.0)
            for k, (a, b) in enumerate(links)]


def facing_centre(p0, bpm, **kw):
    return dict(p0=p0, direction=CENTRE - np.asarray(p0, float), bpm=bpm, **kw)


def constructed_links(spec, seed, dur_s=60.0):
    """CONSTRUCTED (not simulated) clean link amplitude, 52 subcarriers per link: white noise
    (0.02 RMS) plus, per (bpm, amplitude) component, a sinusoid with a random phase and a
    0.5-1.5x amplitude spread on each subcarrier. spec holds one component list per link."""
    rng = np.random.default_rng(seed)
    t = np.arange(int(dur_s * FS)) / FS
    out = []
    for comps in spec:
        x = 0.02 * rng.standard_normal((len(t), 52))
        for bpm_c, a in comps:
            ph = rng.uniform(0, 2 * np.pi, 52)
            amps = a * rng.uniform(0.5, 1.5, 52)
            x += amps[None, :] * np.sin(2 * np.pi * bpm_c / 60.0 * t[:, None] + ph[None, :])
        out.append(x)
    return out


def rates_of(clusters):
    return [c["rate_bpm"] for c in clusters]


def near(rates, target, tol=1.0):
    return [r for r in rates if abs(r - target) <= tol]


def fmt(r):
    return (f"lines {[round(x, 1) for x in rates_of(r['lines'])]} "
            f"harm {[(round(h['rate_bpm'], 1), round(h['of_bpm'], 1), h['order']) for h in r['harmonics']]} "
            f"single {[round(x, 1) for x in rates_of(r['single_link'])]} "
            f"dis {r['disagreement']} used {r['n_links_used']}")


def run_check(name, fn):
    """A missing, empty or malformed result prints [FAIL], never a traceback."""
    try:
        ok, detail = fn()
    except Exception as e:
        ok, detail = False, f"raised {type(e).__name__}: {e}"
    check(name, bool(ok), detail)


def per_seed(breathers, accept, **kw):
    """Run estimate_rates on each room seed; accept(result) -> bool. Returns (ok, detail)."""
    oks, rows = [], []
    for s in ROOM_SEEDS:
        r = estimate_rates(breathing_room(breathers, s, **kw), FS)
        ok_s = bool(accept(r))
        oks.append(ok_s)
        rows.append(f"s{s} {[round(x, 1) for x in rates_of(r['lines'])]}" if ok_s
                    else f"s{s} FAIL {fmt(r)}")
    return all(oks), f"{sum(oks)}/{len(oks)} seeds: " + "; ".join(rows)


TWO_BREATHERS = [facing_centre((1.5, 1.5), 12.0), facing_centre((4.5, 4.5), 18.0)]


def _multi_source():
    def accept(r):
        L = sorted(rates_of(r["lines"]))
        return (len(L) == 2 and abs(L[0] - 12.0) <= 1.0 and abs(L[1] - 18.0) <= 1.0
                and all(c["support"] >= 2 for c in r["lines"])
                and r["disagreement"] is True and r["n_links_used"] == 10)
    ok, d = per_seed(TWO_BREATHERS, accept)
    return ok, f"breathers at 12 and 18 bpm -> 2 lines, disagreement, 10 links; {d}"


def _multi_harmonic():
    r = estimate_rates(constructed_links([[(11.0, 1.0), (22.0, 0.2)]] * 4
                                         + [[(11.0, 0.3), (22.0, 1.0)]] * 2, 1), FS)
    L, H = r["lines"], r["harmonics"]
    ok = (len(L) == 1 and abs(L[0]["rate_bpm"] - 11.0) <= 1.0
          and len(H) == 1 and abs(H[0]["rate_bpm"] - 22.0) <= 1.5
          and abs(H[0]["of_bpm"] - 11.0) <= 1.0 and H[0]["order"] == 2
          and r["disagreement"] is False)
    return ok, f"constructed 4x(11 + weak 22) + 2x(22 dominant) -> 1 line, 22 an order-2 harmonic: {fmt(r)}"


def _multi_single_source():
    def accept(r):
        L = rates_of(r["lines"])
        return len(L) == 1 and abs(L[0] - 15.0) <= 1.0 and r["disagreement"] is False
    ok, d = per_seed([dict(p0=(3.0, 3.0), direction=(1.0, 0.0), bpm=15.0)], accept)
    return ok, f"one breather at 15 bpm -> 1 line, no disagreement; {d}"


def _multi_empty_room():
    def accept(r):
        return (not r["lines"] and not r["harmonics"] and not r["single_link"]
                and r["disagreement"] is False and r["n_links_used"] == 10)
    ok, d = per_seed([], accept)
    return ok, f"empty room -> nothing, disagreement false, 10 links; {d}"


def _multi_disagreement():
    r = estimate_rates(constructed_links([[(28.8, 1.0)], [(34.0, 1.0)]], 1), FS)
    S = sorted(rates_of(r["single_link"]))
    ok = (not r["lines"] and len(S) == 2 and abs(S[0] - 28.8) <= 1.0
          and abs(S[1] - 34.0) <= 1.0 and r["disagreement"] is True)
    return ok, f"constructed 2 links at 28.8 / 34.0 bpm (the logged disagreement's shape): {fmt(r)}"


def _multi_nonsine():
    def accept(r):
        L = rates_of(r["lines"])
        return len(L) == 1 and abs(L[0] - 12.0) <= 1.0 and r["disagreement"] is False
    ok, d = per_seed([facing_centre((1.5, 1.5), 12.0, h2=0.5, depth_m=0.010)], accept)
    return ok, f"one 12 bpm breather, 50% second harmonic, 10 mm -> exactly 1 line; {d}"


def _multi_min_links():
    r = estimate_rates(constructed_links([[(14.0, 1.0)]] * 3 + [[(24.0, 1.0)]], 1), FS)
    L, S = r["lines"], r["single_link"]
    ok = (len(L) == 1 and abs(L[0]["rate_bpm"] - 14.0) <= 1.0
          and len(S) == 1 and abs(S[0]["rate_bpm"] - 24.0) <= 1.0 and S[0]["support"] == 1)
    return ok, f"constructed 3x14 + 1x24 -> 14 a line, 24 single-link: {fmt(r)}"


def _clauses():
    C = constructed_links

    def all_clusters(r):
        return r["lines"] + r["harmonics"] + r["single_link"]

    def entry(lp, target, tol=0.75):
        hits = [e for e in lp if abs(e[0] - target) <= tol]
        return hits[0] if len(hits) == 1 else None

    def a():  # a link shorter than 60 s is not used; the others keep their positions
        lk = C([[(14.0, 1.0)]] * 3, 1)
        lk.insert(1, lk[0][: int(30 * FS)])
        r = estimate_rates(lk, FS)
        lp = r["link_peaks"]
        return (lp[1] is None and r["n_links_used"] == 3
                and all(lp[i] and abs(lp[i][0][0] - 14.0) <= 1.0 for i in (0, 2, 3)))

    def b():  # no usable link: abstain (true negative — also holds for the stub)
        r = estimate_rates([x[: int(30 * FS)] for x in C([[(14.0, 1.0)]] * 3, 1)], FS)
        return (r["n_links_used"] == 0 and r["disagreement"] is None
                and not all_clusters(r) and r["link_peaks"] == [None] * 3)

    def c():  # two tones on one link: two entries, strongest first, neither an overtone
        lp = estimate_rates(C([[(12.0, 1.0), (18.0, 0.8)]] + [[(12.0, 1.0)]] * 2, 1), FS)["link_peaks"][0]
        return (len(lp) == 2 and abs(lp[0][0] - 12.0) <= 0.75 and abs(lp[1][0] - 18.0) <= 0.75
                and lp[0][1] >= lp[1][1] and lp[0][2] is None and lp[1][2] is None)

    def d():  # a weaker 2x peak on the same link is an overtone and joins no cluster
        r = estimate_rates(C([[(11.0, 1.0), (22.0, 0.4)]] + [[(11.0, 1.0)]] * 2, 1), FS)
        e = entry(r["link_peaks"][0], 22.5)
        return (e is not None and e[2] is not None and abs(e[2] - 11.0) <= 1.0
                and not near(rates_of(all_clusters(r)), 22.0, 1.5))

    def e():  # the strongest entry equals estimate_bpm's single-link peak exactly
        lk = C([[(12.0, 1.0), (18.0, 0.5)]] * 3, 1)
        first = estimate_rates(lk, FS)["link_peaks"][0][0]
        return first[:2] == estimate_bpm([lk[0]], FS, return_links=True)[2][0]

    def f():  # adjacent bins form one cluster at the (prominence - 1)-weighted mean rate
        r = estimate_rates(C([[(12.0, 1.0)], [(13.5, 0.4)]], 1), FS)
        cl = all_clusters(r)
        m = [(bpm_e, p) for lp in r["link_peaks"] for bpm_e, p, ov in lp if ov is None]
        want = sum((p - 1.0) * bpm_e for bpm_e, p in m) / sum(p - 1.0 for _, p in m)
        return (len(cl) == 1 and cl[0]["links"] == [0, 1]
                and 12.0 < cl[0]["rate_bpm"] < 13.5 and abs(cl[0]["rate_bpm"] - want) <= 1e-9)

    def g():  # a chain of occupied adjacent bins is one cluster
        cl = all_clusters(estimate_rates(C([[(12.0, 1.0)], [(13.5, 1.0)], [(15.0, 1.0)]], 1), FS))
        return len(cl) == 1 and cl[0]["support"] == 3

    def h():  # support counts distinct links, not peaks
        cl = all_clusters(estimate_rates(C([[(12.0, 1.0), (15.0, 1.0)], [(13.5, 1.0)]], 1), FS))
        return len(cl) == 1 and cl[0]["support"] == 2 and cl[0]["links"] == [0, 1]

    def i():  # disjoint links, harmonic better supported: both guards apply
        r = estimate_rates(C([[(11.0, 1.0)]] * 2 + [[(22.0, 1.0)]] * 4, 1), FS)
        return (len(near(rates_of(r["lines"]), 11.0)) == 1 and len(near(rates_of(r["lines"]), 22.0)) == 1
                and not r["harmonics"])

    def j():  # the harmonic test is on integer bins: 13.5 is 2x the 6.0 bin
        H = estimate_rates(C([[(6.7, 1.0), (13.4, 0.2)]] * 4 + [[(6.7, 0.3), (13.4, 1.0)]] * 2, 1),
                           FS)["harmonics"]
        return (len(H) == 1 and abs(H[0]["rate_bpm"] - 13.5) <= 0.75
                and abs(H[0]["of_bpm"] - 6.0) <= 0.75 and H[0]["order"] == 2)

    def k():  # the LOWEST qualifying lower cluster is the fundamental
        H = estimate_rates(C([[(6.0, 1.0), (9.0, 1.0)]] * 4
                             + [[(18.0, 1.0), (6.0, 0.5), (9.0, 0.5)]] * 2, 1), FS)["harmonics"]
        return (len(H) == 1 and abs(H[0]["rate_bpm"] - 18.0) <= 0.75
                and abs(H[0]["of_bpm"] - 6.0) <= 0.75 and H[0]["order"] == 3)

    def l():  # prominence_ratio must exceed 1
        try:
            estimate_rates(C([[(12.0, 1.0)]] * 2, 1), FS, prominence_ratio=1.0)
        except ValueError:
            return True
        return False

    def m():  # the lower edge bin (6 bpm) can hold a peak
        L = estimate_rates(C([[(6.0, 1.0)]] * 3, 1), FS)["lines"]
        return len(L) == 1 and abs(L[0]["rate_bpm"] - 6.0) <= 0.75 and L[0]["support"] == 3

    def n():  # support guard alone: 22 is better supported than 11, so not its harmonic
        r = estimate_rates(C([[(22.0, 1.0), (11.0, 0.5)]] * 2 + [[(22.0, 1.0)]] * 2, 1), FS)
        return (len(near(rates_of(r["lines"]), 11.0)) == 1 and len(near(rates_of(r["lines"]), 22.0)) == 1
                and not r["harmonics"])

    def o():  # co-occurrence guard alone: 18 shares no link with the 9 line
        r = estimate_rates(C([[(9.0, 1.0)]] * 6 + [[(18.0, 1.0)]] * 4, 1), FS)
        return (len(near(rates_of(r["lines"]), 9.0)) == 1 and len(near(rates_of(r["lines"]), 18.0)) == 1
                and not r["harmonics"])

    def p():  # an order-3 overtone on one link (8 bpm lands on the 7.5 bin)
        lp = estimate_rates(C([[(8.0, 1.0), (24.0, 0.4)]] + [[(8.0, 1.0)]] * 2, 1), FS)["link_peaks"][0]
        e24 = entry(lp, 24.0)
        return e24 is not None and e24[2] is not None and abs(e24[2] - 7.5) <= 1e-9

    def q():  # overtones are marked against KEPT peaks only: 24 is n=4 of 6, 12 is not kept
        r = estimate_rates(C([[(6.0, 1.0), (12.0, 0.5), (24.0, 0.3)]] + [[(6.0, 1.0)]] * 2, 1), FS)
        lp = r["link_peaks"][0]
        e12, e24 = entry(lp, 12.0), entry(lp, 24.0)
        S24 = [cl for cl in r["single_link"] if abs(cl["rate_bpm"] - 24.0) <= 0.75]
        return (e12 is not None and e12[2] is not None and abs(e12[2] - 6.0) <= 0.75
                and e24 is not None and e24[2] is None
                and len(S24) == 1 and S24[0]["support"] == 1)

    def r_():  # the 0.6 Hz bin is outside the inherited band mask: a 36 bpm tone finds nothing
        r = estimate_rates(C([[(36.0, 1.0)]] * 3, 1), FS)
        return r["n_links_used"] == 3 and r["link_peaks"] == [[], [], []] and not all_clusters(r)

    def s():  # peak picker: a plateau yields its lower bin; a sub-ratio local maximum none
        psd = np.array([1.0, 1.0, 1.0, 6.0, 6.0, 1.0, 1.1, 1.0, 1.2, 2.5, 1.0, 1.0])
        return [jj for jj, _ in _psd_peaks(psd, np.arange(1, 11), 3.0)] == [3]

    keys = {"lines", "harmonics", "single_link", "rates_bpm", "n_links_used", "disagreement",
            "link_peaks"}

    def t():  # exact result shape: no count key, ascending lists, record fields, rates_bpm
        def shaped(r):
            recs_ok = all(
                set(cl) == ({"rate_bpm", "links", "support"}
                            | ({"of_bpm", "order"} if part == "harmonics" else set()))
                and cl["support"] == len(cl["links"]) and cl["links"] == sorted(set(cl["links"]))
                for part in ("lines", "harmonics", "single_link") for cl in r[part])
            ascending = all(rates_of(r[part]) == sorted(rates_of(r[part]))
                            for part in ("lines", "harmonics", "single_link"))
            return (set(r) == keys and recs_ok and ascending
                    and r["rates_bpm"] == rates_of(r["lines"]))
        r1 = estimate_rates(C([[(11.0, 1.0), (22.0, 0.2)]] * 4 + [[(11.0, 0.3), (22.0, 1.0)]] * 2, 1), FS)
        r2 = estimate_rates(C([[(9.0, 1.0)]] * 6 + [[(18.0, 1.0)]] * 4 + [[(27.0, 1.0)]], 1), FS)
        return (shaped(r1) and shaped(r2) and r1["harmonics"] and len(r2["lines"]) == 2
                and r2["single_link"])

    def u():  # disagreement counts lines AND single-link clusters: 1 line + 1 single -> True
        r = estimate_rates(C([[(14.0, 1.0)]] * 3 + [[(24.0, 1.0)]], 1), FS)
        return len(r["lines"]) == 1 and len(r["single_link"]) == 1 and r["disagreement"] is True

    def v():  # the window cutoff is int(60*fs) samples: one sample short is not used
        lk = C([[(14.0, 1.0)]] * 3, 1)
        r = estimate_rates([lk[0][: int(60 * FS) - 1], lk[1][: int(60 * FS)], lk[2]], FS)
        return (r["link_peaks"][0] is None and r["link_peaks"][1] is not None
                and r["n_links_used"] == 2)

    def harmonic_of_9(r):
        H = r["harmonics"]
        return (len(near(rates_of(r["lines"]), 9.0)) == 1 and not near(rates_of(r["lines"]), 18.0)
                and len(H) == 1 and abs(H[0]["rate_bpm"] - 18.0) <= 0.75
                and abs(H[0]["of_bpm"] - 9.0) <= 0.75 and H[0]["order"] == 2)

    def w():  # support guard is "at least": EQUAL support still labels the harmonic
        return harmonic_of_9(estimate_rates(C([[(18.0, 1.0), (9.0, 0.5)]] * 3, 1), FS))

    def x():  # co-occurrence is "at least half": exactly half of its links shared suffices
        return harmonic_of_9(estimate_rates(C([[(9.0, 1.0)]] * 2 + [[(18.0, 1.0), (9.0, 0.5)]] * 2
                                              + [[(18.0, 1.0)]] * 2, 1), FS))

    def y():  # overtone of the first MATCHING kept peak, not only the strongest (21 > 6 > 12)
        lp = estimate_rates(C([[(21.0, 1.0), (6.0, 0.7), (12.0, 0.4)]] + [[(6.0, 1.0)]] * 2, 1),
                            FS)["link_peaks"][0]
        e21, e12 = entry(lp, 21.0), entry(lp, 12.0)
        return (e21 is not None and e21[2] is None and lp[0] == e21
                and e12 is not None and e12[2] is not None and abs(e12[2] - 6.0) <= 0.75)

    subs = dict(a=a, b=b, c=c, d=d, e=e, f=f, g=g, h=h, i=i, j=j, k=k, l=l, m=m, n=n, o=o,
                p=p, q=q, r=r_, s=s, t=t, u=u, v=v, w=w, x=x, y=y)
    failed = []
    for name, fn in subs.items():
        try:
            ok = bool(fn())
        except Exception:
            ok = False
        if not ok:
            failed.append(name)
    return not failed, (f"all {len(subs)} constructed sub-assertions (a)-(y) hold" if not failed
                        else f"FAILED {len(failed)}/{len(subs)} sub-assertions: {', '.join(failed)}")


def _known_limits():
    # (iii) ASSERTED, deterministic: a well-supported low line (e.g. a drift artifact)
    # relabels a co-occurring real line at 2x as its harmonic.
    r = estimate_rates(constructed_links([[(9.0, 0.5), (18.0, 1.0)]] * 4
                                         + [[(9.0, 0.5)]] * 2, 1), FS)
    H = [c for c in r["harmonics"] if abs(c["rate_bpm"] - 18.0) <= 1.0]
    ok = (len(near(rates_of(r["lines"]), 9.0)) == 1 and not near(rates_of(r["lines"]), 18.0)
          and len(H) == 1 and H[0]["order"] == 2 and abs(H[0]["of_bpm"] - 9.0) <= 1.0)
    return ok, (f"(iii) constructed 6x9 + 4x stronger 18 -> 18 relabelled an order-2 harmonic "
                f"of the 9 line: {fmt(r)}")


def report_limit(label, breathers, **kw):
    """REPORTED, never asserted (Epic 282 decision 10): a change here is reported at
    completion and the docs updated — seeds and models are never tuned to move it."""
    try:
        rows = []
        for s in ROOM_SEEDS:
            r = estimate_rates(breathing_room(breathers, s, **kw), FS)
            rows.append(f"s{s} lines {[round(x, 1) for x in rates_of(r['lines'])]} harm "
                        f"{[(round(c['rate_bpm'], 1), round(c['of_bpm'], 1)) for c in r['harmonics']]}")
        print(f"    KNOWN LIMITATION (reported): {label}: " + "; ".join(rows))
    except Exception as e:
        print(f"    KNOWN LIMITATION (reported): {label}: not computed ({type(e).__name__}: {e})")


def _estimate_bpm_52ddd8d(clean_links, fs, band=(0.1, 0.6), top_k=5, prominence_ratio=3.0,
                          return_links=False):
    """FROZEN ORACLE — wisent.breathing.estimate_bpm exactly as at revision 52ddd8d (code
    verbatim, docstring dropped). Never edit it: SR-1592 criterion 5 pins estimate_bpm to it."""
    fused = None
    freqs = None
    link_peaks = []
    for clean in clean_links:
        T = clean.shape[0]
        nperseg = min(T, int(fs * 40))
        f, P = welch(clean, fs=fs, nperseg=nperseg, axis=0)
        mask = (f >= band[0]) & (f <= band[1])
        if not mask.any():
            continue
        # top-k subcarriers by in-band power: dodge per-subcarrier Fresnel blind spots
        in_band_power = P[mask].sum(axis=0)
        top = np.argsort(in_band_power)[-top_k:]
        psd = P[:, top].mean(axis=1)
        psd = psd / (psd.sum() + 1e-18)
        # this link's own peak + prominence (weight, and per-link report)
        _idx = np.where(mask)[0]
        _i = _idx[int(np.argmax(psd[_idx]))]
        _prom = float(psd[_i] / (np.median(psd[_idx]) + 1e-18))
        link_peaks.append((float(f[_i] * 60.0), _prom))
        w = max(_prom - 1.0, 0.0)
        fused = psd * w if fused is None else fused + psd * w
        freqs = f
    if fused is None or fused.sum() <= 0:
        return (None, 0.0, link_peaks) if return_links else (None, 0.0)

    mask = (freqs >= band[0]) & (freqs <= band[1])
    idx = np.where(mask)[0]
    i = idx[int(np.argmax(fused[idx]))]
    conf = float(fused[i] / (np.median(fused[idx]) + 1e-18))
    if conf < prominence_ratio:
        return (None, conf, link_peaks) if return_links else (None, conf)

    # parabolic interpolation for sub-bin rate resolution
    if 0 < i < len(fused) - 1:
        a, b, c = fused[i - 1], fused[i], fused[i + 1]
        denom = a - 2 * b + c
        delta = 0.5 * (a - c) / denom if abs(denom) > 1e-18 else 0.0
        delta = float(np.clip(delta, -0.5, 0.5))
    else:
        delta = 0.0
    df = freqs[1] - freqs[0]
    bpm = float((freqs[i] + delta * df) * 60.0)
    if return_links:
        return bpm, conf, link_peaks
    return bpm, conf


def _fused_unchanged():
    lk = breathing_room(TWO_BREATHERS, 1)
    lk.insert(3, lk[0][: int(1 * FS)])  # 1 s: no Welch bin falls in the band, link skipped
    new0, old0 = estimate_bpm(lk, FS), _estimate_bpm_52ddd8d(lk, FS)
    new1 = estimate_bpm(lk, FS, return_links=True)
    old1 = _estimate_bpm_52ddd8d(lk, FS, return_links=True)
    same_sig = inspect.signature(estimate_bpm) == inspect.signature(_estimate_bpm_52ddd8d)
    ok = new0 == old0 and new1 == old1 and same_sig
    return ok, (f"estimate_bpm equals the frozen 52ddd8d copy exactly (return_links False and "
                f"True; 11 links incl. a skipped 1 s link): {'identical' if ok else 'DIFFERS'}, "
                f"signature {'unchanged' if same_sig else 'CHANGED'}, "
                f"bpm {old0[0]}, {len(old1[2])} link peaks")


run_check("breathing-multi-source", _multi_source)
run_check("breathing-multi-harmonic", _multi_harmonic)
run_check("breathing-multi-single-source", _multi_single_source)
run_check("breathing-multi-empty-room", _multi_empty_room)
run_check("breathing-multi-disagreement", _multi_disagreement)
run_check("breathing-multi-nonsine", _multi_nonsine)
run_check("breathing-multi-min-links", _multi_min_links)
run_check("breathing-multi-clauses", _clauses)
run_check("breathing-multi-known-limits", _known_limits)
report_limit("(i) empty room + 2% slow static-channel drift", [], static_drift=0.02)
report_limit("(ii) one 15 bpm breather + 5 mm slow posture sway",
             [dict(p0=(3.0, 3.0), direction=(1.0, 0.0), bpm=15.0, sway_m=0.005)])
run_check("breathing-fused-unchanged", _fused_unchanged)

# ---------- summary ----------
n_ok = sum(ok for _, ok in results)
print(f"\n{n_ok}/{len(results)} checks passed")
sys.exit(0 if n_ok == len(results) else 1)
