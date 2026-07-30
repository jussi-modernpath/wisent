# Architecture

Five layers. Lower layers are boring on purpose; each exists to neutralize one specific
hardware defect documented in `theory.md` §2.

## Layer 0 — Radio discipline (firmware)

**Beacon node** (1×): ESP-NOW broadcast, 100 Hz, fixed channel (default 6), HT20 initially
(HT40 after M1 verifies stability), TX power locked (`esp_wifi_set_max_tx_power(78)` = 19.5 dBm),
rate locked to MCS0-LGI. Payload: `{magic, seq:u32, tx_node_id:u8}`. The beacon's sequence
number is the **system clock** — all listeners align on it; no NTP, no RTC.

> **Subcarrier counting.** "52 vs 108 subcarriers" style numbers conflate two things.
> The 128-pair HT20 buffer is ~52 (LLTF) + ~56 (HT-LTF) *estimates of overlapping
> frequencies* — the diversity budget for anything keyed to frequency span (the ratio
> channel's delay-gradient term, Fresnel diversity) is **~52 distinct subcarriers**, and
> the second LTF block is a second *estimate*, worth averaging/SNR, not 2× diversity.

### Protocol v3 — wireless backhaul (2026-07-29, hardware-verified)

Only ONE node needs a cable. Remote nodes format their CSI into the **same USB frames**
they would have written to a cable, and ship them to the gateway over ESP-NOW unicast; the
gateway writes those bytes to USB **verbatim**. Because the wire format is unchanged and
the link key `(tx_id, node_id)` already names the measuring node, `csi_io` required **no
modification** and a relayed frame is byte-identical to a direct one.

```
round (20 ms @ 50 Hz):
  |<-- sensing phase 5.5 ms -->|<---- backhaul phase 14.5 ms ---->|
   n0  n1  n2  n3  n4            node1 | node2 | node3 | node4
   MCS0-LGI, 8-byte beacons      MCS5, <=1400 B of whole frames, unicast+ACK
```

Load-bearing details:
- **Rate config is per peer.** Sensing beacons stay locked at MCS0-LGI (the calibration
  invariant CSI depends on) while backhaul runs at MCS5. MCS5 not MCS7: the weakest link
  arrives at −62 dBm, ~8 dB over MCS5's sensitivity but only ~2 dB over MCS7's.
- **The sensing path rejects backhaul by LENGTH** (`payload_len > 200`). This is not
  optional: a backhaul packet *contains* relayed frames that begin with the very
  `{0x1D,0xC5,ver,tx}` header the CSI callback scans for, so without the gate it would
  inject phantom CSI **and corrupt the round clock**. Measured: exactly one rejection per
  round, i.e. the gate firing precisely on the one backhaul packet.
- **Whole frames only** per packet, so a lost packet costs whole frames instead of
  desynchronising the host's byte stream mid-frame. Per-source packet sequence numbers
  give honest loss accounting (`# bh gateway relayed= lost= overflow=`).
- **The gateway MAC is learned, never configured** — it is read from the gateway's own
  sensing beacon, which is already an ESP-NOW broadcast.
- **Phases exist because a radio cannot receive while transmitting.** Unslotted 1470-byte
  backhaul packets would overlap someone's beacon ~11% of the time — an 11% CSI loss.

Host link budget (measured on this hardware, zero corrupt bytes in multi-MB checks):
921600 → 96.8 kB/s (insufficient), 3 Mbaud → 318 kB/s, **4 Mbaud → 423 kB/s**. A node
costs ~55 kB/s, so 4 Mbaud carries the gateway plus 4 remotes (~275 kB/s) with headroom;
`live_capture.py` probes the baud because the fleet can be mixed.

**Verified 2026-07-29, FULL 5-NODE MESH on a single cable:** **20/20 directed links =
10/10 unique pairs**, median **20 links per round**, 90% of rounds complete, **0 CRC
errors**, 327 kB/s against the measured 423 kB/s ceiling. Relayed/direct round-clock
offset **exactly 0** — relayed frames share the time base, which is what makes cross-link
alignment work at all.

**The backhaul rate lesson (measured, cost 90% of a link's data).** Backhaul first ran at
MCS5. The −77 dBm node then relayed **5.6 frames/s while its own 8-byte MCS0 beacons
arrived at 58.8/s from the same radio** — a 10x deficit that looks like a dying node and is
purely modulation: a 1400-byte MCS5 packet needs ~−70 dBm, an 8-byte MCS0 one survives far
below. Backhaul now defaults to **MCS0-LGI, the same modulation as the beacons**, because
capacity permits robustness: one node's round of CSI is ~1.35 ms at 6.5 Mbps, so four
remotes use 5.4 ms of the 14.5 ms backhaul phase (2.7x headroom). *Sensing beacons were
never affected — rate config is per peer.*

That failure also exposed a genuine bug: frames were dequeued from the CSI ring **before**
`esp_now_send` was known to have succeeded, so an `ESP_ERR_ESPNOW_NO_MEM` (exactly what a
weak link's retry backlog produces) destroyed them with no counter attached. The ring is
now consumed only on a successful queue, and a send callback distinguishes **queued** from
**ACKed** (`acked=`/`noack=`) — queuing is not delivery, and the gap between them is what a
marginal link looks like.

**Listener nodes** (2–4×): CSI callback (`esp_wifi_set_csi_rx_cb`) filtered to beacon MAC.
Config: `lltf_en=1, htltf_en=1, stbc_htltf2_en=0, ltf_merge_en=0, channel_filter_en=0,
manu_scale=0`. Callback copies into a ring buffer; the main loop drains it to UART as binary
frames. Never do work in the callback (it runs in the WiFi task).

**Round-robin TDM** (`firmware/node/`, protocol v2 — built and working 2026-07-29):
every node transmits *and* receives, giving all K(K−1)/2 links instead of the star's K−1.
**Node 0 is the master**: it owns the round counter, which *is* the system clock. Slaves
hear the master's frame and transmit at their slot offset, stamping the **same round
number** — so every transmission in a round shares one index and the host aligns all links
on it with no cross-node clock discipline. Links are keyed `(tx_id, node_id)`.

The slot is a **politeness offset, not a deadline.** `loop()` shares the CPU with a WiFi
task fielding ~500 discarded ambient CSI callbacks/s and cannot be relied on to poll inside
a 5 ms window — enforcing one cost 72% of transmissions on hardware. Nodes transmit once
the offset passes and let ESP-NOW's CSMA handle overlap; `late=` counts slipped slots.

**Rate budget.** Each link is sampled at the *full* round rate, but every receiver streams
(K−1) frames per round, so UART — not airtime — binds. Against the measured 72.1 kB/s
ceiling with 128-pair frames: 50 Hz → 41 kB/s (fits), 75 Hz → 62 kB/s, 100 Hz → 82 kB/s
(over). `ROUND_HZ` defaults to **50**, i.e. Nyquist 25 Hz = |v·g| up to 3.08 m/s, past the
~16 Hz a person walking head-on produces. Raise it only alongside a smaller CSI width.

The older star firmware (`firmware/beacon` + `firmware/listener`, protocol v1) remains for
single-transmitter work; the host parses both.

## Layer 1 — Sanitization (host, per node)

Order matters: **Hampel(k=7, t=3σ) → L1 normalize per frame → resample onto seq grid →
per-subcarrier moving-mean detrend (win=2 s)**. Output: `amp[T, S]` float32, and — when
`keep_complex=True` — `iq[T, S]` complex64 on the same grid, which is what the
signed-Doppler channel consumes (`wisent/ratios.py`). *Note:* an earlier draft specified a
`conj[T, S-1]` cross-subcarrier conjugate-product output; that was never built, and the
ratio (not the conjugate product) is the primitive actually used — see
`docs/signed-doppler.md` for why division avoids the mirror that conjugate multiplication
creates.
Missing seq numbers become NaN rows and are interpolated only for gaps ≤ 3; longer gaps
split the segment.

**Time base and segmenting** (`csi_io.frames_to_segments`, `csi_io.align_segments`):

- Preferred base is the beacon `seq`. Frames whose firmware could not read seq out of the
  ESP-NOW payload (`flags` bit2 clear, `seq == 0xFFFFFFFF`) fall back to the listener's
  local `t_us`, resampled onto a 100 Hz grid. That fallback is **single-node only** —
  local clocks are neither aligned nor disciplined, so `align_segments` refuses to build
  a cross-node grid from them rather than inventing a shared clock.
- A segment is cut wherever the step exceeds 3 or goes backwards. Backwards means a
  beacon or listener reboot; interpolating across one would fabricate motion, and naively
  gridding it would try to allocate the whole u32 range.
- `t_us` is unwrapped across the `micros()` wrap (~71.6 min).
- If *any* frame in a segment has `first_word_invalid`, the leading 2 I/Q pairs are dropped
  from *every* frame in it. Trimming per-frame would shift subcarrier *k* by two on some
  rows only — silently scrambling the frequency axis all of Layer 2 rests on. (IDF v5
  documents this flag as "first four bytes … invalid", i.e. exactly 2 I/Q pairs.)
- Frames are grouped by **CSI length** before segmenting. The number of I/Q pairs follows
  the PHY format of the packet the listener overheard — measured on an ESP32-S3 sniffing
  ambient traffic: 64-pair and 128-pair frames interleaved in one stream, with different
  amplitude scales (mean 10.7 vs 27.6) and different subcarrier layouts. A deployment
  locked to the beacon's fixed MCS0 rate should see exactly one length; the code does not
  assume it, because stacking or truncating those layouts silently corrupts the frequency
  axis in a way no downstream stage can detect.

> **As-built vs planned.** Layers 0–1 and the Layer-2 *functions* are implemented and
> exercised on hardware. The following are **described here but NOT wired**: there is no
> `config/room.yaml` loader, no persisted calibration artifact, no motion-gated breathing
> path, and no unified feature-bus object — `live_capture.py` records, and analysis is
> currently assembled per-script. Treat the fusion rules below as the specification they
> are, not as running code, until a single recording-to-analysis path exists.

## Layer 2 — Link-feature bus

Per link, per 100 ms hop (1 s window): motion energy `E` (mean over top-k=5 variance
subcarriers of windowed variance), band powers `P_resp` (0.1–0.6 Hz), `P_move` (2–50 Hz),
pseudo-Doppler spectrogram column `D[f]` (STFT of detrended amp, averaged |·| across
subcarriers), mean RSSI. This bus is the only interface Layers 3–4 may consume.

## Layer 3 — Physics engines

- **vrti.py**: image = `P @ y` where `y` = link motion energies and
  `P = (WᵀW + α(DxᵀDx + DyᵀDy))⁻¹Wᵀ` is precomputed. W uses ellipse weights
  (excess-path threshold `ellipse_excess`, default 0.4 m; weight 1/√d_link).
- **breathing.py**: gated on `E < still_threshold`; subcarrier top-5 selection in resp band,
  multi-link Welch periodogram fusion (sum of normalized PSDs), peak → bpm with a
  peak-to-median prominence gate (reject if < 3×: "no confident reading" beats a wrong one).
- Fusion rules **(specified, not yet wired)**: breathing only reported when motion says
  still; map only trusted after a 30 s empty-room calibration stored per deployment.
  `estimate_bpm()` is currently callable without any stillness check, and doing so on real
  data produced a confident filter artifact (docs/signed-doppler.md §Limitations) — the
  gate must become code, not prose.

## Layer 4 — Learned heads (M4+, optional)

Small CNN over LinkBVP features only (never raw CSI — that's how room-overfitting happens).
Trained with webcam-teacher labels, evaluated only cross-room. See `linkbvp.md`.

## Binary serial protocol (firmware ⇄ csi_io.py — change together only)

Little-endian, framed, checksummed. One frame per received beacon packet:

```
offset  size  field
0       2     magic = 0xC51D
2       1     version = 1
3       1     node_id
4       4     seq          (u32; beacon seq if payload readable, else 0xFFFFFFFF)
8       4     t_us         (u32; listener local micros() truncated — debugging only)
12      1     rssi         (i8)
13      1     channel      (u8)
14      1     n_pairs      (u8; count of I/Q int8 pairs that follow)
15      1     flags        (bit0: first_word_invalid, bit1: HT40, bit2: seq_from_payload)
16      2*n   payload      (int8 I, int8 Q) × n_pairs
16+2n   1     crc8         (poly 0x07 over bytes 2..16+2n-1)
```

**Version 2 (TDM)** inserts one byte and shifts the payload; the parser accepts both, which
is what keeps `recordings/` readable:

```
offset  size  field
0..15         as v1, but version = 2 and node_id is the RECEIVER
16      1     tx_id        (u8; which node transmitted — link key is (tx_id, node_id))
17      2*n   payload      (int8 I, int8 Q) × n_pairs
17+2n   1     crc8         (poly 0x07 over bytes 2..17+2n-1)
```

Computed in `scripts/validate_protocol.py` (8N1 ⇒ 10 bits/byte), per listener at 100 Hz:

| mode | pairs | bytes/frame | one 921600 port | 4 listeners sharing one port |
|---|---|---|---|---|
| HT20 | 52 | 121 | 13% | 53% — fits, with batching |
| HT40 | 108 | 233 | 25% | 101% — **does not fit** |
| worst case | 192 | 401 | 44% | — |

So: one port per listener is comfortable in either mode, and the "all four on one port"
fallback is available at HT20 only (batch 10 frames per USB write). Going HT40 on a shared
port needs a higher baud — this is the roadmap's standing UART risk, quantified.

**Measured 2026-07-29** on an ESP32-S3 at 921600, writing flat out: **72.1 kB/s**
(737.9 kbit/s, ~80% of line rate — normal 8N1 efficiency). Requesting 115200 gave
9.0 kB/s, confirming the baud request is honoured. One HT20 listener needs 12.1 kB/s, so
there is ~6× headroom and the batching fallback in `listener.ino` is not required on this
hardware. The standing UART risk is retired for S3-class boards at HT20 and HT40.

## Calibration procedure (per deployment)

1. Node geometry: measure/enter node XY positions to ±10 cm (`config/room.yaml`).
2. Empty-room baseline: 30 s, stores per-link `E` floor and per-subcarrier amp means.
3. Walk test: one person, prescribed path, 60 s — sanity-checks VRTI sign and scale.
4. (Breathing) subject sits at marked spot 2 m from a link, facing it; 60 s against
   manual count or chest strap. Store the geometry that worked.
