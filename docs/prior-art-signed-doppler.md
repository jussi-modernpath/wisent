# Prior art & physics review for signed-doppler.md

**Status: literature deep-dive, 2026-07-29 — this is gate step 4 of `docs/signed-doppler.md`.
Read before any novelty language goes into `linkbvp.md`. Two conclusions: (a) the novelty
delta is narrower than the note assumed — the field reached single-antenna signed Doppler
in 2025–26 — and (b) there is a physics challenge a reviewer WILL raise, which the walk
test as originally specified does not probe.**

*Outcome: §3's objection was verified in simulation and found a real bug — the default
subcarrier gap was on a cliff edge. See §3.1 for the measurements and what changed.*

## 1. Verdict up front

Signed/directional Doppler on commodity WiFi has been solved since 2017, but almost always
via **cross-ANTENNA** operations (≥2, usually 3 antennas). The novelty of our method is
therefore NOT "signed Doppler on WiFi" and NOT "single-antenna signed Doppler" (both now
exist). What remains plausibly novel is the *specific combination*:

> signed Doppler from the **winding direction of a cross-SUBCARRIER complex ratio** on a
> **commodity 1×1 (ESP32) device**, fused across a **swarm** of such nodes into a
> Widar3.0-style body-velocity profile.

Every qualifier is load-bearing; drop any one and it lands on existing work. And two
papers we could not retrieve (§5) could still anticipate even that — retrieve them before
committing novelty language.

## 2. The landscape (who resolves the Doppler sign, and how)

| Work | HW (antennas) | Sign mechanism | Cross-? |
|---|---|---|---|
| CARM (MobiCom'15) | any | **none — speed only**, uses \|CFR\| power; toward/away identical | — |
| WiDance (CHI'17) | ≥3 RX | conjugate multiply H⁽¹⁾·H⁽²⁾\* (CACC) | antenna |
| IndoTrack (IMWUT'17) | 2 RX × 3 | Doppler-MUSIC + power adjust | antenna |
| Widar2.0 (MobiSys'18) | array RX | joint AoA/ToF/DFS | antenna |
| mD-Track (MobiCom'19) | arrays | joint 4-D ML estimation | antenna |
| Widar3.0 (MobiSys'19) | 3/RX, ≥3 links | CACC per node → **BVP fusion across links** | antenna |
| FarSense/FullBreathe (IMWUT'19) | 2 antennas | **ratio H₁/H₂**, arc rotation = displacement dir | antenna |
| Uplink CSI-Ratio (arXiv 2211.03250) | 2 antennas | ratio cancels TO+CFO, keeps signed Doppler | antenna |
| **WiDFS 3.0 (arXiv 2508.12614, 2025)** | **1 (SISO!)** | self-ref cross-correlation + delay-domain MVDR | — |
| **CFCC (IEEE TWC 2025)** | **1** | cross-frequency cross-correlation, **LoS-limited** | subcarrier-ish |
| WiRainbow (SenSys'26) | 1 + **special dispersive antenna** | per-subcarrier beam angle | subcarrier |
| **ours** | **1 (ESP32)** | **winding of cross-subcarrier ratio R_k(t)** | **subcarrier** |

The rows that matter most: **Widar3.0** already does the swarm→BVP fusion (that concept is
*not* novel — only doing it from single-antenna front-ends is); and **WiDFS 3.0 / CFCC**
already do single-antenna signed Doppler ("SISO signed Doppler" is *not* novel — only the
specific cross-subcarrier-ratio-winding mechanism on commodity hardware). CFCC is the
closest and is described as **LoS-limited** — see §3 for why that is not a coincidence.

## 3. The physics challenge a reviewer will raise

**Claim under scrutiny:** the winding direction of Rₖ(t)=H(f_k,t)/H(f_{k+δ},t) equals
sign(θ̇)=sign(ḋ)=sign(v·gₗ), i.e. true Doppler sign.

**The objection:** Doppler is ~frequency-flat across 20–40 MHz (θ̇ differs <1% between two
subcarriers), so the *common* Doppler rotation largely divides out in a cross-subcarrier
ratio. What the ratio actually retains, to first order, is

  Rₖ(t) ≈ (A₁/A₂)·[1 + (B₁/A₁ − B₂/A₂)·e^(−jθ(t))]

and the coefficient (B₁/A₁ − B₂/A₂) is governed by the **frequency selectivity** of the
static (A) and dynamic (B) paths — i.e. by their **delay difference** Δτ = τ_dyn − τ_static.
With B_k ∝ e^(−j2πf_k τ_dyn) and A_k ∝ e^(−j2πf_k τ_static):

  |B₁/A₁ − B₂/A₂| ≈ |B/A| · 2|sin(π · δ·Δf · Δτ)|

So the signed term **still rotates at θ̇** — the winding is genuinely the Doppler sign —
but its *amplitude* carries a delay-gradient weighting that **collapses when
δ·Δf·Δτ → 0** (dynamic and static paths at similar delay, or subcarrier gap too small).
This is the ratio-space analogue of the Fresnel blind spot, and it is almost certainly why
CFCC needs strong LoS: LoS gives a dominant static path with a *definite* delay, so a
moving reflector at a *different* delay produces a non-vanishing Δτ.

### 3.1 Verified, and it found a bug (2026-07-29)

The derivation was reproduced independently and tested in simulation with a
delay-structured static channel (`sim.simulate_link_csi(..., n_static_paths=1)`, person
2 m off a 4 m link, Δτ ≈ 2–8 ns). Sign accuracy vs subcarrier gap:

| δ (`dk`) | δ·Δf | strong LoS (1 path) | clutter (8 paths) |
|---|---|---|---|
| 1 | 0.31 MHz | 25% | 50% |
| 2 | 0.62 MHz | 33% | 0% |
| 4 *(old default)* | 1.25 MHz | 75% | 100% |
| 8 | 2.50 MHz | 100% | 100% |
| 16 *(new default)* | 5.00 MHz | 100% | 100% |

Chance below ~1 MHz of separation, solid from ~2.5 MHz. **The old `dk=4` default sat on
the cliff edge.** Changes made:

1. `ratios.DEFAULT_DK` 4 → **16**, with the formula and this table at the definition site.
   Pair selection now trades denominator strength *within* a fixed, large-enough gap.
2. `sim.py` gained `n_static_paths`. The previous random-per-subcarrier static path is the
   *friendliest* case (adjacent subcarriers fully decorrelated) — i.e. the simulator was
   optimistic in exactly the dimension this objection identifies. Default behaviour is
   unchanged so M0 numbers stay bit-identical.
3. New check `signed-doppler-delay-gradient` runs the strong-LoS channel at the default
   gap, so this cannot silently regress.

Interestingly the **dominant variable is the subcarrier gap, not clutter richness** —
the strong-LoS and 8-path columns differ far less than the δ rows do.

### 3.2 Consequences for the walk test

A single straight walk is the *maximum*-Δτ, maximum-|v·g| best case. The protocol must add:

- **oblique paths** (not just along the link bearing);
- a **tangential** pass (v ⊥ g for some links, so their signed term → 0) — testing graceful
  "no confident reading" rather than confident-wrong;
- at least one **strong-LoS vs cluttered** room contrast, since Δτ visibility is what
  CFCC's LoS limitation is really about.

Otherwise the test validates the easy case, and the M4 cross-room gesture claims (varied v
directions) rest on untested geometry.

## 4. Attribution fix, and one point in our favour

**Attribution fix (mechanism claim).** The note said the cross-subcarrier ratio cancels
"random common phase φ(t), AGC g(t), and (as a constant-offset term) STO". That is right
for two of the three:

- **CFO and RCO** (common across subcarriers, per-packet random) **do** cancel in a
  cross-subcarrier ratio — standard model, Tsinghua *Hands-on Wireless Sensing with Wi-Fi*
  (arXiv 2206.09532) §5.4 Eqs. 30–34; CFO being common across subcarriers is textbook
  (Heiskala & Terry, *OFDM Wireless LANs*).
- **STO/SFO does NOT cancel in a cross-subcarrier ratio.** The STO phase is *linear in
  subcarrier index* (SpotFi, SIGCOMM'15 §3.2.2: a sampling-time offset "results in adding
  −2πf_δ(n−1)τ_s to the phase of the nth subcarrier"), so it is exactly the term that
  *differs* between two subcarriers and survives the ratio. What defuses it is **not the
  ratio** but **per-packet timing re-acquisition**: SpotFi §3.2.1 — "SFO changes the
  sampling time offset from packet to packet … additive noise to the ToF estimates across
  packets". Per-PPDU re-sync makes τ_s zero-mean **jitter, not coherent drift**, so it has
  no consistent winding direction and cannot masquerade as a signed tone. This is why the
  still-room empirical result (ratio phase stable, not winding; jitter ≈1 ns bounded from
  the weak Δ-scaling) is the right evidence — it measures exactly the residual that matters.

  **State it as:** *the ratio cancels the common-mode (CFO/RCO) per-packet phase; the
  subcarrier-dependent STO/SFO residual is defused by per-PPDU re-acquisition (zero-mean
  jitter), which we bound empirically at ≈1 ns RMS.* Then it is fully citable, and the
  silicon-dependence is explicit — re-verify on C5/C6, since a config reporting pre-sync or
  phase-accumulated CSI would reintroduce a coherent tone. The magnitudes confirm the
  concern would be real if it were coherent: 802.11 tolerates ±20 ppm, and an uncorrected
  SFO slope *could* land in 0–50 Hz. The jitter argument is the whole defence, so cite it,
  don't assert it.

**Point in our favour (state it — it is a genuine advantage).** arXiv 2607.20108
("Ambiguity-Resolved Micro-Doppler…") makes explicit that the mirror/opposite-sign
ambiguity in CACC methods is created by the **conjugate-multiplication operation itself**,
not by the clock offset — CACC "introduces mirror counterparts" of near-equal energy that
must be suppressed downstream. **Division (a ratio) avoids that mirror**: it keeps a single
rotating term instead of a pair of counter-rotating ones, at the cost of
denominator/deep-fade amplitude sensitivity — exactly the trade pair selection already
handles. So the ratio primitive is not merely an ESP32 necessity (1×1 ⇒ no antenna pair);
it is *principled*, sidestepping the mirror that the dominant CACC lineage spends effort
suppressing. That is a defensible "why ratio, not conjugate product" paragraph.

## 5. Retrieve before finalising novelty language

Two items the sweep could not open; either could anticipate the claim:

- **"Velocity Estimation Using Single Antenna WiFi Devices," APSIPA 2023** (USTC,
  Dongheng Zhang / Yan Chen group). Directly on single-antenna velocity; host was
  inaccessible — retrieve manually.
- **CFCC full text** (Hu, Wu, J. A. Zhang et al., IEEE TWC, accepted 2025). The closest
  cross-frequency method; confirm whether its cross-frequency correlation is
  mathematically our two-subcarrier ratio winding under another name.

## 6. Recommended novelty framing (survives everything above, pending §5)

> First to recover **signed** Doppler from the **winding direction of a cross-subcarrier
> complex CSI ratio** on a **commodity single-antenna (ESP32) device** — avoiding both the
> ≥2-antenna requirement of CACC/CSI-ratio/MUSIC methods and the special dispersive-antenna
> hardware of WiRainbow — and to fuse such single-antenna signed Doppler across a device
> swarm into a Widar3.0-style body-velocity profile. Relative to WiDFS 3.0 / CFCC (SISO
> signed Doppler, 2025), the contribution is the commodity-hardware ratio-winding front-end
> and the swarm fusion; relative to Widar3.0 (BVP fusion), it is the single-antenna,
> cross-subcarrier signed-Doppler source that removes the 3-antenna node requirement.

State it that way and the claim is honest, bounded and defensible — and if §5 turns up an
exact anticipation, the fusion-from-single-antenna-nodes angle likely still stands.

## 7. Citation set

- SpotFi — Kotaru et al., SIGCOMM 2015, §3.2.1–3.2.2 (STO linear-in-subcarrier; per-packet
  re-sync). https://web.stanford.edu/~skatti/pubs/sigcomm15-spotfi.pdf
- Hands-on Wireless Sensing with Wi-Fi (Tsinghua), arXiv 2206.09532, §5.4 Eqs. 30–34
  (unified CFO/SFO/PDD/RCO phase model). https://tns.thss.tsinghua.edu.cn/wst/docs/sanitization/
- FarSense — Zeng et al., IMWUT 2019, arXiv 1907.03994 (two-ANTENNA ratio cancels offset).
- Uplink CSI-Ratio — arXiv 2211.03250 (ratio cancels TO+CFO, keeps signed Doppler; antenna).
- WiDance — Qian et al., CHI 2017 (CACC, ≥3 antennas). https://zhouzimu.github.io/paper/chi17-qian.pdf
- IndoTrack — Li et al., IMWUT 2017 (Doppler-MUSIC, antenna).
- Widar3.0 — Zheng et al., MobiSys 2019 (BVP fusion, ≥3 links, 3-antenna nodes).
- CARM — Wang et al., MobiCom 2015 (unsigned speed baseline).
- WiDFS 3.0 — arXiv 2508.12614, 2025 (SISO signed Doppler, delay-domain).
- CFCC — Hu, Wu, Zhang et al., IEEE TWC 2025 (cross-frequency, single antenna, LoS-limited).
- WiRainbow — SenSys 2026, arXiv 2511.20671 (single antenna, dispersive FSA hardware).
- Ambiguity-Resolved Micro-Doppler — arXiv 2607.20108 (conjugate multiplication *creates*
  the mirror; a ratio avoids it, at the price of amplitude sensitivity).
- 802.11 ±20 ppm oscillator tolerance — Heiskala & Terry, *OFDM Wireless LANs*; ESP32 datasheet.

*(BSSIDs/MACs: none in this doc — README honesty rule 5.)*
