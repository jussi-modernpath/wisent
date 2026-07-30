"""Binary CSI frame parser and time-base alignment — host side of the protocol
in docs/architecture.md.

Keep in lockstep with firmware/listener/listener.ino. Change together or not at all.
scripts/validate_protocol.py cross-checks this file against the firmware's own C
source (CRC and frame layout), so a drift on either side fails a test.

Two time bases, in preference order (docs/architecture.md Layer 0, roadmap
"standing risks"):

  seq  — the beacon sequence number, the system clock. Every listener hears the
         same broadcast within microseconds, so seq aligns nodes for free.
  t_us — the listener's local micros(), used only when the firmware could not read
         seq out of the ESP-NOW payload (flags bit2 clear, seq == SEQ_UNAVAILABLE).
         Local clocks drift and are not aligned across nodes; this is a degraded
         mode for single-node bring-up, not a basis for multi-link imaging.
"""

from dataclasses import dataclass

import numpy as np

MAGIC = b"\x1d\xc5"  # 0xC51D little-endian on the wire
# v1: star topology, one implicit transmitter (the beacon), 16-byte header.
# v2: round-robin TDM, frames carry tx_id at offset 16, 17-byte header. Both are parsed —
# v1 support is not legacy politeness, it is what keeps recordings/ readable.
HEADER_LEN = 16              # v1
HEADER_LEN_V2 = 17
VERSION = 1
VERSION_TDM = 2
SUPPORTED_VERSIONS = (VERSION, VERSION_TDM)


def header_len(version: int) -> int:
    return HEADER_LEN_V2 if version == VERSION_TDM else HEADER_LEN


MAX_PAIRS = 192       # == MAX_PAIRS in listener.ino
SEQ_UNAVAILABLE = 0xFFFFFFFF
U32 = 1 << 32

FLAG_FIRST_WORD_INVALID = 0x01
FLAG_HT40 = 0x02
FLAG_SEQ_FROM_PAYLOAD = 0x04

# When first_word_invalid is set, the leading 2 I/Q pairs are hardware garbage.
# Trimming happens in the aligner, never per-frame — see CsiFrame.iq.
FIRST_WORD_PAIRS = 2

# ---- ESP32 CSI buffer layout -------------------------------------------------------
# Two facts, both load-bearing for anything that uses PHASE. Amplitude is immune to both.
#
# 1. Each item is two bytes, IMAGINARY FIRST then REAL (Espressif ESP-IDF Wi-Fi CSI docs:
#    "each item is stored as two bytes: imaginary part followed by real part"). Decoding
#    real-first yields j*conj(H): |H| is unchanged, but every phase is negated, which
#    REVERSES the winding direction the signed-Doppler channel depends on.
# 2. The buffer holds one block per LTF type, in the order LLTF, HT-LTF, STBC-HT-LTF —
#    NOT one wide frequency axis. Each block is 64 items with its own subcarrier map, and
#    the two blocks measure overlapping frequencies twice.
#    Verified on hardware (recordings/tdm4.npz, 17429 frames of 128 pairs): all-zero
#    indices were {0, 27..37} and {64, 93..99}. That is DC+guard for a 52-subcarrier
#    legacy LLTF, then DC+guard for a 56-subcarrier HT20 HT-LTF — different patterns
#    because the two LTFs use different subcarrier counts. Magnitude spectra of the two
#    halves correlate r=+0.96, as two looks at one channel should.
LTF_BLOCK = 64          # items per LTF block on 20 MHz
LTF_NAMES = ("LLTF", "HT-LTF", "STBC-HT-LTF")


def _guard_measured_layout(n_pairs: int):
    """Refuse layouts we have not measured. 192-pair (HT40) buffers are firmware-clipped
    mid-block and their true LTF layout is UNKNOWN — treating them as three 64-item
    blocks would invent frequencies, the exact silent-corruption class this module
    exists to prevent. Measure the HT40 layout before enabling it (roadmap)."""
    if n_pairs % LTF_BLOCK == 0 and n_pairs // LTF_BLOCK > 2:
        raise ValueError(
            f"{n_pairs}-pair CSI: unmeasured HT40/clipped layout — refusing to map "
            f"subcarriers. Measure the real layout first (docs/roadmap.md).")


def merge_ltf_blocks(amp):
    """(T, 128) hardware amplitude -> (T, ~52): PER-BLOCK L1-normalize, then average.

    The 128-pair HT20 buffer is TWO estimates of the same channel (LLTF + HT-LTF,
    overlapping frequencies — see subcarrier_map), and the silicon scales each block
    with an INDEPENDENT per-frame automatic shift. Measured on the 2026-07-30
    empty-room baseline: on marginal-headroom directions the HT-LTF block's scale
    TOGGLES by ~2x on 26-33% of frames (a discrete half-amplitude cluster; the
    LLTF/HT-LTF scale ratio has CoV 0.26-0.29 on affected links vs 0.06 on stable
    ones). A global L1-normalize cannot remove a RELATIVE scale flip between halves
    of the vector, so it surfaced as phantom motion energy — links at 45-61% of
    quiet windows above 2x their floor in a literally empty room, non-reciprocal
    (per-receiver headroom), RSSI-invariant (digital, not RF). This was the
    "hotspots jumping with nobody moving" mechanism, and it polluted every raw-128
    hardware analysis on marginal links before it, including the M2 walk test.

    The fix is one line of algebra: each block estimates the SAME channel, so each
    is L1-normalized on its own used subcarriers BEFORE averaging — any per-block
    scale (stable or toggling) cancels exactly. Verified on the same empty-room
    capture: every affected link's false-trigger rate fell to 0.0%.

    Guard nulls differ per block (LLTF 52 used, HT-LTF 56); columns null in either
    block are dropped so both contributions exist everywhere kept. Non-hardware
    widths (simulator, 64-pair non-HT) pass through unchanged. Output scale is
    ~1/n_used per row — floors are relative quantities, so only absolute-calibrated
    numbers need re-baselining.
    """
    amp = np.asarray(amp, dtype=np.float32)
    if amp.ndim != 2 or amp.shape[1] != 2 * LTF_BLOCK:
        return amp
    a = amp[:, :LTF_BLOCK].copy()
    b = amp[:, LTF_BLOCK:].copy()
    va, vb = (a > 1e-6).all(axis=0), (b > 1e-6).all(axis=0)
    keep = va & vb
    a, b = a[:, keep], b[:, keep]
    a = a / (a.sum(axis=1, keepdims=True) + 1e-12)
    b = b / (b.sum(axis=1, keepdims=True) + 1e-12)
    return (a + b) / 2.0


def ltf_blocks(n_pairs: int):
    """[(start, stop, name)] for each LTF block present in an n_pairs CSI buffer."""
    _guard_measured_layout(n_pairs)
    return [(i * LTF_BLOCK, min((i + 1) * LTF_BLOCK, n_pairs),
             LTF_NAMES[i] if i < len(LTF_NAMES) else f"LTF{i}")
            for i in range((n_pairs + LTF_BLOCK - 1) // LTF_BLOCK)]


def subcarrier_number(k: int) -> int:
    """Signed subcarrier index for position k WITHIN one 64-item LTF block.

    Layout 0=DC, 1..31=+1..+31, 32..63=-32..-1 — the standard FFT wrap, confirmed by the
    measured null map. Consequently *buffer index distance is not frequency distance*:
    indices 20 and 40 are subcarriers +20 and -24, i.e. 44 apart, not 20.
    """
    k = int(k) % LTF_BLOCK
    return k if k <= 31 else k - LTF_BLOCK


def subcarrier_map(n_pairs: int) -> np.ndarray:
    """(n_pairs,) signed subcarrier number per buffer index.

    Real ESP32 buffers are whole 64-item LTF blocks (we have only ever measured 64 and
    128), and those use the wrapped FFT layout above. Any other width is not hardware —
    it is the simulator, whose subcarriers are in LINEAR frequency order — so the map
    falls back to linear. Applying the wrapped layout to linear data manufactures pairs
    that straddle DC: two indices 16 apart in the wrapped map can be ~49 subcarriers apart
    in reality, which silently breaks the delay-gradient assumption the ratio depends on.
    """
    _guard_measured_layout(n_pairs)
    if n_pairs % LTF_BLOCK == 0:
        return np.array([subcarrier_number(k % LTF_BLOCK) for k in range(n_pairs)],
                        dtype=np.int32)
    return np.arange(n_pairs, dtype=np.int32) - n_pairs // 2   # linear (simulator)


def block_of(n_pairs: int) -> np.ndarray:
    """(n_pairs,) LTF block id per buffer index. Pairs must never cross blocks.
    Non-hardware widths (simulator) are a single block."""
    _guard_measured_layout(n_pairs)
    if n_pairs % LTF_BLOCK == 0:
        return np.arange(n_pairs, dtype=np.int32) // LTF_BLOCK
    return np.zeros(n_pairs, dtype=np.int32)


def crc8(data: bytes) -> int:
    """CRC-8, poly 0x07, init 0 — matches crc8() in listener.ino."""
    c = 0
    for b in data:
        c ^= b
        for _ in range(8):
            c = ((c << 1) ^ 0x07) & 0xFF if c & 0x80 else (c << 1) & 0xFF
    return c


# NOTE: there is deliberately no frame *encoder* here. This module can only ever turn
# bytes-from-a-radio into arrays. The test-side encoder lives in
# scripts/validate_protocol.py, where it cannot be imported by anything live — same
# anti-RuView reasoning that keeps sim.py out of this package's live path.


@dataclass
class CsiFrame:
    node_id: int      # RECEIVER. With TDM the link key is (tx_id, node_id).
    seq: int          # SEQ_UNAVAILABLE means "not available; use t_us alignment"
    t_us: int
    rssi: int
    channel: int
    flags: int
    iq: np.ndarray    # complex64, (n_pairs,) — RAW, including any invalid first word
    tx_id: int = 0    # TRANSMITTER; 0 for v1 frames, which had a single implicit beacon

    @property
    def link(self):
        return (self.tx_id, self.node_id)

    @property
    def amplitude(self) -> np.ndarray:
        return np.abs(self.iq)

    @property
    def has_seq(self) -> bool:
        return self.seq != SEQ_UNAVAILABLE

    @property
    def first_word_invalid(self) -> bool:
        return bool(self.flags & FLAG_FIRST_WORD_INVALID)


class FrameParser:
    """Incremental parser: feed() raw bytes, returns a list of CsiFrame.

    Resyncs on garbage by scanning for the magic, which also absorbs the firmware's
    '#'-prefixed ASCII stat lines. Counters are separated on purpose: M1 acceptance
    measures the *CRC error rate*, and ASCII lines that happen to contain the magic
    byte pair would otherwise inflate it. A byte that was never part of an accepted
    frame is counted in resync_bytes; a frame with a valid-looking header whose CRC
    fails is a real corruption and counts in crc_errors.
    """

    def __init__(self):
        self._buf = bytearray()
        self.crc_errors = 0
        self.bad_headers = 0
        self.resync_bytes = 0
        self.frames_ok = 0

    @property
    def crc_error_rate(self) -> float:
        """CRC failures / (frames seen). The M1 acceptance metric (< 0.1%)."""
        seen = self.frames_ok + self.crc_errors
        return self.crc_errors / seen if seen else 0.0

    def _header_plausible(self, n_pairs: int) -> bool:
        return self._buf[2] in SUPPORTED_VERSIONS and 0 < n_pairs <= MAX_PAIRS

    def feed(self, data: bytes):
        self._buf.extend(data)
        out = []
        while True:
            i = self._buf.find(MAGIC)
            if i < 0:
                # No magic: drop everything but a trailing byte (may be a split magic).
                keep = 1 if self._buf else 0
                self.resync_bytes += len(self._buf) - keep
                del self._buf[:len(self._buf) - keep]
                break
            if i > 0:
                self.resync_bytes += i
                del self._buf[:i]
            if len(self._buf) < HEADER_LEN:
                break
            version = self._buf[2]
            n_pairs = self._buf[14]
            if not self._header_plausible(n_pairs):
                # A false magic inside garbage — not a corrupted frame. Rescan past it
                # without charging the CRC budget.
                self.bad_headers += 1
                self.resync_bytes += 2
                del self._buf[:2]
                continue
            hlen = header_len(version)
            total = hlen + 2 * n_pairs + 1
            if len(self._buf) < total:
                break
            frame = bytes(self._buf[:total])
            if crc8(frame[2:total - 1]) != frame[total - 1]:
                self.crc_errors += 1
                self.resync_bytes += 2
                del self._buf[:2]  # skip this magic, rescan
                continue
            raw = np.frombuffer(frame[hlen:hlen + 2 * n_pairs], dtype=np.int8)
            # IMAGINARY first, REAL second — see the layout note above. Getting this
            # backwards conjugates H, which is invisible in amplitude and fatal to sign.
            iq = (raw[1::2].astype(np.float32)
                  + 1j * raw[0::2].astype(np.float32)).astype(np.complex64)
            out.append(CsiFrame(
                node_id=frame[3],
                tx_id=frame[16] if version == VERSION_TDM else 0,
                seq=int.from_bytes(frame[4:8], "little"),
                t_us=int.from_bytes(frame[8:12], "little"),
                rssi=int.from_bytes(frame[12:13], "little", signed=True),
                channel=frame[13],
                flags=frame[15],
                iq=iq,
            ))
            self.frames_ok += 1
            del self._buf[:total]
        return out


@dataclass
class Segment:
    """A gap-free run of one node's CSI on a single time base.

    amp:      (T, S) float32, rows on a uniform grid, short gaps interpolated.
    index:    (T,) int64 — beacon seq when timebase == "seq", else sample number.
    t_us:     (T,) float64 unwrapped listener micros (NaN on interpolated rows).
    timebase: "seq" (cross-node aligned) or "t_us" (single-node, drifting).
    """
    node_id: int          # receiver
    amp: np.ndarray
    index: np.ndarray
    t_us: np.ndarray
    timebase: str
    tx_id: int = 0        # transmitter; (tx_id, node_id) is the LINK identity
    iq: np.ndarray = None # complex CSI on the SAME grid, when keep_complex=True

    @property
    def link(self):
        return (self.tx_id, self.node_id)

    def __len__(self) -> int:
        return self.amp.shape[0]


def unwrap_u32(values) -> np.ndarray:
    """Unwrap a u32 counter that wraps (micros() wraps every ~71.6 minutes).

    Only forward wraps are unwrapped: a *backwards* jump too large to be a wrap is a
    reboot, and the caller must split the segment there rather than paper over it.
    """
    v = np.asarray(values, dtype=np.int64)
    if v.size == 0:
        return v.astype(np.float64)
    d = np.diff(v)
    return (v + U32 * np.concatenate([[0], np.cumsum(d < -(U32 // 2))])).astype(np.float64)


def _interp_nan_rows(amp: np.ndarray) -> None:
    """In-place linear interpolation of all-NaN rows. Only ever called on segments
    whose internal gaps are already known to be <= max_gap."""
    bad = np.isnan(amp).all(axis=1)
    if not bad.any() or bad.all():
        return
    idx = np.arange(amp.shape[0])
    for s in range(amp.shape[1]):
        col = amp[:, s]
        col[bad] = np.interp(idx[bad], idx[~bad], col[~bad])


def _trim_pairs(frames, n_sc):
    """Consistent subcarrier indexing across a segment.

    If ANY frame in the segment has first_word_invalid, the leading FIRST_WORD_PAIRS
    are dropped from EVERY frame. Trimming per-frame instead would shift subcarrier k
    by 2 on some rows and not others — silently scrambling the frequency axis that all
    of Layer 2 depends on.
    """
    drop = FIRST_WORD_PAIRS if any(f.first_word_invalid for f in frames) else 0
    n_sc = n_sc or min(len(f.iq) for f in frames) - drop
    if n_sc <= 0:
        return None, 0, 0
    return drop, n_sc, drop + n_sc


def frames_to_segments(frames, n_sc=None, max_gap: int = 3, min_len: int = 1,
                       fs: float = 100.0, keep_complex: bool = False):
    """Split one node's frames into gap-free Segments on the best available time base.

    Rules (docs/architecture.md Layer 1): missing rows become NaN and are interpolated
    only for gaps <= max_gap; a longer gap splits the segment. Non-monotonic time
    (beacon or listener reboot) always splits — never interpolate across a reboot, and
    never allocate a grid from a backwards jump.

    Frames are additionally grouped by CSI length, because the ESP32 emits a different
    number of I/Q pairs depending on the PHY format of the packet it heard (measured on
    an ESP32-S3 listening to ambient traffic: 64-pair and 128-pair frames interleaved in
    one stream). Those layouts do NOT share a subcarrier axis — stacking them, or
    truncating the long ones, would make subcarrier k mean different frequencies on
    different rows. A deployment locked to the beacon's fixed rate should see exactly one
    length; more than one is a signal worth seeing, not smoothing over.

    Frames must come from a single node; mixing node_ids raises.
    """
    frames = [f for f in frames if len(f.iq) > 0]
    if not frames:
        return []
    node_ids = {f.node_id for f in frames}
    if len(node_ids) > 1:
        raise ValueError(f"frames_to_segments expects one node, got {sorted(node_ids)}")

    # Under TDM one receiver hears several transmitters, and (tx_id, node_id) is a
    # DIFFERENT PHYSICAL LINK with its own geometry — never one time series.
    tx_ids = {f.tx_id for f in frames}
    widths = {len(f.iq) for f in frames}
    if len(tx_ids) > 1 or len(widths) > 1:
        out = []
        for t in sorted(tx_ids):
            for w in sorted(widths):
                grp = [f for f in frames if f.tx_id == t and len(f.iq) == w]
                if grp:
                    out += _segments_one_format(grp, n_sc, max_gap, min_len, fs,
                                                keep_complex)
        return sorted(out, key=lambda s: (s.tx_id, s.amp.shape[1], int(s.index[0])))
    return _segments_one_format(frames, n_sc, max_gap, min_len, fs, keep_complex)


def _segments_one_format(frames, n_sc, max_gap, min_len, fs, keep_complex=False):
    """Segment frames that all carry the same number of I/Q pairs."""
    node_id = frames[0].node_id

    with_seq = [f for f in frames if f.has_seq]
    if with_seq:
        # Drop isolated corrupted-seq frames first, in ARRIVAL order: a frame whose seq
        # jumps far from both arrival-neighbors while those neighbors are consecutive is
        # an over-the-air corruption that survived the UART CRC (the listener computes
        # CRC8 over already-corrupted RAM; measured 2026-07-29: ~0.01% of frames at weak
        # RSSI). Firmware now rejects these via rx_ctrl.rx_state; this is the host-side
        # belt to that suspender, and it protects old recordings on replay.
        if len(with_seq) >= 3:
            k = np.array([f.seq for f in with_seq], dtype=np.int64)
            prev_far = np.abs(k[1:-1] - k[:-2]) > 1000
            next_far = np.abs(k[2:] - k[1:-1]) > 1000
            bridge_ok = np.abs(k[2:] - k[:-2]) <= 2 * max_gap + 2
            drop_mid = prev_far & next_far & bridge_ok
            keep = np.ones(len(with_seq), dtype=bool)
            keep[1:-1] = ~drop_mid
            with_seq = [f for f, kp in zip(with_seq, keep) if kp]
        # Beacon time base. Drop the seq-less minority: on the seq grid they have no
        # place, and guessing one would fabricate alignment.
        frames, timebase = sorted(with_seq, key=lambda f: f.seq), "seq"
        keys = np.array([f.seq for f in frames], dtype=np.int64)
    else:
        # Degraded single-node base. Unwrap in ARRIVAL order (that is the only order in
        # which a wrap is distinguishable from a reboot), then sort by the result.
        timebase = "t_us"
        t = unwrap_u32([f.t_us for f in frames])
        order = np.argsort(t, kind="stable")
        frames = [frames[i] for i in order]
        t = t[order]
        keys = np.rint((t - t[0]) * 1e-6 * fs).astype(np.int64)

    drop, n_sc, end = _trim_pairs(frames, n_sc)
    if drop is None:
        return []
    # A short frame (truncated CSI buffer) cannot fill the subcarrier axis; drop it
    # rather than pad, so no invented subcarrier ever reaches Layer 1.
    long_enough = np.array([len(f.iq) >= end for f in frames])
    if not long_enough.all():
        frames = [f for f, ok in zip(frames, long_enough) if ok]
        keys = keys[long_enough]
        if not frames:
            return []

    # Deduplicate identical time keys (a retransmitted or double-reported packet):
    # keep the first, since the later copy carries no new channel information.
    keep = np.concatenate([[True], np.diff(keys) != 0])
    frames = [f for f, k in zip(frames, keep) if k]
    keys = keys[keep]

    # Cut where the step is too large to interpolate, or goes backwards (reboot).
    step = np.diff(keys)
    cuts = np.where((step > max_gap) | (step <= 0))[0] + 1
    segments = []
    for a, b in zip(np.concatenate([[0], cuts]), np.concatenate([cuts, [len(frames)]])):
        grp, k = frames[a:b], keys[a:b]
        span = int(k[-1] - k[0]) + 1
        if span < min_len:
            continue
        amp = np.full((span, n_sc), np.nan, dtype=np.float32)
        t_us = np.full(span, np.nan, dtype=np.float64)
        rows = (k - k[0]).astype(np.int64)
        stack = np.stack([f.iq[drop:end] for f in grp])
        amp[rows] = np.abs(stack)
        t_us[rows] = unwrap_u32([f.t_us for f in grp])
        _interp_nan_rows(amp)
        iq = None
        if keep_complex:
            # The signed-Doppler channel needs PHASE, so it needs the same gap-gridded,
            # deduped, reboot-split time base the amplitude path gets. Without this,
            # callers re-implement gridding (walk_test.py did) and a lost packet silently
            # compresses time, shifting every inferred Doppler frequency.
            iq = np.full((span, n_sc), np.nan, dtype=np.complex64)
            iq[rows] = stack
            bad = np.isnan(iq[:, 0])
            if bad.any() and not bad.all():
                idx = np.arange(span)
                for c in range(n_sc):     # interpolate real/imag separately
                    iq[bad, c] = (np.interp(idx[bad], idx[~bad], iq[~bad, c].real)
                                  + 1j * np.interp(idx[bad], idx[~bad], iq[~bad, c].imag))
        segments.append(Segment(node_id=node_id, amp=amp,
                                index=np.arange(k[0], k[0] + span, dtype=np.int64),
                                t_us=t_us, timebase=timebase,
                                tx_id=frames[0].tx_id, iq=iq))
    return segments


def frames_to_matrix(frames, n_sc=None, max_gap: int = 3):
    """Convenience wrapper: the longest Segment as (amp[T, S], index[T]).

    Prefer frames_to_segments — dropping the shorter segments silently discards data.
    """
    segs = frames_to_segments(frames, n_sc=n_sc, max_gap=max_gap)
    if not segs:
        return np.zeros((0, 0), dtype=np.float32), np.zeros(0, dtype=np.int64)
    best = max(segs, key=len)
    return best.amp, best.index


def align_segments(segments, min_overlap: int = 1):
    """Intersect per-node Segments onto one common seq grid (M1 acceptance:
    "frames align on seq").

    segments: one Segment per node, all with timebase == "seq". Returns
    (index[T], {(tx_id, node_id): amp[T, S]}) over the largest common seq range, or
    (empty, {}) if the overlap is shorter than min_overlap. t_us segments are
    rejected: local clocks are not aligned across nodes, so an "alignment" built
    from them would be fiction.
    """
    segments = list(segments)
    if not segments:
        return np.zeros(0, dtype=np.int64), {}
    bad = [s.node_id for s in segments if s.timebase != "seq"]
    if bad:
        raise ValueError(f"cannot align nodes on local clocks; nodes {bad} lack beacon seq")
    lo = max(int(s.index[0]) for s in segments)
    hi = min(int(s.index[-1]) for s in segments)
    if hi - lo + 1 < min_overlap:
        return np.zeros(0, dtype=np.int64), {}
    index = np.arange(lo, hi + 1, dtype=np.int64)
    return index, {s.link: s.amp[lo - int(s.index[0]): hi - int(s.index[0]) + 1]
                   for s in segments}
