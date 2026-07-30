# wisent

*A wisent is the European bison: cheap to feed, unglamorous, very hard to knock over.
That's the design goal.*

**WiFi human-sensing on five $8 single-antenna ESP32-S3 boards — through-wall motion
localization, a live browser UI, and one USB cable for the whole mesh.**

Built in a few days of intensive human+AI pair work, in a real furnished home with kids
and dogs, under one governing rule: **no claim without a recording behind it.** Every
number below traces to an archived capture (SHA-256 in `recordings/MANIFEST.md`), every
failure is written up next to every success, and things we could not prove are labelled
as exactly that.

```
     0m   1    2    3    4    5    6    7    8    9   10m
   ┌───────────────────────────────────────────────────┐
   │          N2   ▒                             N1    │   5 nodes, 10 links
   │               ▒                                   │   ▒ = half-wall
   │                    N4                             │   only N0 has a cable —
   │                                                   │   the rest relay by air
   │ N3                                                │
   │                                              N0═USB
   └───────────────────────────────────────────────────┘
```

## What actually works (measured)

| result | number | evidence |
|---|---|---|
| 5-node TDM mesh, all-pairs CSI on **one USB cable** | 20/20 directed links, 0 CRC errors, 327 kB/s of a measured 423 kB/s ceiling | live counters, `docs/architecture.md` §Protocol v3 |
| Empty-room false-alarm rate of the full pipeline | **0.0%** (was 92.9% before the root-cause fix below) | `empty_ch11.npz` |
| Stationary-person localization, gain-calibrated, leave-one-out | **median 0.75 m, 5/5 quadrants** (from 3.05 m, 2/5 uncalibrated) | `station2.npz` |
| Through-wall link viability | half-wall cost 14.8 dB at floor level, **~0 dB elevated** — elevation, not distance, decided it | RSSI surveys in `config/room.yaml` |
| DSSS beacon-harvesting law | 3442 beacons from consumer mesh APs → **0 CSI**, mechanism + 60-s field audit tool | `docs/dsss-compatibility.md`, `firmware/mesh_audit/` |

## What honestly does not work (also measured)

| claim we do NOT make | why |
|---|---|
| Walking-person tracking | calibration is a *dwell* localizer; on walking legs it changes nothing (2.7 m → 2.8 m median). Unsolved. |
| Signed per-link Doppler on real humans | our own ground-truth-free cross-check fails on hardware (~51% self-disagreement vs ~1% synthetic). The channel is **gated off**; `docs/signed-doppler.md` documents the negative. |
| Breathing rate | plausible 10–12 bpm inter-link agreement over a sofa with two seated people, but **zero ground truth**, and we caught our own earlier reading being a filter artifact (the "rate" tracked the detrend cutoff). |
| Pose, identity, heart rate | physically closed on 1×1 radios at 2.4 GHz — `docs/theory.md` §5 explains why we refuse these. |
| The pre-registered 80% localization bar | leave-one-out 5/5 is strong evidence, **not** the bar: that requires a fresh blind run against frozen calibration, which we ran out of time for. |

## The five findings worth reading this repo for

**1. The ESP32 CSI per-block scale toggle** (`host/wisent/csi_io.py::merge_ltf_blocks`).
The silicon scales its two intra-packet channel estimates (L-LTF, HT-LTF) with
*independent* per-frame automatic shifts. On marginal-headroom link directions, one
block's scale toggles ~2× on 26–33% of frames — non-reciprocal, RSSI-invariant, invisible
to global normalization, and it masquerades perfectly as human motion. It polluted every
amplitude pipeline we built until an empty-room recording exposed it (a link 56% "active"
in one direction, 0.0% in the reverse of the same physical path — channels are
reciprocal, so the radio was lying). One line of algebra fixes it: **L1-normalize each
LTF block separately before merging.** False alarms went from 92.9% duty to zero on the
same capture. If you process ESP32 CSI amplitudes and have not handled this, some of
your "motion" is the scaler.

**2. Wireless CSI backhaul: the whole mesh on one cable** (`firmware/node/node.ino`).
Remote nodes format CSI into the same wire frames they would write to USB and ship them
to a gateway over ESP-NOW (v2 frames, 1470-byte payloads) inside a TDM round that
separates sensing beacons from backhaul bursts. The gateway writes relayed bytes to USB
verbatim — the host parser never learned relaying exists. Per-peer rate config keeps
sensing beacons locked at MCS0 (a calibration invariant) while backhaul runs separately.
Measured: relayed links at the same 59 frames/s as direct ones, round-clock offset
exactly 0, ~1 lost packet per 4000. Includes the failure that taught the rate lesson
twice: MCS5 backhaul starved a −77 dBm node (5.6 of 58 frames/s — modulation, not
distance), and the "fix" to MCS0 traded it for airtime pressure.

**3. Per-link gain calibration** (`host/wisent/vrti.py::fit_gains`). Radio-tomographic
localization assumes links respond comparably to the same person. Measured: they differ
by up to ~10× (deep-fade amplification), and the fitted geometry exponent is γ≈0.3 where
the standard model implicitly assumes 1.0 — most of what the model treats as position
information is actually per-link gain. Fitting 10 gains + γ on four 20-second labelled
dwells and localizing the held-out fifth: median error 3.05 m → **0.75 m**, quadrant
2/5 → **5/5**. The theoretically-cleaner "excess-only" correction *loses* to the naive
full-ratio one on real data (2.96 m vs 0.75 m) — we shipped the empirical winner and
documented the open question rather than the elegant loser.

**4. The DSSS compatibility law** (`docs/dsss-compatibility.md`,
`docs/public/dsss-beacon-harvesting.md`). Passive sensing that reads CSI from AP beacons
is gated by one setting: the lowest *basic rate*. 2.4 GHz beacons default to 1 Mbps DSSS,
DSSS has no OFDM training field, and ESP32 CSI comes only from OFDM training fields —
so most consumer/ISP deployments yield **zero CSI from beacons, forever, at any signal
strength**. We measured all three points of the curve on one radio (DSSS: none; non-HT
OFDM: 64 pairs; HT: 128), mapped which vendor ecosystems expose the fix, and shipped a
60-second audit tool. Vendor-documentation claims are marked *documented*, not
*verified* — contributions welcome to convert rows.

**5. Honesty as infrastructure.** The interesting methodology here is not any single
algorithm but the guard rails: pre-registered acceptance bars with Clopper–Pearson
bounds; null controls that killed our own favourite signal three times; a
ground-truth-free consistency check that caught the signed-Doppler channel lying on
hardware; an observability tool that answers "is this failure link starvation or
physics?" and *abstains* when leg timings weren't recorded; estimator gates that return
**no answer** instead of a confident wrong one; and validation suites (12 synthetic + 40
protocol checks) including one that asserts the simulator is never imported by the live
path. Four firmware bugs each produced *healthy-looking* failure modes (`rx=0` forever,
pure-noise CSI, garbage timestamps); their fixes are marked load-bearing in the source
and guarded by lockstep tests that parse the firmware.

## Architecture

```
firmware/node/       one sketch, five roles: TDM master / slaves, gateway / remotes
                     (compile-time flags). 50 Hz rounds; round counter = system clock.
firmware/mesh_audit/ 60-second "can this AP be harvested?" field instrument
host/wisent/         csi_io (wire parsing, LTF handling)  sanitize  features
                     vrti (imaging + calibrated matched-field locate)  breathing
                     linkbvp + ratios (signed-Doppler research channel, gated)
                     observability (starvation-vs-physics diagnostics)  sim (synthetic
                     validation ONLY — a test asserts it never touches the live path)
host/scripts/        live_ui.py (browser dashboard: map, per-link bars, gated dot)
                     station_test.py (self-cueing, self-scoring ground-truth runs
                     with voice + big-screen prompts)  live_capture.py  walk_test.py
                     validate_synthetic.py (12)  validate_protocol.py (40)
config/room.yaml     measured geometry — every engine consumes it
config/vrti_gains.json  the localization calibration (per-deployment by design)
docs/                theory, architecture, roadmap (results tables incl. failures),
                     signed-doppler + prior art, DSSS law, public writeup, handoff
```

Hard rules that shaped everything: amplitude-only DSP across packets (per-packet phase
is random on this hardware; complex math *within* one packet is the sanctioned
exception), every frame L1-normalized per LTF block, time base = round counter (never
wall clock, never `t_us` across nodes), host deps = numpy + scipy only.

## Reproducing

Hardware: 5× ESP32-S3 dev boards (single antenna; one was reported broken and measured
fine), USB chargers, one data cable.

```bash
# host checks (no hardware needed)
cd host && python scripts/validate_synthetic.py && python scripts/validate_protocol.py

# firmware (gateway = node 0; remotes 1..4 need power only)
arduino-cli compile --fqbn esp32:esp32:esp32s3:CDCOnBoot=default \
  --build-property "build.extra_flags=-DNODE_ID=0 -DN_NODES=5 -DBACKHAUL=1" firmware/node
arduino-cli upload -p <port> --fqbn esp32:esp32:esp32s3:CDCOnBoot=default firmware/node

# live dashboard
python scripts/live_ui.py         # -> http://127.0.0.1:8760

# calibration run for a new room (fill config/room.yaml first — measured positions)
python scripts/station_test.py --out my_room.npz
```

Gotchas that cost real time are documented where they bit: `CDCOnBoot` silence, DTR/RTS
auto-reset muting, the always-115200 ROM log, variable CSI width, MAC learning, and the
serial-port-open reboot.

## Recordings

`recordings/` holds ~330 MB of raw captures backing every claim; they are not in git
(size, and they are RF recordings of a private home). `recordings/MANIFEST.md` lists
SHA-256 hashes. A checkout without them should treat the results as
*claims-not-reproducible* and re-measure — the tooling to do so is this repo.

## Status

Development paused 2026-07-30 with the localization result fresh. The two experiments
the existing data cannot replace, for whoever picks this up: a fresh station run scored
blind against the frozen calibration (the actual bar), and a denser dictionary walk
(~8–10 spots) to move from parametric gains to measured fields. `docs/handoff.md` and
`docs/roadmap.md` carry the full state, including the backlog and the graveyard of ideas
that died with their evidence.

## License

MIT — see `LICENSE`. Do whatever you like with it; if you audit an AP's beacon rate or
reproduce the scale-toggle finding on other silicon, a PR with the row would be welcome.
