# LinkBVP — room-independent motion features from a swarm of 1×1 radios

> **Framing note (2026-07-29).** This document is written throughout for the *folded*
> (unsigned) observation |v·gₗ|/λ. A **signed** channel now exists — `wisent/ratios.py`,
> `linkbvp.infer_velocity_signed[_joint]` — which, if it survives hardware validation,
> replaces the folded grid search with a linear solve and removes the ±v ambiguity that
> §2 treats as inherent. The folded path remains the fallback and is what all *validated*
> results below use. See `docs/signed-doppler.md` and `docs/prior-art-signed-doppler.md`.

**Status: novel research. Validated on synthetic CSI only (see `host/scripts/validate_synthetic.py`).
Nobody has published this on single-antenna hardware. It may fail on real boards — that outcome
is publishable too.**

## 1. Motivation

WiFi-sensing ML dies in new rooms: CSI entangles body motion with the room's multipath
fingerprint, and models memorize the room (DensePose-from-WiFi AP: 43.5 → 27.3 on an unseen
layout; AdaPose: >45% drops). The one published cure with zero-retraining transfer is
Widar3.0's **body-coordinate velocity profile (BVP)**: recover the *physical velocity
distribution of the body*, which is room-invariant by construction, from Doppler spectra
observed at multiple receivers at different bearings (~92% cross-room accuracy).

Widar3.0 needs clean signed Doppler, which needs coherent phase, which needs multi-antenna
NICs. ESP32s have one antenna and random per-packet phase. **The research question: can the
BVP inversion survive when each receiver contributes only an *unsigned* (magnitude-only)
Doppler estimate, if you have enough receivers at diverse bearings?**

## 2. Signal model

Person ≈ dominant point scatterer at position **p** with velocity **v**. For link ℓ
(TX at **tₗ**, RX at **rₗ**), the reflected path length is dₗ = |p−tₗ| + |rₗ−p|, and

  ḋₗ = **v** · ( û(p−tₗ) + û(p−rₗ) )  ≡ **v** · **gₗ(p)**

where **gₗ** is the *bistatic gradient vector* (known once p is known — VRTI supplies p).
The interference term in received amplitude is A·cos(2π dₗ/λ + φ), so the amplitude
spectrogram shows a tone at

  fₗ = |ḋₗ| / λ = |**v** · **gₗ(p)**| / λ    ← **magnitude only; sign lost.**

That lost sign is the crux. One link cannot tell approach from retreat.
*(2026-07-29 update: the sign may in fact be recoverable per-link from cross-subcarrier
ratio winding direction — synthetically validated, unfolds this whole section's ambiguity
if it survives hardware. See `docs/signed-doppler.md`; everything below stands until the
ground-truth test passes.)* But the sign
pattern across links is *not* free: a single unknown **v** must explain all L observations
simultaneously. Each link constrains **v** to the pair of hyperplane-offset sets
{**v** : **v**·**gₗ** = ±λfₗ}; the feasible set is the intersection over links. For L links
in 2D, generic position: 2 links leave a 4-point ambiguity, 3+ links at diverse bearings
generically cut it to a single **±v pair**. That last global sign flip is *inherent* — the
folded model satisfies cost(**v**) ≡ cost(−**v**) identically, so no node placement can break
it within one window. Only temporal continuity (tracking p̂ across windows) or task
symmetry (train classifiers on sign-folded features) resolves or sidesteps it.

## 3. Algorithm (v0, implemented in `host/wisent/linkbvp.py`)

Inputs per 1 s window: p̂ from VRTI argmax; per-link dominant pseudo-Doppler f̂ₗ with
confidence wₗ (spectral prominence); links gated on motion energy.

1. Compute **gₗ(p̂)** for each active link.
2. Grid search v = (s·cos θ, s·sin θ) over s ∈ [0.1, 2.5] m/s × θ ∈ [0°, 360°):
   cost(v) = Σₗ wₗ ( f̂ₗ − |v·gₗ|/λ )².
3. **Joint (p, v) refinement** (`infer_velocity_joint`): re-run the search over a
   ±1.5 m position neighbourhood of p̂ and take the (p, v) pair minimizing the same
   cost. Rationale: p̂ error bends every gₗ and biases v; but with L ≥ 4 links there
   are L folded observations for only 4 unknowns (pₓ, p_y, vₓ, v_y), so the Doppler
   magnitudes themselves refine position. **M0 synthetic result:** joint refinement
   recovered a 1 m/s, 45° walk to 0° folded heading error / 0% speed error and pulled
   position error from 1.75 m (VRTI seed) to 0.35 m; across a 6-case sweep of
   headings/speeds, median folded heading error 0°, median speed error 0%, 6/6 within
   (25°, 30%) tolerance. Caveat observed: position is less identifiable than velocity
   (one case: 12° heading, exact speed, but 4.3 m position mirror) — velocity is the
   robust output, refined position is opportunistic.
4. Return argmin + the ± mirror; downstream consumers must handle the ±v pair until
   tracking disambiguates.
5. (M4+) Replace per-window point estimate with the full **BVP**: distribute spectrogram
   *mass* (not just peaks) over a velocity grid via the same |v·gₗ| forward model —
   a nonneg least squares / EM inversion, Widar3.0-style but with folded (|·|) projections.
   The folded operator is nonlinear, so solve by alternating sign assignment (E-step:
   pick signs minimizing residual; M-step: linear NNLS with signs fixed). Unexplored
   territory — this is the paper.

## 4. Why it might actually work (and what kills it)

For it: (a) folded-measurement recovery is a known-solvable class (phase retrieval,
one-bit CS) given diversity — bearings are our diversity; (b) synthetic validation
recovers heading to a few degrees with 4–10 links and realistic AGC noise; (c) even a
±v-ambiguous BVP is already a room-invariant feature for gesture classification —
classifiers don't need the sign resolved if training folds it too.

Against it: (1) real amplitude Doppler is smeared by body extent (torso+limbs = multiple
scatterers → spectrogram ridges, not tones) — mitigation: track the strongest ridge,
accept BVP mass spreading; (2) p̂ error from coarse VRTI biases **gₗ** — mitigation:
5-node VRTI is quadrant-level, and gₗ varies slowly with p away from node positions;
sensitivity analysis in validate_synthetic.py; (3) low SNR links vote wrong —
mitigation: prominence weighting + gating; (4) 100 Hz sampling caps |f| at 50 Hz ≈
3 m/s bistatic — fine for gestures/walking, not sports.

## 5. Evaluation plan (pre-registered, M4)

- Gesture set: push, pull, swipe-L, swipe-R, circle (Widar3.0-comparable subset).
- Protocol: 2 rooms train / 1 room held out entirely; 3 subjects; 20 reps each.
- Primary metric: cross-room accuracy of a classifier on LinkBVP features
  vs. the same classifier on raw spectrogram features (the ablation that shows the
  invariance claim). Target: LinkBVP ≥ raw+15 points cross-room, and cross-room drop
  ≤ 10 points (raw baselines typically drop 30–45).
- Negative results are reported in the same README table as positive ones.

## 6. Relation to prior art (for the eventual writeup)

Widar3.0 (BVP, coherent multi-antenna); phase retrieval / one-bit compressed sensing
(folded measurements); ESPARGOS & esp-ppb (the *hardware* route to coherence — LinkBVP
is the *algorithmic* route that keeps nodes at $5); PulseFi/COVID-Beat (ESP32
amplitude-only precedent). Claimed novelty: BVP-class inversion from **unsigned**
single-antenna Doppler magnitudes across a spatially diverse node swarm.
