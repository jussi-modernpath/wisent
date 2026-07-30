"""Physics-based synthetic CSI — VALIDATION ONLY.

HARD RULE (anti-RuView): no live-path module may import this file, and no output of
this file may flow into a demo. scripts/validate_synthetic.py enforces the import rule.

Model per link: H(t, k) = H_static(k) + a(t) * exp(-j * 2*pi * d(t) / lambda_k) + noise,
with d(t) the bistatic path via the scatterer, a(t) a bistatic-radar-ish amplitude
1/(r_tx * r_rx), random static phase per subcarrier (creates realistic per-subcarrier
Fresnel blind spots), and an erratic per-frame AGC gain (exercises L1 normalization).
"""

import numpy as np

C = 299792458.0
F0 = 2.437e9
SC_SPACING = 312.5e3


def subcarrier_freqs(n_sc: int = 52) -> np.ndarray:
    idx = np.concatenate([np.arange(-26, 0), np.arange(1, 27)])[:n_sc]
    return F0 + idx * SC_SPACING


# Measured hardware null maps (recordings/tdm4.npz, 17429 frames): DC + guard bands,
# per 64-item LTF block. Block 0 (LLTF, 52 used) and block 1 (HT-LTF, 56 used) differ
# because the two LTFs use different subcarrier counts.
_HW128_NULLS_B0 = frozenset([0] + list(range(27, 38)))
_HW128_NULLS_B1 = frozenset([0] + list(range(29, 36)))


def simulate_link_csi(tx, rx, traj, fs=100.0, n_sc=52, refl_gain=2.0,
                      noise=0.02, agc=True, phase_random=True, sto_ns=1.0,
                      n_static_paths=None, layout=None, rng=None) -> np.ndarray:
    """COMPLEX CSI (T, n_sc) with the full per-packet corruption model: random common
    phase (CFO/PLL), erratic AGC gain, and STO jitter (linear-in-frequency phase).

    sto_ns default 1.0: the RMS sampling-time jitter measured on a real ESP32-S3 via
    the ratio-phase-stability bound (docs/signed-doppler.md §Evidence). Raise it to
    stress-test; signed-Doppler recovery degraded near 50 ns in validation.

    n_static_paths: if None (default), the static channel is random per subcarrier — rich
    multipath, and the FRIENDLIEST case for cross-subcarrier ratios, since adjacent
    subcarriers then decorrelate completely. Set an integer to model discrete static paths
    with DEFINITE delays instead (1 = strong LoS, the hardest case): the static response
    is then smooth in frequency, and the signed ratio term is suppressed by
    2|sin(pi * dk*df * dtau)| exactly as docs/prior-art-signed-doppler.md §3 predicts.
    Use it to check that pair selection keeps a large enough subcarrier gap.

    layout="hw128": emit the REAL ESP32 buffer layout instead of a linear axis — two
    64-item wrapped LTF blocks (0=DC, 1..31=+1..31, 32..63=-32..-1) measuring the same
    channel, with the hardware-measured guard nulls zeroed. The synthetic signed-Doppler
    checks previously only exercised the simulator's linear fallback, so an off-by-one at
    a block boundary in pair selection would have passed 100% of checks.

    Kept separate from simulate_link_amp (below) so that function's seeded RNG draw
    order — and therefore every M0 validation number — stays bit-identical.
    """
    if layout == "hw128":
        return _simulate_hw128(tx, rx, traj, fs, refl_gain, noise, agc,
                               phase_random, sto_ns, n_static_paths, rng)
    rng = rng if rng is not None else np.random.default_rng(0)
    tx = np.asarray(tx, float)
    rx = np.asarray(rx, float)
    traj = np.asarray(traj, float)

    f = subcarrier_freqs(n_sc)
    lam = C / f
    r1 = np.linalg.norm(traj - tx, axis=1)
    r2 = np.linalg.norm(traj - rx, axis=1)
    d = r1 + r2
    a = refl_gain / np.maximum(r1 * r2, 0.25)

    T = len(d)
    if n_static_paths is None:
        H_static = (rng.uniform(0.5, 1.5, n_sc)
                    * np.exp(1j * rng.uniform(0, 2 * np.pi, n_sc)))[None, :]
    else:
        # Discrete static paths with definite delays: LoS first, then extra reflectors.
        d_los = float(np.linalg.norm(tx - rx))
        H_static = np.exp(-1j * 2 * np.pi * f * (d_los / C))[None, :]
        for _ in range(max(0, int(n_static_paths) - 1)):
            excess = d_los + rng.uniform(0.5, 8.0)
            H_static = H_static + (0.5 * rng.uniform(0.3, 1.0)
                                   * np.exp(1j * rng.uniform(0, 2 * np.pi))
                                   * np.exp(-1j * 2 * np.pi * f * (excess / C))[None, :])
    H_dyn = a[:, None] * np.exp(-1j * 2 * np.pi * f[None, :] * (d[:, None] / C))
    n = noise * (rng.standard_normal((T, n_sc)) + 1j * rng.standard_normal((T, n_sc)))
    H = H_static + H_dyn + n
    if phase_random:  # per-packet common phase — random on this hardware, always on live
        H = H * np.exp(1j * rng.uniform(0, 2 * np.pi, T))[:, None]
    if agc:
        H = H * rng.uniform(0.5, 2.0, T)[:, None]
    if sto_ns:
        tau = (sto_ns * 1e-9) * rng.standard_normal(T)[:, None]
        H = H * np.exp(1j * 2 * np.pi * (f - F0)[None, :] * tau)
    return H.astype(np.complex64)


def _simulate_hw128(tx, rx, traj, fs, refl_gain, noise, agc, phase_random,
                    sto_ns, n_static_paths, rng):
    """Hardware-layout CSI: 128 pairs = LLTF block + HT-LTF block, wrapped indexing."""
    rng = rng if rng is not None else np.random.default_rng(0)
    tx = np.asarray(tx, float); rx = np.asarray(rx, float)
    traj = np.asarray(traj, float)
    T = len(traj)
    sc = np.array([k if k <= 31 else k - 64 for k in range(64)])
    f = F0 + sc * SC_SPACING
    r1 = np.linalg.norm(traj - tx, axis=1); r2 = np.linalg.norm(traj - rx, axis=1)
    d = r1 + r2
    a = refl_gain / np.maximum(r1 * r2, 0.25)
    if n_static_paths is None:
        A = (rng.uniform(0.5, 1.5, 64)
             * np.exp(1j * rng.uniform(0, 2 * np.pi, 64)))[None, :]
    else:
        d_los = float(np.linalg.norm(tx - rx))
        A = np.exp(-1j * 2 * np.pi * f * (d_los / C))[None, :]
        for _ in range(max(0, int(n_static_paths) - 1)):
            excess = d_los + rng.uniform(0.5, 8.0)
            A = A + (0.5 * rng.uniform(0.3, 1.0)
                     * np.exp(1j * rng.uniform(0, 2 * np.pi))
                     * np.exp(-1j * 2 * np.pi * f * (excess / C))[None, :])
    B = a[:, None] * np.exp(-1j * 2 * np.pi * f[None, :] * (d[:, None] / C))
    H = np.zeros((T, 128), dtype=np.complex128)
    for b, nulls in ((0, _HW128_NULLS_B0), (1, _HW128_NULLS_B1)):
        n = noise * (rng.standard_normal((T, 64)) + 1j * rng.standard_normal((T, 64)))
        blk = A + B + n                      # both blocks: same channel, fresh noise
        for k in nulls:
            blk[:, k] = 0.0
        H[:, b * 64:(b + 1) * 64] = blk
    if phase_random:
        H = H * np.exp(1j * rng.uniform(0, 2 * np.pi, T))[:, None]
    if agc:
        H = H * rng.uniform(0.5, 2.0, T)[:, None]
    if sto_ns:
        tau = (sto_ns * 1e-9) * rng.standard_normal(T)[:, None]
        ff = np.tile(f, 2)[None, :]
        H = H * np.exp(1j * 2 * np.pi * (ff - F0) * tau)
    return H.astype(np.complex64)


def simulate_link_amp(tx, rx, traj, fs=100.0, n_sc=52, refl_gain=2.0,
                      noise=0.02, agc=True, rng=None) -> np.ndarray:
    """Amplitude CSI (T, n_sc) for one link and one scatterer trajectory (T, 2)."""
    rng = rng if rng is not None else np.random.default_rng(0)
    tx = np.asarray(tx, float)
    rx = np.asarray(rx, float)
    traj = np.asarray(traj, float)

    lam = C / subcarrier_freqs(n_sc)
    r1 = np.linalg.norm(traj - tx, axis=1)
    r2 = np.linalg.norm(traj - rx, axis=1)
    d = r1 + r2
    a = refl_gain / np.maximum(r1 * r2, 0.25)

    H_static = np.exp(1j * rng.uniform(0, 2 * np.pi, n_sc))[None, :]
    H_dyn = a[:, None] * np.exp(-1j * 2 * np.pi * d[:, None] / lam[None, :])
    T = len(d)
    n = noise * (rng.standard_normal((T, n_sc)) + 1j * rng.standard_normal((T, n_sc)))
    amp = np.abs(H_static + H_dyn + n).astype(np.float32)
    if agc:  # erratic per-frame gain, as measured on real ESP32s
        amp *= rng.uniform(0.5, 2.0, size=(T, 1)).astype(np.float32)
    return amp


# ---- trajectories ----

def breathing_traj(p0, direction, fs, dur_s, bpm, depth_m=0.005, rng=None):
    """Chest at p0 oscillating depth_m along unit `direction` at bpm."""
    rng = rng if rng is not None else np.random.default_rng(1)
    t = np.arange(int(dur_s * fs)) / fs
    u = np.asarray(direction, float)
    u = u / (np.linalg.norm(u) + 1e-12)
    disp = depth_m * np.sin(2 * np.pi * (bpm / 60.0) * t)
    sway = 0.0005 * rng.standard_normal((len(t), 2))  # sub-mm body sway
    return np.asarray(p0, float)[None, :] + disp[:, None] * u[None, :] + sway


def jitter_traj(p0, fs, dur_s, step_m=0.02, extent_m=0.15, rng=None):
    """Person fidgeting near p0 — a bounded random walk (drives VRTI variance)."""
    rng = rng if rng is not None else np.random.default_rng(2)
    T = int(dur_s * fs)
    steps = step_m * rng.standard_normal((T, 2))
    walk = np.cumsum(steps, axis=0)
    walk -= walk.mean(axis=0)
    r = np.linalg.norm(walk, axis=1, keepdims=True)
    walk *= np.minimum(1.0, extent_m / np.maximum(r, 1e-9))
    return np.asarray(p0, float)[None, :] + walk


def walk_traj(p_start, v, fs, dur_s):
    """Constant-velocity walk. v: (vx, vy) m/s."""
    t = np.arange(int(dur_s * fs)) / fs
    return np.asarray(p_start, float)[None, :] + t[:, None] * np.asarray(v, float)[None, :]


def all_links(nodes):
    """All K(K-1)/2 node pairs (round-robin TDM topology)."""
    nodes = [np.asarray(n, float) for n in nodes]
    return [(nodes[i], nodes[j])
            for i in range(len(nodes)) for j in range(i + 1, len(nodes))]
