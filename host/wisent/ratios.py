"""Cross-subcarrier ratio features — the signed-Doppler channel.

Physics and validation record: docs/signed-doppler.md. In short: per-packet random
phase, AGC gain and STO are common-mode across subcarriers within one packet, so the
ratio H(f1,t)/H(f2,t) cancels them exactly (measured on ESP32-S3: ratio phase stable to
0.02-0.04 rad across packets while raw CSI phase is uniform-random). To first order in
|dynamic/static| the ratio contains a single term rotating with the scatterer's bistatic
path length d(t), and its winding direction is the sign of d-dot = v . g_link.

Sign convention (used consistently here and in linkbvp.infer_velocity_signed):
    f_signed = -d_dot / lambda
so a scatterer APPROACHING the link (d shrinking) produces f_signed > 0.

This is the one sanctioned use of CSI complex values on the live path: complex math
WITHIN a packet only. Nothing here compares raw phase across packets.
"""

import numpy as np
from scipy.ndimage import uniform_filter1d

from .csi_io import block_of, subcarrier_map


# Default subcarrier gap. NOT a free parameter — see the delay-gradient note in
# signed_dominant_doppler(). Measured sign accuracy vs gap on a delay-structured channel
# (person 2 m off a 4 m link, dtau 2-8 ns): dk=1 -> 25%, dk=2 -> 33%, dk=4 -> 75%,
# dk>=8 -> 100%. Chance below ~1 MHz of separation, solid from ~2.5 MHz. 16 leaves margin
# for smaller dtau (a scatterer near the LoS line) while keeping plenty of usable pairs.
DEFAULT_DK = 16          # SUBCARRIERS of separation, not buffer indices


def select_pairs(H: np.ndarray, dk: int = DEFAULT_DK, n_pairs: int = 12):
    """Choose subcarrier pairs separated by `dk` SUBCARRIERS within one LTF block.

    Three constraints, all load-bearing:
    - **Same LTF block.** The buffer holds LLTF then HT-LTF, each 64 items with its own
      map (csi_io). A pair straddling the boundary compares two different LTF estimates
      of overlapping frequencies — not two subcarriers, and the delay-gradient model
      simply does not apply to it.
    - **Subcarrier distance, not index distance.** Index 20 and index 40 are subcarriers
      +20 and −24, i.e. 44 apart. Using index arithmetic silently randomises the
      frequency gap that the whole method depends on.
    - **Denominator strength.** A deep-fade denominator makes the ratio explode and a
      faded numerator carries no signal, so candidates are ranked by min(|H_a|, |H_b|).
      Ranking happens WITHIN a fixed frequency gap, because the signed term scales as
      2|sin(pi * dk*df * dtau)| and too small a gap annihilates it however clean the
      subcarriers are.
    """
    n = H.shape[1]
    amp = np.abs(H).mean(axis=0)
    blk, sc = block_of(n), subcarrier_map(n)
    by_key = {(int(blk[k]), int(sc[k])): k for k in range(n) if amp[k] > 0}
    cands = []
    for (b, s_), k in by_key.items():
        j = by_key.get((b, s_ + dk))
        if j is not None:
            cands.append((min(amp[k], amp[j]), s_, k, j))
    cands.sort(reverse=True)
    # Prefer UNIQUE subcarrier pairs before duplicating across LTF blocks. Both blocks
    # estimate the same channel, so a (b0, s) pair and its (b1, s) twin have identical
    # coefficients and differ only in estimation noise — a duplicate adds averaging, not
    # diversity. Ranking purely by amplitude picked twins (blocks share amplitudes) and
    # halved effective pair diversity; measured on the hw128 layout at 20 ns STO stress,
    # that cost ~16% sign accuracy.
    first, extra, seen = [], [], set()
    for _, s_, k, j in cands:
        (first if s_ not in seen else extra).append((int(k), int(j)))
        seen.add(s_)
    return (first + extra)[:n_pairs]


def subcarrier_ratios(H: np.ndarray, pairs) -> np.ndarray:
    """Complex ratio series (T, P) for the given pairs. H: complex CSI (T, S)."""
    num = np.stack([H[:, k1] for k1, _ in pairs], axis=1)
    den = np.stack([H[:, k2] for _, k2 in pairs], axis=1)
    return num / (den + 1e-9)


def detrend_ratios(R: np.ndarray, fs: float, win_s: float = 1.0) -> np.ndarray:
    """Remove the quasi-static component (the A1/A2 term) with a moving mean, per pair.
    Same rationale as sanitize.detrend, applied to a complex series."""
    w = max(3, int(win_s * fs))
    trend = (uniform_filter1d(R.real, size=w, axis=0, mode="nearest")
             + 1j * uniform_filter1d(R.imag, size=w, axis=0, mode="nearest"))
    return R - trend


def spectral_lines(H: np.ndarray, fs: float, fmin: float = 1.5,
                   prominence_ratio: float = 4.0, dk: int = DEFAULT_DK, n_pairs: int = 12,
                   detrend_win_s: float = 1.0):
    """Prominent lines in a STILL-scene ratio spectrum -> list of signed freqs (Hz).

    The per-deployment baseline idea from architecture.md, applied to this channel:
    anything spectrally prominent while the scene is known-still is environment
    (fans, compressors — a ~36 Hz fan-class line was measured 2026-07-29), and callers
    pass these to signed_dominant_doppler(notch=...) so a stationary interferer can
    never masquerade as a walker.
    """
    pairs = select_pairs(H, dk=dk, n_pairs=n_pairs)
    if not pairs:
        return []
    R = detrend_ratios(subcarrier_ratios(H, pairs), fs, win_s=detrend_win_s)
    T = R.shape[0]
    if T < 8:
        return []
    X = (R - R.mean(axis=0, keepdims=True)) * np.hanning(T)[:, None]
    S = (np.abs(np.fft.fft(X, axis=0)) ** 2).sum(axis=1)
    freqs = np.fft.fftfreq(T, 1.0 / fs)
    band = np.abs(freqs) >= fmin
    med = np.median(S[band]) + 1e-18
    return [float(f) for f, p, b in zip(freqs, S, band) if b and p / med >= prominence_ratio]


def signed_dominant_doppler(R: np.ndarray, fs: float, fmin: float = 1.5,
                            prominence_ratio: float = 3.0, mirror_ratio: float = 3.0,
                            notch=(), notch_bw: float = 1.5):
    """Signed spectral peak of detrended ratio series R (T, P).

    Unlike features.dominant_doppler (magnitude spectrum of a real signal), the input
    here is complex, so its spectrum is asymmetric and the peak's SIGN is meaningful:
    f > 0 means the path length is shrinking (approach). Returns (f_signed, conf) or
    (None, conf) when gated — no reading beats a wrong one.

    **What the winding does and does not mean.** The winding direction is the Doppler
    sign; the *amplitude* of the signed term is weighted by delay selectivity. Writing the
    static and dynamic paths with definite delays, the first-order coefficient is

        |B1/A1 - B2/A2| ~ |B/A| * 2|sin(pi * dk*df * dtau)|,  dtau = tau_dyn - tau_static

    so the signed term collapses when the subcarrier gap or the dynamic/static delay
    difference goes to zero — a ratio-space analogue of the Fresnel blind spot, and
    plausibly why the published single-antenna cross-frequency method (CFCC) is
    LoS-limited. This method reads sign, weighted by delay selectivity; it is not "pure"
    Doppler. See docs/prior-art-signed-doppler.md §3.

    Two gates:
    - prominence: peak power vs band median, 3.0 to match the breathing engine's gate.
      Raising the subcarrier gap raises the noise floor's structure too, and at 2.0 a
      spurious one-sided noise peak could clear both gates (measured 2026-07-29).
    - mirror asymmetry: peak power vs power at MINUS the peak frequency. Progressive
      motion (a walker) rotates one way and is one-sided in the spectrum; an oscillatory
      path (vibration) swings both ways and is ±symmetric, with an arbitrary winner.
      Found on hardware 2026-07-29: a ~36 Hz line (fan-class interferer, ~2160 RPM)
      passed the prominence gate with a coin-flip sign on every still segment; the
      mirror gate is what rejects that whole artifact class.
    """
    T = R.shape[0]
    if T < 8:
        return None, 0.0
    X = (R - R.mean(axis=0, keepdims=True)) * np.hanning(T)[:, None]
    S = (np.abs(np.fft.fft(X, axis=0)) ** 2).sum(axis=1)
    freqs = np.fft.fftfreq(T, 1.0 / fs)
    band = np.abs(freqs) >= fmin
    for f0 in notch:                               # still-baseline interference lines
        band &= np.abs(freqs - f0) > notch_bw
    if not band.any():
        return None, 0.0
    idx = np.flatnonzero(band)
    i = idx[int(np.argmax(S[idx]))]
    conf = float(S[i] / (np.median(S[idx]) + 1e-18))
    if conf < prominence_ratio:
        return None, conf
    j = int(np.argmin(np.abs(freqs + freqs[i])))   # bin nearest to -f_peak
    if S[i] / (S[j] + 1e-18) < mirror_ratio:
        return None, conf                          # oscillatory, not progressive
    # parabolic interpolation on the power spectrum for sub-bin frequency
    if 0 < i < len(S) - 1:
        a, b, c = S[i - 1], S[i], S[i + 1]
        denom = a - 2 * b + c
        delta = 0.5 * (a - c) / denom if abs(denom) > 1e-18 else 0.0
        delta = float(np.clip(delta, -0.5, 0.5))
    else:
        delta = 0.0
    df = freqs[1] - freqs[0] if len(freqs) > 1 else 0.0
    return float(freqs[i] + delta * df), conf


def link_signed_doppler(H: np.ndarray, fs: float, dk: int = DEFAULT_DK, n_pairs: int = 12,
                        fmin: float = 1.5, prominence_ratio: float = 3.0,
                        mirror_ratio: float = 3.0, notch=(), notch_bw: float = 1.5,
                        detrend_win_s: float = 1.0):
    """One-call per-link signed Doppler: complex CSI (T, S) -> (f_signed, conf).

    The link-level primitive LinkBVP consumes: pair selection -> ratios -> detrend ->
    signed spectral peak with prominence, mirror-asymmetry, and still-baseline gates
    (pass notch=spectral_lines(H_still, fs) from a known-still stretch).
    """
    pairs = select_pairs(H, dk=dk, n_pairs=n_pairs)
    if not pairs:
        return None, 0.0
    R = detrend_ratios(subcarrier_ratios(H, pairs), fs, win_s=detrend_win_s)
    return signed_dominant_doppler(R, fs, fmin=fmin,
                                   prominence_ratio=prominence_ratio,
                                   mirror_ratio=mirror_ratio,
                                   notch=notch, notch_bw=notch_bw)


def corroborated_signed_doppler(H: np.ndarray, fs: float, max_rel_err: float = 0.35,
                                **kw):
    """Signed Doppler, emitted ONLY when the amplitude path corroborates it.

    The ratio and amplitude channels fail differently: a one-sided noise line from a
    denominator fade is a RATIO artifact, and the amplitude spectrogram will not show a
    prominent tone near |f_signed|. Requiring agreement (within max_rel_err, the bound the
    synthetic suite validates at ~1% median error) vetoes exactly the leak class the null
    controls kept finding — the residual that signed-doppler.md's still-baseline notch
    could not close. Needs no ground truth, so it runs on live data.

    Returns (f_signed | None, conf, note). On real hardware walking data the two channels
    currently disagree at ~51% median, so expect this gate to veto often — that is the
    honest behaviour until the disagreement is understood.
    """
    from .features import doppler_spectrogram, dominant_doppler
    from .sanitize import sanitize
    f_s, conf = link_signed_doppler(H, fs, **kw)
    if f_s is None:
        return None, conf, "no signed reading"
    fmin = kw.get("fmin", 1.5)
    cl = sanitize(np.abs(H), fs, detrend_win_s=1.0)
    fq, _, mag = doppler_spectrogram(cl, fs)
    f_f, _ = dominant_doppler(fq, mag.mean(axis=1), fmin=fmin)
    if f_f is None:
        return None, conf, f"vetoed: amplitude path shows no tone near |{f_s:.1f}| Hz"
    rel = abs(abs(f_s) - f_f) / max(f_f, 1e-9)
    if rel > max_rel_err:
        return None, conf, (f"vetoed: amplitude says {f_f:.1f} Hz, ratio says "
                            f"{abs(f_s):.1f} Hz ({rel:.0%} apart)")
    return f_s, conf, f"corroborated ({rel:.0%} apart)"
