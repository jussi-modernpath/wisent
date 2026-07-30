# Signed Doppler from cross-subcarrier ratios — unfolding LinkBVP

**Status: research note, 2026-07-29. Implemented in the repo** (`sim.simulate_link_csi`,
`wisent/ratios.py`, `linkbvp.infer_velocity_signed[_joint]`, checks `signed-doppler-*`
in validate_synthetic, ground-truth protocol in `scripts/walk_test.py`). Synthetic
validation, the real-hardware invariant check, and the **hardware null control** pass
(below). **NOT yet validated on a moving target with ground truth** — that is the walk
test, which needs a person to walk the line.

## The idea

linkbvp.md §2 treats the ±v ambiguity as inherent: amplitude sensing measures
|v·gₗ|/λ, sign lost, and only temporal tracking can disambiguate. This note claims the
sign is recoverable per-window, per-link, from data we already record.

Write one link's CSI as H(f,t) = A(f) + B(f)e^(−jθ(t)) with θ(t) = 2πd(t)/λ (static
paths + one moving scatterer). The **cross-subcarrier ratio** Rₖ(t) = H(fₖ,t)/H(fₖ₊Δ,t)
removes the per-packet nuisances — but by two different mechanisms, and conflating them
is an error this note previously made:

- **CFO and RCO cancel in the ratio.** They are common-mode across subcarriers within a
  packet, so they divide out exactly (Tsinghua *Hands-on Wireless Sensing with Wi-Fi*,
  arXiv 2206.09532 §5.4). AGC gain likewise.
- **STO/SFO does *not* cancel in a cross-subcarrier ratio.** Its phase is *linear in
  subcarrier index* (SpotFi, SIGCOMM'15 §3.2.2), so it is precisely the term that
  *differs* between two subcarriers and survives division. What defuses it is **per-PPDU
  timing re-acquisition**: re-syncing every packet makes τₛ zero-mean *jitter* rather than
  coherent drift (SpotFi §3.2.1), so it has no consistent winding direction and cannot
  masquerade as a signed tone. We bound that residual empirically at **≈1 ns RMS** (below).
  This matters because 802.11 tolerates ±20 ppm, and a *coherent* SFO slope could land
  squarely in the 0–50 Hz band we read — the jitter argument is the entire defence, so it
  must be cited rather than assumed, and **re-verified on any new silicon** (a CSI mode
  reporting pre-sync or phase-accumulated data would reintroduce a coherent tone).

To first order in |B/A|:

  Rₖ(t) ≈ (A₁/A₂) · [1 + (B₁/A₁ − B₂/A₂) e^(−jθ(t))]

— a **single** rotating term. Its winding direction is the sign of θ̇, i.e. the sign of
ḋ = v·gₗ. Each link then yields a *signed* fₗ = −v·gₗ/λ, and the folded grid search in
`linkbvp.py` collapses to weighted linear least squares with a **unique** v — no mirror.
**Why a ratio and not a conjugate product** — this is principled, not just an ESP32
necessity. Conjugate multiplication (the CACC lineage: WiDance, Widar3.0) *creates* a
mirror image of near-equal energy that downstream stages must then suppress
(arXiv 2607.20108 is explicit that the operation itself introduces it). Division keeps a
single rotating term and sidesteps the mirror entirely, at the cost of deep-fade
sensitivity in the denominator — which is what pair selection handles.

**What the winding does and does not mean.** The winding direction is the Doppler sign,
but the signed term's *amplitude* is weighted by delay selectivity. With static and
dynamic paths at definite delays (Bₖ ∝ e^(−j2πfₖτ_dyn), Aₖ ∝ e^(−j2πfₖτ_static)):

  |B₁/A₁ − B₂/A₂| ≈ |B/A| · 2|sin(π · δ·Δf · Δτ)|,  Δτ = τ_dyn − τ_static

So the signed term **collapses as δ·Δf·Δτ → 0** — a ratio-space analogue of the Fresnel
blind spot, and plausibly why the published single-antenna cross-frequency method (CFCC)
is LoS-limited. This method reads sign, *weighted by delay selectivity*; it is not "pure"
Doppler.

**Measured, and it bit us.** Sign accuracy versus subcarrier gap on a strong-LoS channel
(person 2 m off a 4 m link, Δτ ≈ 2–8 ns):

| δ (`dk`) | δ·Δf | strong LoS | clutter (8 paths) |
|---|---|---|---|
| 1 | 0.31 MHz | 25% | 50% |
| 2 | 0.62 MHz | 33% | 0% |
| 4 *(old default)* | 1.25 MHz | 75% | 100% |
| 8 | 2.50 MHz | 100% | 100% |
| 16 *(new default)* | 5.00 MHz | 100% | 100% |

Chance below ~1 MHz, solid from ~2.5 MHz. **`DEFAULT_DK` is therefore 16, not 4** — the
original default sat on the cliff edge. Pair selection now trades denominator strength
*within* a fixed, large-enough gap rather than across gaps. Note also that the repo
simulator's default random-per-subcarrier static path is the *friendliest* case; use
`sim.simulate_link_csi(..., n_static_paths=1)` for the hard one
(check `signed-doppler-delay-gradient`).

## Evidence so far

Scratchpad experiment (simulator = sim.py's model + random per-packet phase + erratic
AGC + STO jitter + noise; sign extracted as the signed spectral peak of detrended Rₖ,
pairs chosen by denominator strength):

1. **Single link, approach vs retreat, 20 trials/condition:** sign correct **100%** at
   0–20 ns STO jitter, 95% at 50 ns.
2. **Ten links, walker, six headings spanning 0–360°:** signed linear inversion recovers
   heading with **median 0.7° error, unfolded** — the existing folded pipeline's error is
   only defined mod 180° (it cannot tell forward from backward at all).
3. **Real ESP32-S3 data** (`recordings/seqlock.npz`, still room): raw CSI phase across
   packets is uniform-random (circular std 3.46 rad — phase is garbage, as documented),
   while the cross-subcarrier ratio phase is stable to **0.022–0.035 rad** (Δk = 1…16).
   Two consequences: the cancellation works on our silicon, and the barely-growing std
   vs Δk bounds ESP32-S3 STO jitter at **≈ 1 ns RMS** — 20–50× below where sign recovery
   degraded in simulation. The enabling invariant holds with wide margin.

## The null-control iteration (2026-07-29) — how the detector earned its gates

The naive detector (prominence gate only) was run as a null control on real hardware —
nobody walking, listener on the desk — **three times, failing twice, each failure adding
one physically-motivated gate:**

1. **Null #1 FAILED (4/4 phantoms):** a consistent ~36 Hz line with coin-flip sign —
   fan-class interferer (2160 RPM). Physics of the fix: a *vibration* is oscillatory and
   ±symmetric in the ratio spectrum; a *walker* is progressive and one-sided. →
   **mirror-asymmetry gate** (peak power must beat its own −f bin by 3×).
2. **Null #2 FAILED (2/4):** a −1.56 Hz line at conf 8.4 — that one was *real* (the
   operator sat 1 m away; seated-body sway lives below 2 Hz), plus one marginal noise
   peak at conf 2.7. → walk-test scoring uses the **walking band** (fmin 4 Hz; the
   protocol prescribes ~1 m/s ⇒ ~16 Hz) and the standard **prominence 3.0**.
3. **Null #3 FAILED (1/4):** the fan line wandered and went momentarily asymmetric. →
   **still-baseline notch**: the test's settling period is a per-run baseline; any line
   prominent while everyone stands still is environment and is notched from every
   walking segment (`ratios.spectral_lines`). This is architecture.md's empty-room
   calibration principle applied to the ratio spectrum.
4. **Null #4 PASSED: 0/4 confident readings.**

Null recordings are archived (`recordings/walk_null_fail1.npz`, `walk_null_pass.npz`).
The lesson worth keeping: the prominence gate alone — which is what the amplitude pipeline
uses — was fooled by every artifact class here.

## Hardware cross-check FAILS (2026-07-29) — the representation fixes were necessary, not sufficient

Two decode bugs were found and fixed by review: CSI I/Q was being read real-first when
Espressif stores **imaginary first** (yielding `j·conj(H)` — amplitude identical, every
phase negated, winding **reversed**), and ratio pairs were chosen by buffer index across
what is really **two LTF blocks** (LLTF then HT-LTF), so the nominal 5 MHz gap was often
neither 5 MHz nor within one block. Both are now fixed and test-guarded
(`codec-iq-byte-order`, `csi-ltf-block-layout`), and pair selection is verified on hardware
data to make 0 block crossings with every gap exactly 16 subcarriers.

**The signed channel still does not validate on real data.** The magnitude cross-check —
|f_signed| from the ratio channel versus |f| from the amplitude spectrogram, which needs no
ground truth — on a real walking recording:

| data | n | median rel. error | within 35% |
|---|---|---|---|
| synthetic, single point scatterer | 6 | **1%** | 100% |
| **hardware, walking person** | **19** | **51%** | **47%** |

Individual windows contradict outright: `signed −20.48 Hz` against `folded 3.34 Hz`, both
"confident". At least one channel is reading an artifact. Sign balance was 9+/10−, so the
convention is not stuck — the magnitudes simply disagree.

Candidate causes, in order of prior suspicion: **body-extent smearing** (a walker is many
scatterers, which §4 lists as the first thing that could kill this), 4 s windows being too
long for continuous motion, or a residual layout issue the tests do not cover.

Attempting to isolate the regime on archived data was **inconclusive**: the stationary-
subject windows yielded n=1–2 confident samples, far too few to compare against n=19 for
walking. Settling it needs a purpose-collected recording — a genuinely still subject,
several minutes, more links — not more analysis of what we have.

**Status: the ±v unfolding claim remains unvalidated on hardware, and the one
ground-truth-free check available disagrees at 51%. Do not quote a hardware sign result.**

## Limitations (measured, not tuned away)

- **Residual false positives on no-net-motion scenes.** In simulation, a vibrating-but-
  still scatterer yields a confident signed reading on a minority of 3 s windows — not the
  vibration line (the mirror gate rejects that reliably: measured mirror ratios 1.3–1.8
  against a 3.0 gate) but an *unrelated one-sided noise line*. The still-baseline notch is
  the deployed defence and is what made the hardware null control pass, yet it is **not a
  cure**: notching the dominant line merely promotes the next-strongest noise peak. Check
  `signed-doppler-rejects-oscillation` therefore asserts the *mechanism* (dominant
  oscillation ⇒ gated) and reports the leak rate rather than thresholding it green.
  **Update (review, 2026-07-29): this leak class now has a runtime veto** —
  `ratios.corroborated_signed_doppler` emits a signed reading only when the amplitude
  spectrogram independently shows a tone within 35% of |f_signed| (the bound the
  magnitude-consistency check validates at ~1% median on synthetic). A denominator-fade
  ratio artifact has no amplitude counterpart, so it is vetoed. On current hardware data
  the two channels disagree ~51%, so the gate vetoes often — which is the correct
  behaviour until that disagreement is understood.
- **Delay-selectivity blind spot.** Where Δτ → 0 — a scatterer near the LoS line, or a
  dynamic path at nearly the static path's delay — the signed term vanishes regardless of
  SNR. The failure mode should be "no confident reading", not a confident wrong sign; the
  walk test's tangential pass is what probes it.
- **Not yet validated on a moving human.** Everything above is simulation plus still-room
  hardware controls.

## Prior art (checked 2026-07-29 — needs a deeper pass before any novelty claim)

- **SA-WiSense** (arXiv:2507.17623): cross-subcarrier CSI *ratio* (CSCR) on ESP32 for
  **blind-spot-free respiration** — the same signal primitive, used for slow-motion
  complementarity. Validates feasibility on this hardware class; does not extract signed
  Doppler or invert velocity. We should *adopt* CSCR for M3 breathing (with citation) —
  that part is not novel.
- Cross-**antenna** conjugate ambiguity resolution (e.g. arXiv:2607.20108) needs ≥ 3 RX
  antennas — closed to 1×1 hardware.
- Widar3.0 BVP needs coherent multi-antenna Doppler.
- **WiDFS 3.0** (arXiv 2508.12614, 2025) and **CFCC** (IEEE TWC 2025) both achieve
  single-antenna *signed* Doppler. So "SISO signed Doppler" is **not** novel as such; CFCC
  is the closest, is cross-frequency, and is described as LoS-limited — which the Δτ
  analysis above explains.
- **Widar3.0** already does swarm→BVP fusion, so that concept is not novel either — only
  doing it from single-antenna front-ends is.

**Novelty framing that survives the review** (pending the retrievals below):

> First to recover **signed** Doppler from the **winding direction of a cross-subcarrier
> complex CSI ratio** on a **commodity single-antenna (ESP32) device** — avoiding both the
> ≥2-antenna requirement of CACC/CSI-ratio/MUSIC methods and the special dispersive-antenna
> hardware of WiRainbow — and to fuse such single-antenna signed Doppler across a device
> swarm into a Widar3.0-style body-velocity profile.

Every qualifier is load-bearing; drop one and it lands on existing work. Full landscape,
citations and the physics derivation: `docs/prior-art-signed-doppler.md`.

**Retrieve before any novelty language ships** (could anticipate the claim):
"Velocity Estimation Using Single Antenna WiFi Devices", APSIPA 2023 (USTC); and the CFCC
full text — confirm whether its cross-frequency correlation is our two-subcarrier ratio
winding under another name.

## What it would improve, concretely

1. **LinkBVP (M4, the paper):** ±v fold gone → unique heading per window, linear
   inversion (better conditioned than the folded grid), signed BVP without the
   sign-alternation EM in linkbvp.md §3.5.
2. **Breathing (M3):** adopt SA-WiSense-style CSCR as a second observable with blind
   spots in quadrature to amplitude's — directly attacks the observed link disagreement
   (28.8 vs 34.0 bpm on the same still stretch).
3. **VRTI/joint (p,v):** signed constraints double the information per link in the
   joint refinement.

## Known risks (pre-registered)

- Second-order term e^(−j2θ) scales as |B/A|² — a strong close reflector could corrupt
  the winding. Mitigation: pair selection; measure on hardware.
- A body is many scatterers, not one — ridges, not tones; the sign of the *dominant*
  ridge is what tracking needs, but smearing may cut confidence. This is the main
  hardware risk.
- Deep fades make the denominator explode — pair selection by denominator strength is
  load-bearing (SA-WiSense solves this with a GA; we start simpler).

## Next steps (in order)

1. ~~Extend `sim.py`~~ **done** — `simulate_link_csi` (full corruption model; the
   seeded amplitude path is untouched so M0 numbers stay bit-identical).
2. ~~`wisent/ratios.py`~~ **done** — pairs, ratios, detrend, three-gate signed peak;
   `linkbvp.infer_velocity_signed[_joint]` does the linear unfolded inversion.
3. **Ground-truth walk test — READY TO RUN, needs a human:**
   `python scripts/walk_test.py --port <listener> --passes 4`
   Person walks a straight line toward/away from the boards on the script's cues.
   **Pre-registered acceptance (revised 2026-07-29, before any run): Clopper–Pearson 95%
   lower bound on sign accuracy ≥ 80%, default 8 passes (16 segments).** The original
   "≥90% of segments" bar at 4 passes had almost no power — a true-95% method failed it
   34% of the time, and the null control passed a 10%-phantom method 66% of the time.
   A negative result gets written up too.
4. Literature deep-dive before any novelty language goes into linkbvp.md.
5. After (3) passes: feature bus grows a signed-fₗ column; LinkBVP M4 experiments run
   signed-first with folded fallback.
