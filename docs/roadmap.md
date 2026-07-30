# Roadmap

Milestones with pre-registered acceptance tests. A milestone is done when its tests pass
**on hardware, in the stated room count**, with recordings archived. Tests are written
down *before* the code runs — that's the point.

## M0 — Synthetic validation ✅ (done at handoff)

- `validate_synthetic.py` passed 5/5 at handoff (now 8/8 with the signed-Doppler checks): breathing 16.8 vs 17.0 bpm (+ no false positive
  on empty room); VRTI localization 1.17 m on a 6×6 m synthetic room (5 nodes,
  10 links); LinkBVP joint (p,v): 0° folded heading err, 0% speed err, position
  refined 1.75 m → 0.35 m. AGC corruption and noise enabled throughout.
  Sim-isolation check passes (live path imports no sim).
- Two M0 findings now baked into the code, revalidate on hardware in M2:
  (1) naive RTI argmax collapses onto node positions with only 5 nodes — locate()
  therefore uses exponent-invariant log-domain matched-field scoring instead;
  (2) the sensitivity kernel needs the bistatic gradient factor |g| — links are
  blind to motion on their own LOS segment.

## M1 — First contact (1 beacon + 1–4 listeners, ~1–2 sessions)

- [x] Beacon flashes, broadcasts 100 Hz — **hz=100.00, overrun=0, sendfail=0**, and now
      confirmed *received*: a listener recovered 99.7% of its sequence numbers.
- [x] Listener CSI callback fires **≥ 95 per 100 beacons: measured 99.7%** (2125 of 2132
      beacons in the window). Caveat: boards were adjacent (RSSI −11 dBm), not the
      specified 3 m LOS — repeat at distance. All `TODO(hw-test)` items resolved.
- [x] Binary framing: **0 CRC errors** in every run, incl. 14064 frames at 116/s and a
      10-min soak. Not yet across 4 simultaneous ports.
- [x] `live_capture.py` written: multi-port → timestamped .npz recording.
- [~] **Acceptance:** 10-minute recording, no gaps > 1 s, frames align on seq — **passed on
      2 listeners** (0 gaps, CRC 0.0000%, 48082 rows cross-node aligned). Needs boards 4–5
      for the specified 4 listeners, and a repeat at 3 m LOS rather than ~1 m.

### M1 hardware results (2026-07-29, 3× ESP32-S3)

Hardware: ESP32-S3 rev 0.2, arduino-esp32 3.3.11. Beacon `ac:a7:04:ee:1a:34`;
listeners node 1 `ac:a7:04:1d:a9:94`, node 2 `ac:a7:04:ed:f3:bc`.
FQBN **`esp32:esp32:esp32s3:CDCOnBoot=default`** (see Board gotchas below).

| Measurement | Result |
|---|---|
| Beacon rate | `hz=100.00`, `overrun=0`, `sendfail=0` |
| Beacon reception (boards adjacent, −11 dBm) | **99.7%** of sequence numbers |
| Beacon reception (nodes ~1 m, 10 min) | 97.3% / 98.0% |
| CRC error rate | **0.0000%** in every run (>150k frames) |
| Gaps > 1 s | 0, both ports, 10 min |
| Cross-node alignment | **48082 rows** on one seq grid; motion-energy correlation r=0.557 |
| Longest gap-free segment | 12053 rows on beacon seq (vs ≤4-row fragments on `t_us`) |
| CSI width | constant 128 pairs with rate locked; 64/128 mixed on ambient traffic |
| Link throughput | **72.1 kB/s @ 921600** — 6× one HT20 listener; batching not needed |
| Engines on real CSI | `sanitize`/features finite; empty-room `estimate_bpm` correctly declines (prominence 2.35 < gate 3.0) |

Resolved from the installed core headers: `wifi_csi_config_t` is the classic struct on S3
(not HE), `rx_ctrl.cwb` exists, `info->payload`/`payload_len` exist (so the seq scan
compiles), `first_word_invalid` = first 4 bytes = 2 I/Q pairs. All `TODO(hw-test)`
markers are closed.

**Four firmware bugs, every one with a healthy-looking failure mode:**

1. **No promiscuous RX callback.** Promiscuous mode was enabled without registering a
   packet callback, so IDF delivered nothing and the CSI callback never fired — while
   every `esp_wifi_*` call returned `ESP_OK` and the listener cheerfully reported `rx=0`
   forever. A **no-op** callback is the whole fix.
2. **ESP-NOW PHY rate never locked.** `architecture.md` always specified MCS0-LGI; the
   default is a low 802.11b DSSS rate, and **DSSS carries no OFDM training field, so the
   receiver produces no CSI at all.** ~100 beacons/s yielded ~9 CSI/s (all of it
   unrelated ambient traffic) until `esp_now_set_peer_rate_config` was set → 105.8 CSI/s.
   Locking the rate is also what makes CSI width constant.
3. **2-byte magic match.** ~0.03% of ambient frames contain the bare magic by chance,
   yielding garbage seq (spans of 4.2e9). Now matches the full 4-byte header; `BEACON_MAC`
   is a build flag, and with the filter on the stream is 100% seq-locked.
4. **Air corruption defeats the UART CRC.** ~0.01% of frames (weak RSSI, during motion)
   arrived CRC-valid with random seq: the CSI callback also fires for FCS-failed
   receptions, and the listener CRCs already-corrupted RAM. Firmware now rejects
   `rx_ctrl.rx_state != 0`; the host also drops isolated wild-seq frames, which protects
   existing recordings on replay.

**Board gotchas.** Built with `CDCOnBoot=cdc` this board is *completely silent* — its USB
connector reaches UART0, not the native USB-Serial-JTAG. Opening the port with DTR/RTS
asserted parks it in the ROM bootloader. The ROM boot log is always 115200 regardless of
`Serial.begin()`, so it reads as garbage before the app's clean output — not a baud fault.

**Open observation (for M3, not a claim).** On still stretches with a person ~1 m away,
both links independently report a confident sub-Hz periodicity but **disagree on rate**
(28.8 vs 34.0 bpm) with no ground truth. May be real breathing or an environmental
periodicity. Notably 2-link fusion *suppressed* a 34.7 bpm single-link reading to "no
reading" — link diversity rejecting a single-link artifact, as theory.md §3.1 predicts.

### M1 pre-flight ✅ (2026-07-28, software only)

`scripts/validate_protocol.py` settles Layer 0/1 without boards, so bench time goes to
radio problems rather than parser problems. It extracts the CRC-8 **from `listener.ino`**,
compiles it with `cc` and fuzzes it against the Python decoder; parses the frame offsets
out of the `.ino`; and compiles the beacon's own struct to confirm `seq` sits at `magic+4`.
Parser hardening covers byte-at-a-time delivery, interleaved ASCII stat lines, 60 kB of
garbage plus adversarial false magics, truncation, and corrupted length fields — all 968
single-bit corruptions of a frame are rejected. CRC accounting separates real corruption
from resync noise so the M1 metric measures what it claims.

## M2 — Motion + map (1 room)

### First labelled presence result (2026-07-29) — sobering, and worth reading before M2

`recordings/labelled_presence.npz`: 10.5 min, 3 links (all terminating at node 0), three
operator-labelled regimes. Detection rate per 2 s window, thresholded at the **empty
window's 98th percentile** (i.e. M2's own 2% false-motion budget):

| link | person at FAR end | person at DESK |
|---|---|---|
| tx1→rx0 | 3.0% | 4.2% |
| tx2→rx0 | 2.1% | 18.7% |
| tx3→rx0 | **10.0%** | **53.5%** |

Per-link empty floors differ 6× (1.9e-2 on tx3→rx0 vs 1.2e-1 on tx2→rx0, the weak
through-clutter link) — which is exactly why the baseline must be *measured*, never assumed
as one global threshold.

**Read honestly:**
- A person **at the far end is not detected** — 10% on the best link against a 2% false
  rate is barely above chance. With all three links terminating at node 0 (the desk
  corner), the far half of the room has poor *sensitivity*.
- **Sensitivity is not conditioning.** The earlier "99% of the room usable" figure measures
  whether the inverse problem is well-posed, NOT whether a disturbance is detectable. Good
  conditioning over a region the links barely illuminate buys nothing.
- The one unambiguous event was an **arrival transient** (someone walking in: 14.6× floor
  for a few seconds). Sustained detection of a *seated* person is weak — physically
  expected, since seated ≈ still, which is the regime the architecture hands to breathing
  rather than motion.
- Not a parameter artifact: sweeping `sanitize` detrend 1→16 s moves tx3→rx0 not at all
  (53.5% throughout) and tx2→rx0 only 10.7%→23.4%.
- Labels are operator recollection to ~±1 min, not instrumented. The sensor's strongest
  event sits at 14:32-14:33, consistent with the stated desk arrival.

**Implication for M2:** the empty-room criterion needs 1 h (we have ~7 min), and the walk
test needs the three links that cross the far end (1-2, 1-3, 2-3) — which requires two more
nodes on USB, not more cable length.

- [ ] Empty-room: false-motion < 2% of windows over 1 h.
- [ ] Walk test: VRTI quadrant correct ≥ 80% of windows (prescribed path, 5 min).
      **First attempt 2026-07-29: FAILED at chance.** `recordings/walk_vrti.npz`, 5 nodes,
      7 links, operator walked nodes 3→2→1→0. `locate()` in 10 s windows should have swept
      x from 0→2→9→9; instead it jumped between opposite ends of a 10 m room within single
      windows (`9 9 9 0 3 7 9 8 9 7 3 3 1 8 3 7 3 9 9 8 9 9 9`), x-monotonicity **55%**
      against ~50% chance. `image()` was unusable, as predicted for 7 measurements over
      800 voxels.
      Diagnosis — two measured causes, neither a software bug:
      (1) **Node 2's quadrant is unobservable.** Its links sit behind the half-wall at
      −65 dBm with a floor 6× the others; standing *at* node 2 moved them only 1.3–2.9×,
      i.e. not at all. A quadrant with no observability cannot be localised.
      (2) **Seven links is too few**, three of them weak. theory.md §3.2 puts ten links at
      "coarse zone-level"; we are below that and the matched-field score has ~800 voxels
      to choose between on 7 numbers.
      Protocol fault to fix next time: a *stationary-station* protocol was specified
      (step in place at known coordinates) but a continuous walk was performed, so each
      10 s window averages over metres of travel — which blurs the quantity being
      localised. Re-run with stations, and record leg timings.
      **Prediction check:** beforehand I expected `locate()` to find the right region and
      `image()` to be poor. `image()` poor was right; `locate()` was *worse* than predicted.
      An earlier apparent 2-for-2 on `recordings/labelled_presence.npz` (3 links) should
      now be read as luck, not signal — this run is the larger sample.

      **Re-analysed 2026-07-29 after the abstention gate and per-link baselines were added
      — the gates do NOT rescue it, and that is the useful finding:**
      - Abstention fired on **0 of 23** windows; output byte-identical to before. The gate
        catches *no evidence* (real: an empty room previously returned an arbitrary
        corner), but this failure is *evidence present, too few links to localise it*.
      - Leave-one-link-out displacement is a median of only **0.73 m** — the estimate is
        **stable but stably wrong**. Gating on >2 m spread rejects 30% of windows and
        leaves the remainder at **53% monotonic**, still chance.
      - Interpretation: with 7 links over 800 voxels the matched-field kernel dominates and
        the measurements barely move the answer — prior-dominated, not data-driven. **No
        internal self-check distinguishes this from a good estimate.**
      - Consequence: **VRTI cannot be made honest by adding gates.** It needs more links or
        a coarser output space (a calibrated zone posterior rather than an 800-voxel map).
        Do not spend further effort on estimator guards at K=5.
- [ ] Through-interior-wall: motion detected ≥ 90% of 30 walk events, nodes in adjacent room.
- [ ] Live heatmap script (matplotlib is fine).

## M3 — Breathing (2 rooms)

- [ ] Subject at 2 m, facing link, still: within ±1 bpm of manual count for 5 min, both rooms.
- [ ] Orientation study: repeat at 90° — expected to degrade; *document the numbers*.
- [ ] No-subject control: reports "no confident reading" (not a number) ≥ 95% of windows.
- [x] Round-robin TDM firmware (all-pairs links) — **built and working, 2026-07-29**.
      `firmware/node/` (protocol v2). First 3-node capture: all **6 directed links** live
      — but note that is **3 unique pairs**: g_ij(p) = g_ji(p), so a reverse link is a
      repeated measurement of the same geometry, not a new constraint. Count unordered
      pairs for rank; use reciprocal disagreement as a quality channel instead.
      **CRC 0.0000%, 0 gaps**, per-link reception 92–100%, and **91% of rounds carry all 6
      links**. Why it matters here: with listeners clustered on short USB cables the star
      geometry is degenerate (23% of the room usable, cond(G) median 24); all-pairs takes
      that to **93% usable, median cond 2.7** without moving a board. It also lifts the
      joint (p,v) inversion from underdetermined to overdetermined **only once there are
      >= 4 unordered pairs** (4 unknowns: px, py, vx, vy). Four nodes give C(4,2)=6 unique
      pairs, so 4 nodes qualify and 3 nodes (3 pairs) do not — regardless of how many
      directed links are recorded.
      **Open anomaly:** the master receives ~1.2–1.6 copies per round of each slave frame,
      the extra copies 25 dB weaker (−46 vs −70 dBm), same round and width. Slaves see
      exactly 1.00 copies/round, so it is receiver-side at node 0 — a second path or a
      stack-level retry, not a scheduling fault. `frames_to_segments` dedupes on seq so the
      timeline is unaffected; the cost is ~50% wasted UART on the master. Root cause
      unidentified — do not close this silently.

## M4 — LinkBVP research (3 rooms) — see docs/linkbvp.md §5

- [ ] Webcam-teacher recording rig (synchronized video + CSI, one command).
- [ ] Gesture dataset: 5 gestures × 3 subjects × 20 reps × 3 rooms.
- [ ] Pre-registered eval: cross-room LinkBVP vs raw-spectrogram baseline.
- [ ] Writeup of results — positive or negative — in docs/.

## M5 — Publication / release

- [ ] Public repo with recordings for every README claim.
- [ ] "What this cannot do and why" section front and center (theory.md §5 distilled).
- [ ] Optional: ESP32-C5/C6 port; esp-ppb phase-coherence experiment (the *hardware* route
      to signed Doppler). Note `docs/signed-doppler.md` proposes an *algorithmic* route to
      the same unfolding, with no hardware change — validate that first, it is cheaper.
      The C5 port also unblocks mesh harvesting for free: 5 GHz has no DSSS, so AP beacons
      there are OFDM by construction and harvestable without touching the AP
      (`docs/mesh-harvesting.md` §1c — weigh it against worse wall penetration).

### Station run #1: the fork is RESOLVED — it is starvation 🟢 (2026-07-29 evening)

`recordings/station1.npz`: script-cued stations at known coordinates, exact timings (the
thing whose absence made the first observability verdict inconclusive).

**Observability, decisive:** links (0,3) (ratio 1.56, 9.2 dB) and (1,4) (3.43, 6.4 dB) are
position-sensitive. **The front end tracks position where geometry favours it — the VRTI
failure is link starvation, not a front-end defect. More/better-placed links is the
lever.** (The quiet gate also earned its keep: link (0,1) showed ratio 8.63 on 3.8 dB of
signal and was correctly refused as uninformative.)

**VRTI:** errors 0.51 / 4.79 / 4.43 / 3.25 m, quadrant 1/4 (bar: 80%). The per-station
response matrix explains the pattern: **station 1 had a single dominant responder**
(link 0-3 at 10.2x floor) and localized to 0.51 m — the best localization this project
has produced. Stations 2–4 produced diffuse 2–8x responses across many links, and with 7
links the matched field resolves diffuse evidence wrongly (all three estimates collapsed
toward the desk half). Rule of thumb now on record: **one loud link localizes; five
murmuring links mislead.**

**Node 2 quadrant, partially healed:** at station 2 its links responded 3.1x/4.4x (vs
1.3–2.9x standing AT the node before the elevation+coasting fix). Improved, not solved —
RF-level recovery (−62 dBm, free space) did not fully translate into motion sensitivity.

**Cheapest next lever — a third hub, no new hardware:** the three unrecorded pairs
(1,2), (1,3), (2,3) all cross the lower half of the room, exactly where stations 2–3
failed. Putting a third node on USB (three ports exist) captures two of them → 9 of 10
links. That is the "more links" the fork verdict calls for, for free.

### Protocol v3 wireless backhaul — WORKING on hardware 🟢 (2026-07-29)

One cable for the whole mesh. Design + measurements: `architecture.md` §Protocol v3.
Verified with gateway (node 0) + one remote (node 4): **8 links on a single USB cable**,
0 CRC errors, 0 backhaul packet loss, round-clock offset 0 between relayed and direct.

**DONE — all five nodes on v3:** 20/20 directed links, 10/10 unique pairs, median 20 links
per round, 90% of rounds complete, 0 CRC errors, 327 kB/s of a 423 kB/s ceiling. **This is
the first time the project has had >=10 links** — the threshold the starvation verdict
named as VRTI's blocker, so the next station test is the real experiment.

Two honest caveats on the current fleet:
- Nodes 1/2/3 still carry **MCS5** backhaul (flashed before the rate lesson). They work at
  tonight's RSSI (−57/−68/−64 dBm) but have little margin: node 2 relayed only 5.6 frames/s
  when it sat at −77 dBm. Reflash each to the MCS0 default on its next plug-in.
- **Node positions moved tonight.** Nodes 1/2/3 each gained 5–9 dB at the gateway after
  being handled, while untouched node 4 moved 1 dB. `config/room.yaml` coordinates are
  therefore stale for 1/2/3 — re-measure before trusting any geometry-dependent result.

If throughput ever binds, the ESP32-S3's **second USB-C port is native USB** (~800 kB/s+)
rather than the UART bridge — the upgrade path, and the reason not to cut link count.

### THE PHANTOM-MOTION ROOT CAUSE: per-LTF-block auto-scale toggle 🟢 FOUND & FIXED (2026-07-30)

The "hotspots jumping with nobody moving" mystery, chased through clock accuracy
(exonerated with numbers), pipeline gaps (exonerated), backhaul congestion (a false alarm
from a broken loss metric), interference (couldn't explain non-reciprocity) — is the
ESP32's CSI engine scaling the LLTF and HT-LTF blocks with INDEPENDENT per-frame automatic
shifts. On marginal-headroom directions the HT-LTF scale toggles ~2x on 26-33% of frames
(`recordings/empty_ch11.npz`: discrete half-amplitude cluster; block-ratio CoV 0.26-0.29
dirty vs 0.06 clean). Global L1 normalization cannot remove a relative inter-block flip,
so it surfaced as motion energy: links at 45-61% of quiet windows above 2x floor in a
LITERALLY EMPTY room, non-reciprocal (0->4 56% while 4->0 0.0% on the same path — the
observation that cracked it), RSSI-invariant, no cross-link structure.

**Fix: L1-normalize each block on its own used subcarriers BEFORE averaging**
(`csi_io.merge_ltf_blocks`, with a toggle-immunity lockstep check). Verified on the same
empty-room capture: every pair's max excursion <=1.75x, full UI chain false-alarm duty
92.9% -> **0.0%**.

**Historical results were polluted and re-score dramatically differently.** Station test
re-scored with the fix, same recording, same geometry-of-the-day:

| station | old err | NEW err |
|---|---|---|
| (0.5,3.5) near n3 | 0.51 m | 0.51 m |
| (2.2,0.9) near n2 | 4.79 m | **0.39 m** |
| (5.0,2.5) centre | 4.43 m | 3.19 m |
| (8.3,3.9) sofa | 3.25 m | 6.40 m (collapses toward n2 corner) |

Median 3.86 -> **1.85 m**; quadrant 1/4 -> **3/4** (bar: 80%). Two stations now localize
to <0.55 m — the first real localization this project has produced. The remaining failure
mode changed character: desk-end/diffuse stations collapse toward one corner, i.e. an
uncalibrated-per-link-gain problem (the measured-field dictionary item), not a noise
problem. The M2 "VRTI failed at chance" verdict is therefore SUPERSEDED: it was measured
through a corrupted pipeline. **Next: a fresh station run with everything compounded —
10 links + clean pipeline + new geometry + quiet channel — against the pre-registered
80% quadrant bar.**

### Station test #2 — clean pipeline, 10 links, ch11 (2026-07-30) 🟡

First run with everything compounded. `recordings/station2.npz`. Two scoring bugs found
and fixed first (v3: one port carries five receivers; time base must be the ROUND COUNTER,
never t_us across nodes — mixing five uptime clocks stretched 165 s to a fabricated 431 s).

Result vs the pre-registered >=80% quadrant bar: **2/5, median 3.05 m — bar not met.**
errs: 3.05 (blind spot), **0.55** (by n3), 3.37 (by n2), 4.28 (centre), **1.19** (by n1).

The informative failure shape: estimates land ON node coordinates (4.0,2.1 / 0.2,3.8 /
9.1,0.5 are literally nodes) — the matched-field proximity kernel 1/(r_tx*r_rx) peaks at
node positions, so with clean evidence the estimator acts as a NEAREST-NODE classifier
(3/5 by that metric, and the two sub-1.2 m "hits" were stations near nodes). Noise is no
longer the limit (empty room: 0.0% false alarm); the KERNEL is. Two levers, in order:
1. **Measured-field dictionary** — replace the geometric kernel with per-link response
   vectors measured at known spots. Needs one denser run (~8-10 spots x 15 s, ~4 min of
   operator time); station2's 5 dwells are the seed but cannot leave-one-out themselves.
2. Whitened/temporal locate() from the review backlog, once the dictionary exists.

### Gain-calibrated locate(): LOO 5/5 quadrants, median 0.75 m 🟢 (2026-07-30)

With no new operator data — station2's five dwells + the empty-room floors — per-link
gain calibration (`VRTI.fit_gains` -> `locate(gains=)`) turned the nearest-node
degeneracy around, leave-one-out validated through the full gated estimator:

| | uncalibrated | calibrated (LOO) |
|---|---|---|
| median error | 3.05 m | **0.75 m** |
| quadrant | 2/5 | **5/5** |

Three held-out stations under 0.75 m. Fitted gamma ~0.2-0.5: real link responses vary
FAR more weakly with geometry than the kernel's implicit gamma=1 — most of what the
kernel treated as position information was per-link gain. Calibration artifact:
`config/vrti_gains.json` (per-geometry/channel, as VRTI always was); live UI loads it.
Honest scope: n=5 LOO is strong evidence, NOT the pre-registered bar — that requires a
fresh walk scored blind with these fixed gains. A denser dictionary run (~8-10 spots)
remains the next data collection; the calibration seeds it.

## External-review backlog (2026-07-29)

Adopted immediately: slave round-coasting (`coast=` counter), runtime corroboration gate,
Nyquist-capped speed grid, HT40/192-pair loud guard, `hw128` synthetic layout + check,
prominence-weighted breathing fusion with per-link disagreement reporting, abstention
weights in `locate()`, CP-bound walk-test bar, doc staleness fixes.

Deferred with reasons:
- **Whitened inversion + temporal (acceleration-prior) tracker for locate()** — do with
  the station-protocol re-run; measured conclusion stands that at 7 links the kernel
  dominates, so evaluate against fresh ground truth, not walk_vrti.
- **features.py SNR items** (persistent top-k selection; variance-weighted spectrogram
  averaging) — real, but they shift every baseline-calibrated number; land them together
  with a baseline re-collection, not mid-stream.
- **CRC-16 + AGC/noise-floor logging** — fold into any future protocol v3; rx_state
  already catches the observed corruption class.
- **Dropout-at-operating-point validation** (engine accuracy at 90% reception, bursty) —
  add to validate_synthetic before the next round of engine claims.
- **Ideas queued**: gap-diversity sign vote (dk ∈ {8,16,24,32}); RSSI as a first-class
  per-link observable (strongest exactly on the −77 dBm links); reciprocity disagreement
  as link-quality weights; LTF-cross ratio as a per-frame phase-noise/STO instrument
  (makes every recording self-certifying); ratio-phase respirogram (mm-scale displacement;
  needs the same literature pass as signed Doppler); static-ratio room fingerprint
  (drift/moved-node alarm + recording↔calibration binding); **circle-walk ground truth**
  (subsumes oblique+tangential+timing in one 60 s protocol — adopt for the next walk
  test); measured-field VRTI dictionary from station recordings; tier-0 RSSI harvesting
  from DSSS beacons (an afternoon; would partially revive mesh-harvesting on locked APs);
  RANSAC consensus over links for LinkBVP; placement-optimizer script.

## Standing risks

- ~~UART throughput~~ **retired** for S3-class boards: measured 72.1 kB/s at 921600, ~6×
  one HT20 listener. Revisit only if a slower USB-UART bridge appears.
- ~~CSI API drift~~ **resolved** on arduino-esp32 3.3.11 / S3 (classic `wifi_csi_config_t`);
  still open for C5/C6, which use `wifi_csi_acquire_config_t` (M5 port).
- ~~Beacon seq not visible~~ **resolved**: `info->payload` exists, the scan locks, and the
  `t_us` fallback remains for degraded single-node operation (flags bit2 says which).
- **Transmit rate is load-bearing.** Anything whose CSI we want must be sent as OFDM.
  Measured on our own silicon, three points on the curve: 1 Mbps DSSS → **0 CSI**
  (0 of 3442 beacons); 6 Mbps non-HT OFDM → CSI at **64** pairs; MCS0-HT20 → CSI at
  **128** pairs. This bit our own beacon (M1 bug 2) and it is what gates passive
  AP-beacon harvesting — now mapped per vendor in `docs/dsss-compatibility.md`, with
  `firmware/mesh_audit` as the 60-second field test.
