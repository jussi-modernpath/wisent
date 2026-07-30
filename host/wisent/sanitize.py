"""Layer 1: sanitization. Order is load-bearing: Hampel -> L1 norm -> detrend.

Each step neutralizes one documented ESP32 defect (docs/theory.md #2):
Hampel kills impulse outliers; L1 normalization kills the erratic AGC scaling;
detrending removes the static-channel DC so variance/spectral features see only motion.
"""

import numpy as np
from scipy.ndimage import median_filter, uniform_filter1d


def hampel(amp: np.ndarray, k: int = 7, t: float = 3.0) -> np.ndarray:
    """Replace impulse outliers with the rolling median. amp: (T, S)."""
    med = median_filter(amp, size=(2 * k + 1, 1), mode="nearest")
    mad = median_filter(np.abs(amp - med), size=(2 * k + 1, 1), mode="nearest")
    out = amp.copy()
    mask = np.abs(amp - med) > t * 1.4826 * (mad + 1e-9)
    out[mask] = med[mask]
    return out


def l1_normalize(amp: np.ndarray) -> np.ndarray:
    """Per-frame L1 normalization — the published fix for ESP32 AGC erraticness."""
    return amp / (np.mean(np.abs(amp), axis=1, keepdims=True) + 1e-12)


def detrend(amp: np.ndarray, fs: float, win_s: float = 2.0) -> np.ndarray:
    """Subtract per-subcarrier moving mean (window win_s seconds)."""
    w = max(3, int(win_s * fs))
    trend = uniform_filter1d(amp, size=w, axis=0, mode="nearest")
    return amp - trend


def sanitize(amp: np.ndarray, fs: float, detrend_win_s: float = 2.0) -> np.ndarray:
    """Full Layer-1 chain. Input raw amp (T, S), output motion-only residual (T, S)."""
    return detrend(l1_normalize(hampel(amp)), fs, win_s=detrend_win_s)
