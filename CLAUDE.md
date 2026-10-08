# CLAUDE.md — project context for Claude Code

<!-- BEGIN modernpath-rdd (managed — do not edit; changes are overwritten) -->
The shared process is installed by the `modernpath` CLI. These files are a
versioned snapshot of the canonical `req-driven-dev` source repository.

@.modernpath/rdd/AGENTS.md

@.modernpath/rdd/PROCESS.md

@AGENTS.md
<!-- END modernpath-rdd -->

## What this project is

wisent: WiFi human-sensing on 5 bare single-antenna ESP32s (Arduino toolchain) + a Python host.
Three deliverables, in priority order:
1. **VRTI disturbance map** — through-wall motion imaging via variance-based radio tomography.
2. **Breathing monitor** — single-subject respiration rate, Fresnel-geometry-aware.
3. **LinkBVP (novel research)** — room-independent motion features from multi-node
   pseudo-Doppler fusion. This is the publishable part. Spec: `docs/linkbvp.md`.

Read `docs/theory.md` before changing any DSP — the design decisions all trace to physics
constraints there (1×1 radio ⇒ no usable absolute phase ⇒ amplitude + geometry diversity).

## Current state (2026-07-29)

- **Suites: `validate_synthetic.py` 9/9, `validate_protocol.py` 33/33.** Run both:
  `cd host && python scripts/validate_synthetic.py && python scripts/validate_protocol.py`
- **Layers 0–2 are proven on real hardware** with 3× ESP32-S3 (1 beacon + 2 listeners):
  beacon at `hz=100.00`, 97–99.7% reception, CRC 0.0000%, 0 gaps over 10 min, and 48082
  rows cross-node aligned on the beacon seq grid. Full table: `docs/roadmap.md`.
- `scripts/live_capture.py` records multi-port → one `.npz` (raw bytes + wall-clock +
  decoded tables); `--replay` re-decodes captured bytes. `pyserial` is a lazy import.
- **Four firmware bugs found on hardware, every one with a healthy-looking failure mode**
  (`rx=0` forever / ~9 CSI/s of pure noise / garbage timestamps). They are documented at
  their fix sites in `listener.ino` and `beacon.ino` — **do not simplify them away.**
  The load-bearing lines: the no-op `promisc_cb`, `esp_now_set_peer_rate_config(MCS0_LGI)`,
  the 4-byte header match, and the `rx_ctrl.rx_state != 0` reject.
- **Not proven, do not claim:** VRTI and anything needing link geometry (all boards have
  been ≤ 1 m apart on one desk); the 4-listener acceptance recording; reception at 3 m LOS;
  and the sub-Hz periodicity seen with a person nearby, where the two links *disagree*
  (28.8 vs 34.0 bpm) and there is no ground truth — that is M3's job to settle.
- No UI, no live heatmap yet.

## Signed Doppler (new research direction, 2026-07-29)

`docs/signed-doppler.md`: cross-subcarrier RATIOS cancel the per-packet random phase
(measured stable to 0.02 rad on the S3 vs uniform-random raw phase) and their winding
direction gives SIGNED per-link Doppler — unfolding LinkBVP's ±v ambiguity with no
hardware mods. Implemented: `wisent/ratios.py` (three gates: prominence,
mirror-asymmetry, still-baseline notch — each earned by a failed hardware null control),
`linkbvp.infer_velocity_signed[_joint]`, `sim.simulate_link_csi`, `scripts/walk_test.py`.
Synthetic: sign 10/10, heading unfolded to ~2°, vibration gated 6/6; hardware null
control passes 0/4. **Ground-truth walk test not yet run — do not claim the sign works
on real moving humans until it has.** Complex math WITHIN a packet is the sanctioned
exception to amplitude-only (CLAUDE.md conventions; theory.md §2).

## Hardware notes (learned the hard way, 2026-07-29)

- **FQBN**: `esp32:esp32:esp32s3:CDCOnBoot=default`. With `CDCOnBoot=cdc` the board is
  completely silent — this dev board's USB connector goes to UART0, not the native
  USB-Serial-JTAG, so `Serial` would be writing to unconnected pins.
- **Open serial ports with DTR/RTS low.** Asserted, they drive the auto-reset circuit and
  leave the board mute in the ROM bootloader. `live_capture.py` handles this.
- The ROM boot log is **always 115200** regardless of `Serial.begin()`. Reading at 921600
  shows it as garbage before the app's own clean output — do not mistake it for a baud
  mismatch (this cost real time).
- CSI length **varies per packet** (64 and 128 pairs seen). Never assume a fixed width —
  it is the PHY format: non-HT OFDM gives ~64 (L-LTF only), HT gives ~128 (L-LTF+HT-LTF),
  DSSS gives none at all. Measured, see `docs/dsss-compatibility.md` §1.1.
- **Listeners learn the beacon at runtime** (default `BEACON_MAC` is all-zero): they
  accept everything until a valid 4-byte wisent header appears, then latch that MAC and
  filter on it. The learned MAC is printed as `beacon=` in the stats line. This exists
  because a listener pinned to a swapped-out beacon goes silently deaf — `rx=0`, which
  looks exactly like a broken radio. Pin it explicitly only if you need to
  (`-DBEACON_MAC_BYTES={0xAC,0xA7,...}` — no spaces in the braces).

## Build / test commands

- Host tests: `cd host && python scripts/validate_synthetic.py && python scripts/validate_protocol.py`
  (both must stay at 9/9 and 33/33; `validate_protocol.py` needs a working `cc`)
- Deps: `pip install -r host/requirements.txt` (numpy, scipy only; keep it that way)
- Firmware: `arduino-cli compile --fqbn esp32:esp32:esp32s3:CDCOnBoot=default firmware/beacon`
  then `arduino-cli upload -p <port> --fqbn <same> firmware/beacon` (see Hardware notes)

## Conventions & hard rules

- **`host/wisent/sim.py` must never be imported by live-path modules** (csi_io, sanitize,
  features, vrti, breathing, linkbvp, ratios, live_capture, walk_test import nothing from
  sim). This is the anti-RuView
  guarantee; there is a check for it in validate_synthetic.py — keep it passing.
- Amplitude-only DSP. Never use raw CSI phase across packets (it's random on this hardware).
  Complex math WITHIN one packet is allowed and is where `ratios.py` lives (theory.md §2).
- Every frame is L1-normalized before features (AGC is erratic on ESP32 — measured fact).
- Units: SI, Hz, meters, breaths/min only at display edge. Time base = beacon seq numbers.
- Keep the host package dependency-light: numpy + scipy. Plotting only in scripts/.
- Binary serial protocol is specified in `docs/architecture.md` §Protocol — firmware and
  `csi_io.py` must stay in lockstep; change them together or not at all.

## Next tasks (updated 2026-07-29, post-review)

**State: 5-node TDM mesh live; Layers 0–2 proven; VRTI failed at chance at 7 links;
signed Doppler gated off on hardware (51% self-disagreement).**

1. **Walk test, revised protocol**: `scripts/walk_test.py` now defaults to 8 passes with a
   pre-registered Clopper–Pearson bar (95% LB ≥ 80%) and a runtime corroboration gate
   (`ratios.corroborated_signed_doppler`). Run toward/away plus the circle walk
   (roadmap backlog) — the corroboration gate means both gated and ungated outcomes land
   in one recording.
2. **Station-protocol VRTI re-run with timed legs** — the observability tool
   (`scripts/observability_check.py`) returned INCONCLUSIVE on the old walk purely for
   lack of leg timings; that free result gates whether to buy boards.
3. **Reflash nodes 1/2/3 on next plug-in** with `-DBACKHAUL=1 -DN_NODES=5` — they still
   lack the coasting fix AND protocol v3 backhaul. Nodes 0 (gateway) and 4 (remote) run
   v3 already; each remote flashed adds 4 more links to the gateway's single cable.
   Serial is **4 Mbaud** on v3 nodes (host tools auto-probe).
4. Then the review backlog in roadmap.md: whitened/temporal locate(), features.py SNR
   items alongside a baseline re-run, dropout-at-operating-point validation.

**Do not "fix" these — they are load-bearing:** the no-op `promisc_cb`,
`esp_now_set_peer_rate_config(MCS0_LGI)`, the 4-byte header match, the
`rx_ctrl.rx_state != 0` reject, imag-first I/Q decode, and the LTF-block pair guard.

## Things NOT to do

- Don't add pose estimation, heart-rate headline claims, or person identification —
  physically closed on this hardware; docs/theory.md §5 explains why. If asked, cite it.
- Don't "fix" weird-looking CSI amplitude by smoothing across subcarriers on-device
  (`channel_filter_en` stays 0 — subcarrier independence is load-bearing for diversity).
- Don't invent results. If a test didn't run on hardware, the README table says so.
