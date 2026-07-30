"""Layer 2: the link-feature bus. Everything downstream consumes only these."""

import numpy as np
from scipy.signal import stft


def motion_energy(clean: np.ndarray, fs: float, win_s: float = 1.0,
                  hop_s: float = 0.1, top_k: int = 5):
    """Windowed variance of the top-k most active subcarriers.

    clean: sanitized amp (T, S). Returns (times, E) with E per hop.
    """
    win = int(win_s * fs)
    hop = max(1, int(hop_s * fs))
    T = clean.shape[0]
    times, energies = [], []
    for start in range(0, T - win + 1, hop):
        seg = clean[start:start + win]
        v = seg.var(axis=0)
        energies.append(float(np.sort(v)[-top_k:].mean()))
        times.append((start + win / 2) / fs)
    return np.asarray(times), np.asarray(energies)


def doppler_spectrogram(clean: np.ndarray, fs: float, nperseg: int = 64):
    """Pseudo-Doppler spectrogram: STFT magnitude averaged across subcarriers.

    Amplitude-only sensing folds Doppler sign: tones appear at |f|. See linkbvp.md.
    Returns (freqs, frame_times, mag[F, frames]).
    """
    f, t, Z = stft(clean, fs=fs, nperseg=nperseg,
                   noverlap=nperseg - max(1, nperseg // 8), axis=0)
    mag = np.abs(Z).mean(axis=1)  # average over subcarriers -> (F, frames)
    return f, t, mag


def dominant_doppler(freqs: np.ndarray, mag_col: np.ndarray,
                     fmin: float = 1.5, prominence_ratio: float = 2.0):
    """Strongest tone above fmin in one spectrogram column.

    Returns (f_hz, confidence) or (None, conf) when nothing is prominent enough —
    an honest 'no reading' beats a confident wrong one.
    """
    band = freqs >= fmin
    col = mag_col[band]
    fb = freqs[band]
    if col.size == 0:
        return None, 0.0
    i = int(np.argmax(col))
    conf = float(col[i] / (np.median(col) + 1e-12))
    if conf < prominence_ratio:
        return None, conf
    # parabolic interpolation for sub-bin frequency
    if 0 < i < len(col) - 1:
        a, b, c = col[i - 1], col[i], col[i + 1]
        denom = a - 2 * b + c
        delta = 0.5 * (a - c) / denom if abs(denom) > 1e-12 else 0.0
        delta = float(np.clip(delta, -0.5, 0.5))
    else:
        delta = 0.0
    df = fb[1] - fb[0] if len(fb) > 1 else 0.0
    return float(fb[i] + delta * df), conf


def band_power(clean: np.ndarray, fs: float, band) -> float:
    """Mean PSD power in [band[0], band[1]] Hz across subcarriers (Welch-lite)."""
    T = clean.shape[0]
    if T < 8:
        return 0.0
    spec = np.abs(np.fft.rfft(clean, axis=0)) ** 2
    freqs = np.fft.rfftfreq(T, 1.0 / fs)
    mask = (freqs >= band[0]) & (freqs <= band[1])
    return float(spec[mask].mean()) if mask.any() else 0.0
