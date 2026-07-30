# wisent — state handoff for planning (as of 2026-07-29, end of session)

Written for an agent that has not seen this session. Everything below is either **measured
on hardware** (numbers + where the recording lives) or explicitly marked **not proven**.
The project's governing rule is that a claim without a recording behind it does not get
made — please preserve that when planning.

---

## 1. What the system physically is, right now

Five bare ESP32-S3 boards (single antenna each) in a ~10 × 5 m room, plus a Python host on
a Mac. Channel 6, 2.4 GHz.

| node | role | connection | position status |
|---|---|---|---|
| 0 | **gateway** + TDM master (owns the round clock) | **USB cable, 4 Mbaud** | desk corner, trusted |
| 1 | remote | power only | **coordinates STALE** |
| 2 | remote | power only | **coordinates STALE** (RSSI swung 15 dB tonight) |
| 3 | remote | power only | **coordinates STALE** |
| 4 | remote | power only | untouched, trusted |

**Only node 0 has a data cable.** This changed tonight (protocol v3, §3) and is the single
biggest structural improvement — previously each node needed its own USB cable, which
capped the usable mesh at whatever the operator could physically cable.

### Measured performance of the current mesh (single cable)

```
20/20 directed links = 10/10 unique node pairs
median 20 links per round, 90% of rounds complete, 98% carry >=16
per-link rate 58.8-60.3 frames/s (round rate 50 Hz nominal)
327 kB/s over USB, against a measured 423 kB/s ceiling
0 CRC errors, 0 backhaul overflow, backhaul loss 1 packet in ~4000
relayed-vs-direct round-clock offset: exactly 0
```

**Why the ≥10 links matters:** the VRTI localization engine previously ran on 7 links and
failed at chance. An observability diagnostic (§4) established the cause is **link
starvation, not a broken front end**. 10 links is the threshold the literature associates
with zone-level accuracy. Testing whether 10 links actually fixes localization is the
highest-value experiment available and is now unblocked.

---

## 2. The three deliverables and their honest status

| # | deliverable | status |
|---|---|---|
| 1 | **VRTI disturbance map** (through-wall motion imaging) | **failed at 7 links**; retest at 10 pending |
| 2 | **Breathing monitor** | plausible signal, **zero ground truth**; one artifact already caught |
| 3 | **LinkBVP** (novel research: room-independent motion features) | synthetic-only; its signed-Doppler extension **fails its own hardware cross-check** |

### VRTI detail
- Station test (`recordings/station1.npz`, script-cued known coordinates, exact timings):
  errors **0.51 / 4.79 / 4.43 / 3.25 m**, quadrant **1/4** against a pre-registered ≥80% bar.
- The one success is informative: station 1 had **a single dominant responder** (one link at
  10.2× its floor) → 0.51 m, the best localization this project has produced. Stations 2–4
  produced diffuse 2–8× responses across many links → all three collapsed toward the desk
  half. Working rule: **one loud link localizes; five murmuring links mislead.**
- Estimator gates (abstention, floors, margins) were measured **not** to rescue this: the
  failure is "stable but stably wrong" / prior-dominated, not "no evidence". Do not plan
  more gate work at low link counts.

### Breathing detail
- Fusion is now prominence-weighted and reports **per-link peaks** rather than one averaged
  number, because averaging previously hid a 28.8-vs-34.0 bpm inter-link disagreement.
- Last blind recording: three links covering the sofa agreed at **10.5–12.0 bpm** (two
  people seated there), four other links scattered. Suggestive, **not a claim** — no counts,
  motion in-window, and two dogs live in the room and breathe in the same band.
- An earlier 8.5 bpm reading was **proven** to be a filter-shoulder artifact (the rate
  tracked the detrend cutoff: 4 s → 19.9, 8 s → 8.3, 16 s → 6.8 bpm). Any breathing number
  must survive a cutoff sweep before being believed.

### LinkBVP / signed-Doppler detail
- The novel idea: cross-subcarrier **ratios** cancel the per-packet random phase, and their
  winding direction gives **signed** per-link Doppler — which would remove the ±v ambiguity
  that single-antenna amplitude sensing normally suffers. Spec: `docs/signed-doppler.md`,
  prior art: `docs/prior-art-signed-doppler.md`.
- Synthetic: sign 10/10, heading unfolded to ~2°, hardware-layout variant passing.
- **Hardware: FAILS its own ground-truth-free consistency check** — |f_signed| should match
  the amplitude-path |f_folded|; synthetic median error ~1%, hardware walking **~51%**.
  A runtime veto (`ratios.corroborated_signed_doppler`) now suppresses uncorroborated
  readings; live, it emitted 2 and vetoed 25 of 27 confident windows.
- **Do not plan anything that assumes the sign works on real humans.** The next step is
  diagnostic, not confirmatory (§5.2).

---

## 3. Protocol v3 — wireless backhaul (built and verified tonight)

Full design: `docs/architecture.md` §"Protocol v3". Summary for planning:

Remote nodes format CSI into **the same USB frames they would have written to a cable** and
ship them to the gateway over ESP-NOW unicast; the gateway writes those bytes to USB
**verbatim**. Consequences worth knowing:

- **`csi_io.py` required zero changes.** A relayed frame is byte-identical to a direct one,
  and the link key `(tx_id, node_id)` already names the measuring node.
- **Round structure** (20 ms at 50 Hz): sensing phase 5.5 ms (all 5 nodes emit an 8-byte
  beacon at MCS0-LGI) then backhaul phase 14.5 ms (each remote gets an exclusive ~3.6 ms
  window). The phase split exists because **a radio cannot receive while transmitting** —
  unslotted 1400-byte packets would overlap someone's beacon ~11% of the time.
- **Rate config is per peer**, so backhaul rate changes cannot disturb the MCS0-LGI
  calibration invariant that CSI depends on.
- **The gateway MAC is learned, never configured** — read from the gateway's own sensing
  beacon (already an ESP-NOW broadcast).
- **The sensing path rejects backhaul by LENGTH** (`payload_len > 200`). Not optional: a
  backhaul packet *contains* relayed frames beginning with the very `{0x1D,0xC5,ver,tx}`
  header the CSI callback scans for. Without the gate it would inject phantom CSI **and
  corrupt the round clock**. Verified firing exactly once per round.

### Host link budget (measured, zero corrupt bytes in multi-MB integrity checks)

| baud | throughput | verdict |
|---|---|---|
| 921600 | 96.8 kB/s | insufficient for a relayed mesh |
| 3000000 | 318.2 kB/s | works |
| **4000000** | **423.0 kB/s** | current setting; 20 links need 327 |

The ESP32-S3's **second USB-C port is native USB** (~800 kB/s+) rather than the UART bridge.
Untested, but it is the documented upgrade path if link count ever outgrows the cable —
the reason not to cut links for bandwidth.

### The backhaul-rate lesson (cost 90% of one link's data; worth internalizing)

Backhaul initially ran at MCS5. The −77 dBm node then relayed **5.6 frames/s while its own
8-byte MCS0 beacons arrived at 58.8/s from the same radio.** That looks exactly like a dying
node and was purely modulation: a 1400-byte MCS5 packet needs ~−70 dBm; an 8-byte MCS0 one
survives far below. Default is now **MCS0-LGI for backhaul too**, because capacity permits
robustness (one node's round of CSI = 1.35 ms at 6.5 Mbps; four remotes use 5.4 ms of the
14.5 ms window, 2.7× headroom).

That failure also exposed a genuine bug, now fixed: frames were dequeued from the CSI ring
**before** `esp_now_send` succeeded, so the `ESP_ERR_ESPNOW_NO_MEM` a weak link's retry
backlog produces destroyed them **with no counter attached**. The ring is now consumed only
on successful queue, and a send callback separates **queued** from **ACKed**.

---

## 4. The observability diagnostic (why "buy more boards" is now justified)

`host/wisent/observability.py` + `scripts/observability_check.py`. Asks, per link: *during
this link's most-active moments, was the person actually in its geometrically sensitive
region?* Verdicts: `position-sensitive` / `flat/insensitive` / `quiet` (signal < 6 dB, i.e.
uninformative).

- Self-test (`scripts/observability_selftest.py`) proves it separates a walker-driven swing
  (ratio 5.04) from an **equally large interferer-driven** one (0.81) — that discrimination
  is the whole point.
- First run on an old walk recording returned **INCONCLUSIVE**, and the reason is a useful
  precedent: leg timings had not been recorded, and sweeping five plausible timing
  assumptions **flipped which link won**. The tool now abstains (`timing_robust()`) rather
  than answer confidently from an unrecorded assumption. It also had a real numerical bug
  (ratio → 2.4 × 10¹⁰ from a divide-by-underflow), now floored relatively.
- Run on the **station** recording (exact timings): links (0,3) and (1,4) are
  `position-sensitive` → **the front end tracks position where geometry favours it. The VRTI
  failure is starvation.** That is the finding that justifies spending on links/placement
  rather than on estimator tuning.

---

## 5. What to plan next (ordered by value per unit of operator effort)

### 5.1 Re-measure node positions, then repeat the station test — **top priority**
Nodes 1/2/3 were physically handled tonight; each gained **5–9 dB** at the gateway while
untouched node 4 moved 1 dB, so `config/room.yaml` is stale for three of five nodes (a
warning is in the file). VRTI and observability both consume those coordinates, so **no
geometry-dependent result should be trusted until they are re-measured** (±30 cm suffices).

Then re-run `python scripts/station_test.py` (script-cued, ~2 min 17 s, self-scoring). This
is the decisive test of whether 10 links fixes localization. It reports per-station error,
quadrant accuracy vs the ≥80% bar, and the observability verdict with exact timings.

Note node 2's sensitivity to height: **−62 dBm elevated, −77 dBm at floor level, −68 dBm
now.** That 15 dB is the difference between a useful link and a dead one; the excess loss is
a counter-height half-wall plus an adjacent coffee machine, and it is fully recoverable by
elevation. Worth fixing before the run.

### 5.2 Still-subject recording — unblocks two deliverables at once
Person seated ~2 m from a link, facing it, still for ~5 minutes, **counting breaths** (or
someone else counts). Dogs and other people out of the room. This yields:
- the **first breathing ground truth** (deliverable 2 has none), and
- the **regime isolation** the signed-Doppler channel needs: a still person is the
  single-dominant-scatterer case the ratio model assumes. If the ~51% self-disagreement
  persists there, body-extent smearing is exonerated and the cause is deeper.

### 5.3 Empty-room baseline for the current geometry
Still missing; every analysis so far used self-baselines with a stated caveat. 30–60 min
with people and dogs out — start it before leaving the house. Also produces the real
false-motion-rate number.

### 5.4 Reflash nodes 1/2/3 to the MCS0 backhaul default
They carry MCS5 (flashed before the rate lesson). They work at tonight's RSSI but have thin
margin — node 2 managed only 5.6 frames/s at −77 dBm. One plug-in each; not urgent, nothing
is broken today.

### 5.5 Publish the DSSS writeup
`docs/public/dsss-beacon-harvesting.md` is complete and self-contained. Publishing is what
closes its own last caveat (its compatibility table is vendor-documentation-based, not
audit-verified; contributions are what convert rows). Nothing else depends on it.

### 5.6 Backlog with reasons (see `docs/roadmap.md` for the full list)
Deferred deliberately: whitened/temporal `locate()` (evaluate against fresh ground truth,
not the old failed walk); `features.py` SNR items (they shift every baseline-calibrated
number — land with a baseline re-collection); CRC-16 + AGC logging (fold into any future
protocol change). Queued ideas include the **circle-walk ground truth** (subsumes oblique +
tangential + timing in one 60 s protocol — adopt for the next walk test), RSSI as a
first-class per-link observable, reciprocity disagreement as a link-quality weight, a
ratio-phase respirogram (mm-scale displacement), and a measured-field VRTI dictionary built
from station recordings.

---

## 6. Constraints a planner must not casually break

**Load-bearing code** — each of these was earned by a hardware failure with a healthy-looking
symptom (`rx=0` forever, ~9 CSI/s of pure noise, garbage timestamps). Do not "simplify":
the no-op `promisc_cb`; `esp_now_set_peer_rate_config(MCS0_LGI)`; the 4-byte header match;
the `rx_ctrl.rx_state != 0` reject; **imag-first I/Q decode**; the LTF-block pair guard; the
backhaul length gate.

**Architectural rules:**
- `host/wisent/sim.py` must never be imported by live-path modules (enforced by a check in
  `validate_synthetic.py`).
- Amplitude-only DSP across packets — raw CSI phase is random on this hardware. Complex
  math **within** one packet is the sanctioned exception and is where `ratios.py` lives.
- The 128-pair CSI buffer is **two LTF blocks measuring overlapping frequencies** (~52 + ~56),
  not one 108-wide frequency axis. Frequency-span reasoning gets ~52 distinct subcarriers;
  the second block is a second *estimate*. Buffer index ≠ frequency.
- Unmeasured layouts must fail loudly: 192-pair (HT40-clipped) buffers raise rather than
  invent a third block.
- Time base is **beacon/round sequence numbers**, not host wall clock.
- Keep host deps to numpy + scipy.

**Test suites that must stay green:**
`cd host && python scripts/validate_synthetic.py && python scripts/validate_protocol.py`
→ currently **11/11** and **38/38**. Also `scripts/observability_selftest.py` (3/3).

**Evidence hygiene:** no SSIDs, BSSIDs, or third-party MACs in anything shareable (a BSSID
maps to a street address in WiFi-positioning databases). A credential scan runs over the
repo; keep it clean. Recordings live in `recordings/` with `MANIFEST.md` (SHA-256).
`recordings/labelled_blind.npz` and `recordings/station1.npz` are the two most useful
labelled scenes.
