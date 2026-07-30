# Mesh harvesting — sensing on existing WiFi hotspots

**Status: BLOCKED on THIS deployment — not on the idea. The blocker is a per-AP setting,
and which APs expose it is now mapped: see `docs/dsss-compatibility.md`. Enterprise /
prosumer / open gear (OpenWrt, UniFi, Meraki, Aruba, Cisco, Ruckus, MikroTik, EnGenius,
Omada) is controllable; mainstream consumer mesh and ISP gateways are locked, with ASUS
and AVM FRITZ!Box the consumer exceptions. Run `firmware/mesh_audit` to learn which you
have. No implementation should start until §1 passes.**

## 0. The idea, in one paragraph

Every 2.4 GHz frame a listener hears is a channel measurement. Mesh APs are attractive
transmitters of opportunity: always on, mains-powered, elevated, spread across the home.
Harvesting their beacons would turn K_ap radios × K_rx listeners into extra through-wall
links for the variance/RTI path — the cheapest possible coverage upgrade, since RTI
quality scales with link count and geometric diversity. (Commercial precedent: Cognitive
Systems' "WiFi Motion" over AP↔client links. That product does motion/presence, not
tomographic localization — so it supports the presence claim only.)

## 1. Prerequisite test — run this before anything else (60 seconds)

`firmware/mesh_audit/` counts, per BSSID, beacons received versus CSI callbacks produced.
**If yield is 0%, this entire document is dead for that deployment** and no host or
protocol work should be done.

Measured 2026-07-29 on the test network, channel 1, ~150 s:

```
AP (anonymised)   beacons   b/s   CSI  csi/s  yield  sig_mode  rate  rssi
AP1-a                 864   9.8     0   0.00     0%         0     0   -48
AP1-b                 862   9.8     0   0.00     0%         0     0   -48
AP2-a                 860   9.7     0   0.00     0%         0     0   -64
AP2-b                 856   9.7     0   0.00     0%         0     0   -64
CSI TOTAL = 0
```

(BSSIDs anonymised on purpose: WiFi-positioning databases map BSSID → street address, so
publishing a home's AP MACs publishes its location. The audit sketch prints the real ones
locally; keep them out of the repo. `AP1-a`/`AP1-b` were one radio, `AP2-a`/`AP2-b` another.)

Control, same sketch on channel 6 where our own beacon runs: **CSI TOTAL = 3688 in 38 s
(~97/s), all `sig_mode=1` (11n)** — the audit path is alive, so the zero is real.

**Why: `sig_mode=0, rate=0` is 1 Mbps DSSS.** DSSS has no OFDM training field, and ESP32
CSI is derived from LLTF/HT-LTF, so these beacons are structurally incapable of producing
CSI. They arrive perfectly (−48 dBm, rock-steady 9.8/s) and are radio-invisible to us.
This is the same root cause as M1 firmware bug 2, where ESP-NOW's default DSSS rate
yielded no CSI (`docs/roadmap.md`).

Two further facts from the same run:

- The beacon-rate assumption is **correct**: 9.7–9.8 Hz per BSSID, as expected for 100 TU.
- **Count radios, not BSSIDs.** `AP1-a`/`AP1-b` share an RSSI of −48, and the
  `AP2` pair shares −64: these are 4 BSSIDs on **2 physical radios** (main + guest).
  Multi-BSSID inflates any "K_ap × K_rx links" arithmetic ~2×.

**The unblock, if it exists:** force the mesh onto OFDM basic rates — disable 802.11b /
select "802.11g/n only" in the admin UI — then re-run the audit. Many ISP and mesh systems
do not expose this — `docs/dsss-compatibility.md` §2 maps who can and who cannot.

**Expect *different* CSI, not merely *some* CSI.** No longer a prediction: forcing one of
our own nodes to 6 Mbps ERP-OFDM yielded CSI at **64 pairs** versus **128** for MCS0-HT20
(`dsss-compatibility.md` §1.1). That is **one training field instead of two** — L-LTF only
versus L-LTF + HT-LTF — which cover overlapping frequencies, so the usable subcarrier span
is nearly unchanged. The loss is the second channel estimate (SNR/averaging), not half the
frequency diversity. The binding constraint on harvested links is the **~10 Hz beacon
rate**, which is why they stay slow-tier and never feed LinkBVP. With 11b disabled, beacons move to the
lowest basic OFDM rate — 6 Mbps, which is still **non-HT**. So `sig_mode` stays 0 and what
changes is the rate code (`0x00` → `0x0B`); the CSI is **L-LTF only, roughly half the I/Q
pairs of an 11n frame** (~64 vs ~128). We have already seen exactly this: ambient channel-1
traffic in `recordings/first_light.npz` yielded interleaved 64- and 128-pair frames — the
64s being non-HT. Consequences:

- The audit's success criterion is **"yield > 0 with a plausible pairs count"**, never
  "looks like the channel-6 control". `mesh_audit` prints the pairs count and a verdict
  per AP for this reason; PHY rate codes `0x00`–`0x07` are the whole DSSS set and are
  reported as *cannot ever yield CSI*.
- The host must not assume HT-sized buffers on slow-tier links. `frames_to_segments`
  already segments per CSI width, so mixed 64/128 streams stay on separate frequency axes
  rather than being stacked — that guard was added for ambient traffic and applies here.

## 1b. The compatibility finding — promote it, then check it

The mechanism generalises well beyond this project:

> ESP32-class CSI is computed from the OFDM training fields (L-LTF/HT-LTF). 802.11b DSSS
> frames have none, so they yield **no CSI at any signal strength**. Consumer 2.4 GHz
> beacons default to 1 Mbps DSSS for range and compatibility. Therefore **passive
> beacon harvesting requires administrative control of the AP's basic rates** — a
> deployment constraint, not a signal-processing problem.

This is worth a section of the eventual writeup rather than a footnote, alongside the
radios-not-BSSIDs correction below. Plausibly under-reported because the academic
mainstream runs Intel 5300-class NICs on *data* frames, where the issue never arises.

**Two honesty caveats before it is written as a result:**

1. **Prevalence is extrapolated from n = 1 deployment** (one mesh vendor, 2 radios). The
   *physics* is general; "almost universally 1 Mbps" is an expectation, not a measurement.
   `firmware/mesh_audit/` exists precisely to collect this cheaply — a multi-vendor table
   of (AP model → beacon rate → CSI yield) is what turns the claim into a result, and it
   is the kind of table other people can contribute in 60 seconds.
2. **The novelty claim needs a literature pass**, same discipline as
   `docs/signed-doppler.md`. "As far as we know, unstated" is not a citation.

## 1c. Not dead on 5 GHz — deferred to the C5 port

**There is no DSSS on 5 GHz.** 802.11a/n/ac are OFDM by construction, so every 5 GHz
beacon carries an L-LTF and is harvestable by any radio that can hear it, *with no AP
configuration change at all*. The classic ESP32/S3 is 2.4 GHz-only, but the **ESP32-C5 is
dual-band** and already sits in roadmap M5 as Espressif's top-ranked sensing part.

So the correct framing is **"dead on 2.4 GHz with this mesh; alive by construction on
5 GHz with C5 listeners"** — mesh harvesting is deferred, not abandoned.

Trade-offs to measure rather than assume when that port happens:

- **More motion sensitivity, more blind spots.** Phase per millimetre scales with 1/λ, so
  5.2 GHz (λ ≈ 5.8 cm) is ~2× more sensitive than 2.4 GHz (λ ≈ 12.3 cm) — but the Fresnel
  structure is correspondingly finer, so blind spots are denser (theory.md §3.1).
- **Worse wall penetration**, which cuts directly against the through-wall coverage that
  motivates harvesting in the first place. This may well dominate; do not assume 5 GHz is
  a straight upgrade.
- Beacons there are still lowest-basic-rate OFDM, i.e. **non-HT, L-LTF only** — same
  half-width CSI as §1.
- The C5 uses `wifi_csi_acquire_config_t`, not the classic `wifi_csi_config_t` (verified
  in the installed headers), so `listener.ino` needs the M5 port before any of this runs.
  Whether C5 CSI is exposed on 5 GHz at all is **unverified** — check it first.

## 2. Design that follows *if* §1 passes

Two tiers, and the separation is the whole discipline:

| | Fast tier (exists) | Slow tier (this doc) |
|---|---|---|
| TX source | wisent beacon, ESP-NOW, MCS0-LGI | mesh AP beacons |
| Rate | 100 Hz | ~10 Hz per radio |
| Time base | beacon seq | listener `t_us` |
| Feeds | full bus: Doppler, LinkBVP, breathing | motion energy / band powers / RTI only |

- **Beacon frames only.** Data frames are beamformed and rate-adapted, so the channel
  changes with no motion — a false-motion machine. Filter by frame type, not just MAC.
- **The slow tier must never enter `align_segments`.** That function deliberately refuses
  to build a cross-node grid from local clocks (`csi_io.py`). Slow-tier links are combined
  by binning per-link windowed features into common **wall-clock** windows. Sample-level
  alignment is neither available nor needed: RTI consumes ~1 s variance windows, and
  crystal drift (~40 ppm ⇒ ~12 ms over 5 min) is far inside that. Never mix time bases
  inside one `y` vector for `locate()`.
- **APs are nodes**: measured XY in `config/room.yaml`, one entry per *radio*.
- **Per-AP baseline, not a cross-AP threshold.** Auto-excluding an AP whose empty-room
  variance exceeds the wisent beacon's confuses distance and geometry with misbehaviour.
  Compare each AP against its own longitudinal baseline, or across listeners.
- **Don't register neighbour APs.** Their positions are unknown and their channels drift —
  and sensing through a shared wall via a neighbour's AP senses the neighbour. Log, never
  register.

**Protocol impact — now largely solved by TDM.** This section previously said a TX-source
byte would be a breaking change (header 16→17, version bump) and that the parser rejected
`version != 1`. Both statements predate protocol **v2**, which now exists for TDM: frames
carry `tx_id`, the header is 17 bytes, and `csi_io` parses v1 and v2 side by side.
Harvested sources can simply share the `tx_id` space (IDs 5–15 are unused), keyed to
BSSIDs by a host-side registry — no further protocol change required.

**Dropped from the earlier draft:** "rate on demand" (pinging an AP to drive a link at
~100 Hz) contradicted the beacons-only rule — the reply is a data frame, or an ACK at a
basic rate that is likely DSSS and therefore invisible anyway. Untethered UDP streaming
was also dropped for now: the listener's own uplink is transmitted traffic on the channel
it is measuring, and self-interference needs quantifying before it is worth the complexity.

## 3. Milestone M2.5 — only after §1 passes

- [ ] **Gate:** §1 audit shows non-zero CSI yield on ≥ 2 AP radios. *(Currently: 0%.)*
- [ ] AP beacon stability audit: 1 h empty-room per radio; per-AP-model numbers documented
      (that table is publishable on its own).
- [ ] Harvested-link registry: ≥ 6 (radio, listener) links with measured positions.
- [ ] Coverage A/B: repeat the M2 walk test star-only vs star+mesh. **Acceptance: the mesh
      tier improves through-wall walk detection by a stated margin (target ≥ +15 points),
      or the result is documented as negative.** Control for link *count* separately from
      geometry, or the A/B cannot say which one helped.
- [ ] Presence zoning: person-in-which-room over 3 rooms, ≥ 80% of 5-min windows.

Every claim in §0 stays design intent until those numbers exist, and the README capability
table gains a "mesh tier" column only then.
