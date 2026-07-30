"""Layer 3: respiration engine. Multi-link Welch fusion with a prominence gate.

Physics constraints inherited from the Fresnel model (docs/theory.md #3.1): single
subject, roughly still, within a few meters, orientation matters, per-link blind
spots exist — which is exactly why we fuse subcarriers AND links.
"""

import numpy as np
from scipy.signal import welch

RESP_BAND = (0.1, 0.6)  # 6..36 breaths/min


def estimate_bpm(clean_links, fs, band=RESP_BAND, top_k=5, prominence_ratio=3.0,
                 return_links=False):
    """clean_links: list of sanitized amp arrays (T, S), one per link.

    Returns (bpm, confidence) or (None, confidence) when no prominent peak —
    reporting nothing beats reporting noise.

    Fusion is weighted by each link's own in-band peak prominence: a link whose subject
    sits in a Fresnel null contributes a flat PSD, and equal-weight summing let it dilute
    the links that could see (this structure is what HID the logged 28.8-vs-34.0 bpm
    link disagreement — fusion averaged it instead of surfacing it). With
    return_links=True the per-link peaks are returned too, so a caller can REPORT
    disagreement rather than silently averaging it.
    """
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
