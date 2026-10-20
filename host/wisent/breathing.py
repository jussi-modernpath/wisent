"""Layer 3: respiration engine. Multi-link Welch fusion with a prominence gate
(estimate_bpm), and a descriptive per-link line report (estimate_rates).

Physics constraints inherited from the Fresnel model (docs/theory.md #3.1): single
subject, roughly still, within a few meters, orientation matters, per-link blind
spots exist — which is exactly why we fuse subcarriers AND links.
"""

import numpy as np
from scipy.signal import welch

RESP_BAND = (0.1, 0.6)  # 6..36 breaths/min


def _link_psd(clean, fs, band, top_k):
    """One link's unit-sum PSD: Welch (40 s segments, shorter if the link is), averaged over
    the top_k subcarriers by in-band power. Returns (f, psd, in-band bin indices), with psd
    None when no Welch bin falls in the band."""
    T = clean.shape[0]
    nperseg = min(T, int(fs * 40))
    f, P = welch(clean, fs=fs, nperseg=nperseg, axis=0)
    mask = (f >= band[0]) & (f <= band[1])
    if not mask.any():
        return f, None, None
    # top-k subcarriers by in-band power: dodge per-subcarrier Fresnel blind spots
    in_band_power = P[mask].sum(axis=0)
    top = np.argsort(in_band_power)[-top_k:]
    psd = P[:, top].mean(axis=1)
    return f, psd / (psd.sum() + 1e-18), np.where(mask)[0]


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
        f, psd, _idx = _link_psd(clean, fs, band, top_k)
        if psd is None:
            continue
        # this link's own peak + prominence (weight, and per-link report)
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


def estimate_rates(clean_links, fs, band=RESP_BAND, top_k=5, prominence_ratio=3.0,
                   min_links=2):
    """Every respiratory-band spectral LINE the links support — lines, not sources or people.

    clean_links: list of sanitized amp arrays (T, S), one per link, from a STILL window.
    There is no motion or drift gate (docs/architecture.md): the caller must pass a still
    window. A link shorter than 60 s is not used — with a single 40 s Welch segment, chance
    noise peaks on two links made false lines (2/20 empty-room seeds at 40 s, 0/20 at 60 s).

    Per link, on the same unit-sum top-k PSD and band mask as estimate_bpm, every in-band
    local maximum whose peak/in-band-median ratio is at least prominence_ratio is a peak.
    Strongest first, a peak within one Welch bin of 2x or 3x a stronger kept peak on the same
    link is that peak's overtone: listed, not clustered. The other peaks are clustered across
    links by Welch-bin adjacency (chains merge) at the (prominence - 1)-weighted mean rate.
    In ascending rate, a cluster within one bin of 2x or 3x the lowest lower cluster that has
    at least its support and shares at least half its links is a harmonic; any other cluster
    is a line when seen on at least min_links links, and single-link otherwise.

    Returns a dict:
      lines, harmonics, single_link -- ascending by rate, each {rate_bpm, links, support}
          (links are input positions); harmonics add of_bpm and order.
      rates_bpm -- the line rates.
      n_links_used -- links of 60 s or longer.
      disagreement -- True when 2+ non-harmonic clusters (lines plus single-link) are
          present, False when fewer, None when no link was usable (abstain).
      link_peaks -- per input position: None for an unused link, else a list of
          (bpm, prominence, overtone_of_bpm or None), strongest first.
    It carries no source or people count.

    Unlike estimate_bpm: link_peaks holds 3-tuples and keeps input positions (estimate_bpm's
    2-tuples skip links with no in-band bins), and prominence_ratio gates each link's peaks,
    not the fused spectrum.

    Known limitations (Epic 282; verified on synthetic and constructed data only):
    - slow static-channel or posture drift makes lines near the 8 s detrend shoulder
      (~6.5-10 bpm in simulation; docs/handoff.md), which can relabel a real line at 2x or
      3x as its harmonic, or hide it on links where the artifact is the stronger peak;
    - a harmonic better supported than its fundamental is a line; intermodulation lines and
      chance noise peaks on two links are lines too;
    - rates in adjacent bins (up to ~3 bpm apart) merge; a real line at 2x or 3x a stronger
      peak or a co-occurring, at-least-as-supported lower cluster is labelled a harmonic;
    - the 0.6 Hz bin falls outside the inherited band mask by float rounding, so the
      effective upper edge is 34.5 bpm.
    """
    if not prominence_ratio > 1:  # cluster weights are prominence - 1
        raise ValueError(f"prominence_ratio must be > 1, got {prominence_ratio}")
    dbpm = 60.0 * fs / int(fs * 40)  # Welch bin width, breaths/min
    link_peaks = []
    flat = []  # non-overtone peaks: (bin, bpm, prominence, input position)
    for pos, clean in enumerate(clean_links):
        if clean.shape[0] < int(fs * 60):
            link_peaks.append(None)
            continue
        f, psd, idx = _link_psd(clean, fs, band, top_k)
        entries, kept = [], []
        for j, prom in ([] if psd is None else _psd_peaks(psd, idx, prominence_ratio)):
            overtone_of = None
            for j0 in kept:
                n = _round_half_up(j / j0)
                if n in (2, 3) and abs(j - n * j0) <= 1:
                    overtone_of = float(f[j0] * 60.0)
                    break
            entries.append((float(f[j] * 60.0), prom, overtone_of))
            if overtone_of is None:
                kept.append(j)
                flat.append((j, float(f[j] * 60.0), prom, pos))
        link_peaks.append(entries)

    clusters = []
    for j, bpm, prom, pos in sorted(flat):
        if clusters and j - clusters[-1]["bins"][-1] <= 1:
            clusters[-1]["bins"].append(j)
            clusters[-1]["members"].append((bpm, prom, pos))
        else:
            clusters.append({"bins": [j], "members": [(bpm, prom, pos)]})
    for c in clusters:
        w = np.array([prom - 1.0 for _, prom, _ in c["members"]])
        c["rate"] = float(np.dot(w, [bpm for bpm, _, _ in c["members"]]) / w.sum())
        c["links"] = sorted({pos for _, _, pos in c["members"]})
        c["k"] = _round_half_up(c["rate"] / dbpm)

    out = {"lines": [], "harmonics": [], "single_link": []}
    for q, c in enumerate(clusters):  # ascending rate
        rec = {"rate_bpm": c["rate"], "links": c["links"], "support": len(c["links"])}
        for f0 in clusters[:q]:  # lowest lower cluster first
            if len(f0["links"]) < rec["support"]:
                continue
            n = _round_half_up(c["k"] / f0["k"])
            shared = len(set(c["links"]) & set(f0["links"]))
            if n in (2, 3) and abs(c["k"] - n * f0["k"]) <= 1 and 2 * shared >= rec["support"]:
                rec.update(of_bpm=f0["rate"], order=n)
                break
        if "order" in rec:
            out["harmonics"].append(rec)
        elif rec["support"] >= min_links:
            out["lines"].append(rec)
        else:
            out["single_link"].append(rec)

    used = sum(lp is not None for lp in link_peaks)
    out["rates_bpm"] = [c["rate_bpm"] for c in out["lines"]]
    out["n_links_used"] = used
    out["disagreement"] = (None if used == 0
                           else len(out["lines"]) + len(out["single_link"]) >= 2)
    out["link_peaks"] = link_peaks
    return out


def _psd_peaks(psd, idx, prominence_ratio):
    """In-band local maxima of psd whose ratio to the in-band median is at least
    prominence_ratio, as [(bin, prominence)], strongest first. Neighbours come from the
    full PSD, so an in-band edge bin can qualify; a plateau yields its lower bin."""
    med = np.median(psd[idx]) + 1e-18
    peaks = [(int(j), float(psd[j] / med)) for j in idx
             if 0 < j < len(psd) - 1 and psd[j] > psd[j - 1] and psd[j] >= psd[j + 1]
             and psd[j] / med >= prominence_ratio]
    return sorted(peaks, key=lambda pk: -pk[1])


def _round_half_up(x):
    return int(np.floor(x + 0.5))
