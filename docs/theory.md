# Making the RuView Idea Actually Work

## A theory of WiFi human sensing on ESP32-class hardware — what's physically possible, why the popular attempt failed, and an architecture that would survive scrutiny

*Prepared July 2026 · for a 5-node bare-ESP32 kit, Arduino toolchain, scaling path included*

---

## 1. The thesis

RuView did not fail because "WiFi sensing doesn't work." It failed because it promised the three hardest results in the field (through-wall pose, heart rate, camera-free operation out of the box) on hardware that is physically incapable of the methods those results are built on — and then papered over the gap (early versions were caught emitting randomly generated values). The underlying science is real: through-wall *tracking* of a moving person was demonstrated in 2010 with radios far dumber than an ESP32, breathing detection on actual ESP32s was published in 2025 with a mean error of 0.09 breaths/min, and the IEEE ratified a WiFi-sensing standard (802.11bf-2025) last September.

The correct claim is this: **a network of cheap single-antenna nodes cannot see *bodies*, but it can see *disturbance* — and with enough links, disturbance becomes an image.** Every capability you can honestly build sits somewhere on the ladder from disturbance to image, and the theory below explains exactly which rung your hardware buys, why, and how to climb.

---

## 2. The hardware audit: what an ESP32 actually gives you

Everything downstream follows from four facts about the radio, so they come first.

**One antenna, one RF chain.** Every ESP32 variant is a 1×1 device: one spatial stream, no antenna array. This single fact eliminates, at the physics level, the entire family of methods behind the headline results in the literature — the CSI-ratio model (FarSense), phase-difference vitals (PhaseBeat), MUSIC-based multi-person separation, and DensePose-from-WiFi's 3×3 spatial channel. All of those work by dividing or comparing signals from two receive antennas *that share one oscillator*, so the random per-packet phase offsets cancel. With one chain there is nothing to divide by.

**Phase is garbage; amplitude is usable but distorted.** Carrier frequency offset, sampling time offset, and PLL jitter randomize the absolute CSI phase on every packet. Meanwhile, amplitude passes through an automatic gain control stage whose gain is erratic on ESP32 (a 2026 measurement study called the S3's AGC "nearly random" compared to Intel NICs) and isn't even readable on the original ESP32 (it is on S3/C3). The published mitigations that actually work: amplitude-only processing with per-frame L1 normalization (dividing each frame by its mean amplitude recovers most of the cross-device accuracy loss), and cross-*subcarrier* conjugate multiplication, which cancels the common phase error within a single receiver and recovers a usable *differential* phase signal.

**~52 usable subcarriers at 20 MHz, ~108 at 40 MHz.** You get int8 I/Q pairs per subcarrier per packet (12-bit on the ESP32-C5). That is frequency diversity — dozens of slightly different views of the same channel — and it is the resource that replaces the missing spatial diversity. Almost every trick below is a way of spending frequency diversity and *node-count* diversity to buy back what antenna arrays give others.

**~100 Hz sampling, and serial is the bottleneck.** Espressif's reference design transmits ESP-NOW broadcast beacons at 100 packets/s; the radio can capture faster but ASCII-over-UART chokes near 100 Hz with large frames. 100 Hz gives a Nyquist ceiling of 50 Hz for motion dynamics — ample for breathing (0.1–0.5 Hz), gestures (< 10 Hz body Doppler at 2.4 GHz), and walking, but demanding a binary serial protocol, which the official tooling still lacks (open issue #249 on esp-csi).

One more constraint that RuView's architecture ignored: ESP32 clocks are unsynchronized and jittery, so a multi-node system needs an explicit time base. The clean solution is to let the transmit beacon *itself* be the clock — every receiver hears the same broadcast packet within microseconds, so packet sequence numbers give you free cross-node alignment at the sampling resolution that matters.

---

## 3. The physics that works: three load-bearing models

### 3.1 The Fresnel zone model — why breathing is visible and where it isn't

Between a transmitter and receiver, space is layered into concentric ellipsoids (Fresnel zones) whose boundaries differ by half a wavelength of path length — about 2.85 cm of layer spacing at 2.4 GHz. The received signal is the sum of a static path and a body-reflected path, and their interference term goes as cos θ, where θ is the reflected path's phase. A breathing chest moves 4–13 mm, which at these wavelengths swings θ by roughly 60–150° — a large, measurable amplitude modulation.

But the derivative of cos θ is position-dependent: a chest sitting exactly on a zone *boundary* sits at a flat spot of the cosine and produces almost no amplitude change, while a chest mid-zone rides the steep part and produces a strong one. Sensitivity is therefore *periodic in space with a ~λ/2 period* — genuine blind spots every ~3 cm, plus a strong dependence on body orientation (effective chest displacement toward the link drops ~5× when the person turns sideways). This single model explains most "it worked yesterday, it doesn't today" failures in amateur WiFi sensing, RuView's included.

Two escapes from the blind spots exist. The elegant one, the CSI ratio, needs two antennas — closed to us. The available one is **diversity**: the ~52 subcarriers sit at slightly different wavelengths, so their blind spots fall at slightly different places (UbiComp 2016 work detected respiration out to the 48th Fresnel zone this way), and — the key architectural insight — **different TX–RX node pairs have completely different Fresnel geometries.** A chest blind to one link is mid-zone for another. Five nodes give you ten links; the probability that all ten are simultaneously blind is negligible. What one antenna array does with space, a node swarm does with geometry.

### 3.2 Radio tomographic imaging — the honest "see through walls"

This is the theory RuView should have been built on. Patwari and Wilson (2008–2010) showed that if you surround a space with K radio nodes, the K(K−1)/2 links form a measurement mesh, and a body inside the space attenuates or perturbs exactly the links whose Fresnel ellipses it intersects. Discretize the room into voxels, write each link's change as a weighted sum of the voxels along its ellipse, and you get a linear inverse problem: **Δy = W·Δx + n**, where Δy is the vector of link changes and Δx is an *image of the room*. The problem is ill-posed (W has tiny singular values), but Tikhonov regularization with a smoothness prior fixes that, and — crucially — the regularized inverse can be **precomputed once**, making live imaging a single matrix–vector multiply. Real-time tomography on a laptop, no ML.

The through-wall version is even more robust: walls attenuate but don't *change*, so instead of mean signal you image the windowed **variance** of each link (VRTI) — only moving things light up. With 34 nodes around a real building, Wilson and Patwari tracked a person through exterior walls with ~0.9 m average error, in 2010, using radios that only reported RSSI. In 2025, a *Scientific Reports* paper reproduced the approach on exactly your hardware class — 14 ESP32 nodes around a 6×8 m room, RSSI-based RTI images fed to a small CNN, 92.5% localization accuracy. This is the peer-reviewed existence proof that the "RuView idea," correctly formulated, works on ESP32s.

The scaling law is the strategic point: links grow as O(K²). Five nodes → 10 links → coarse zone-level presence ("someone is in the NE quadrant, moving"). Ten nodes → 45 links → genuine blob imaging. Twenty nodes → 190 links → approaching the published sub-meter tracking results. Node count is the knob that antenna count is for everyone else, and ESP32s cost $5.

### 3.3 Doppler and the domain-shift problem — why demos die in a new room

The third pillar explains WiFi sensing's dirtiest secret: models trained in one room collapse in another (DensePose-from-WiFi's own paper shows average precision falling from 43.5 to 27.3 on an unseen layout; AdaPose reports >45% drops). CSI entangles the body's motion with the room's entire multipath fingerprint, and a learned model happily memorizes the room.

The strongest published cure, Widar3.0, is instructive for us: it derives a **body-coordinate velocity profile (BVP)** — the distribution of the body's actual velocity components — by fusing Doppler spectra from *multiple receivers at different bearings* via a compressed-sensing inversion. Because velocity-of-the-body is a physical quantity, not a room fingerprint, recognition transfers across environments with ~92% accuracy *with zero retraining*. The requirement: at least three receivers at diverse angles. A five-node kit is, again, exactly the right shape — one transmitter, four receivers at four bearings.

On single-antenna hardware you can't get clean per-link Doppler from phase, but the cross-subcarrier conjugate trick plus amplitude spectrograms yields a serviceable pseudo-Doppler per link; fusing four of them into a coarse BVP is the most theoretically-grounded route to *room-independent* gesture and activity sensing on ESP32s. This, to my knowledge, has not been published on this hardware class — it's the genuinely novel corner of the proposal.

---

## 4. The architecture: a five-layer system for a five-node kit

**Layer 0 — radio discipline.** One node is the beacon: ESP-NOW broadcast at 100 Hz, fixed rate MCS0, fixed channel, TX power locked. Four nodes are listeners with CSI callbacks filtered to the beacon's MAC, channel filtering disabled (subcarrier independence matters more than smoothing), HT40 for 108 subcarriers. Beacon sequence numbers are the shared clock. Listeners stream *binary* frames (header + int8 I/Q + sequence + RSSI) over UART or, better, batch them over UDP to the host — WiFi backhaul on a second interface isn't available, so interleave: CSI capture on the sensing channel, periodic bursts to the host AP. (Simplest reliable v1: all four listeners on USB into one hub; wireless backhaul is a v2 refinement.)

**Layer 1 — sanitization (per node, on host).** Hampel filter for impulse outliers → per-frame L1 amplitude normalization (kills AGC) → resample onto the beacon-sequence time base → per-subcarrier detrend. Keep two parallel products: normalized amplitude tensors (subcarrier × time), and cross-subcarrier conjugate products for differential phase. Everything above 100 lines of NumPy is unnecessary here; this layer is boring on purpose, and it is exactly the layer RuView got wrong.

**Layer 2 — the link-feature bus.** Reduce each link to a small standard feature vector per 100 ms window: motion energy (windowed variance of the top-k variance subcarriers), spectral band powers (0.1–0.6 Hz respiration band, 0.5–2 Hz cardiac-adjacent band, 2–50 Hz motion band), a pseudo-Doppler spectrogram column, and mean RSSI. Every downstream capability consumes this bus, which means every capability shares one calibration and one data recorder.

**Layer 3 — the physics engines (no ML).**
- *VRTI imaging:* stack the four links' motion-energy values (plus beacon→host RSSI links if the host has a WiFi card), multiply by the precomputed regularized inverse, render a live room heatmap. With 5 nodes this is honestly labeled a "disturbance map," not an image — quadrant-level localization of a moving person, including through an interior wall.
- *Respiration engine:* subcarrier-diversity selection (top-5 variance in the respiration band), multi-link voting, Welch periodogram peak → breaths/min. Published ESP32 baselines to beat: COVID-Beat's 91.8–99.6% accuracy, PulseFi's 0.09 bpm MAE (with an LSTM). Constraints inherited from the physics, stated up front: one subject, roughly still, within a few meters, orientation matters.
- *Fusion logic:* respiration only reported when the motion engine says "still"; motion map only trusted when links are calibrated (30 s empty-room baseline on startup).

**Layer 4 — learned heads (optional, honest).** Only after Layers 0–3 are solid: a small CNN on pseudo-BVP features for a 5-gesture vocabulary, trained with a webcam-teacher rig (synchronized video labeled by an off-the-shelf pose model — the standard cross-modal distillation recipe) and evaluated *only* cross-room. AdaPose-style few-shot calibration (a couple of minutes of data in a new room) is the realistic deployment story; anyone claiming zero-calibration learned sensing on this hardware is ahead of the literature.

---

## 5. The capability ladder, with confidence levels

**Near-certain (physics + published ESP32 replication):** presence and motion detection; through-interior-wall *motion* detection; quadrant-level disturbance mapping with 5 nodes; single-subject respiration rate under stated geometry constraints.

**Probable (published on adjacent hardware, engineering risk on ESP32):** sub-room localization of a walking person (needs ~10 nodes for comfort); breathing *waveform* (not just rate); 5-gesture recognition in-room.

**Research-grade (novel, worth attempting, might fail):** cross-room-transferable gestures via 4-link pseudo-BVP; two-person respiration separation via link geometry (in the literature this needs antenna arrays and ICA; link diversity *might* substitute — unproven).

**Out of reach on this hardware, say so publicly:** skeletal pose (needs 3×3 MIMO + camera-teacher training and still halves its accuracy in unseen rooms); heart rate as a headline claim (0.2–0.5 mm chest displacement — PulseFi reports 0.5 bpm MAE with an LSTM in favorable LOS conditions, so "experimental" at best, and buried under breathing harmonics); identifying *who* a person is; reliable through-exterior-wall vitals.

## 6. The upgrade path

The 5-node bare-ESP32 kit is the proving ground; three upgrades change the theory ceiling. **ESP32-C5/C6** (Espressif's own sensing ranking puts C5 > C6 > C3 ≈ S3 > ESP32) add WiFi-6 HE-LTF CSI and 12-bit I/Q. **Phase coherence retrofits** — the 2025 esp-ppb project disciplines ESP32-C3 nodes to <10° inter-node phase via a VCTCXO and over-the-air FTM, and ESPARGOS builds phase-coherent ESP32 arrays — would unlock true AoA and array processing on $10 nodes, at which point FarSense/PhaseBeat-class methods stop being closed to you. And **802.11bf-2025** is now a ratified standard: no consumer silicon implements it yet as of mid-2026, but it means purpose-built sensing primitives are coming to commodity WiFi, and an architecture organized around the link-feature bus can adopt them without redesign.

## 7. The anti-RuView evaluation protocol

The reproducibility failure is a solvable design problem, and solving it visibly *is the differentiator*. Ship with: (1) a hard rule that no synthetic data can flow through the live path — the demo pipeline physically has no simulation mode; (2) a one-command recorder that captures raw CSI + a webcam ground-truth track to a sharable file, so any claim comes with its evidence; (3) pre-registered acceptance tests (e.g. "empty room: < 2% false motion over 1 h; walk test: quadrant correct ≥ 80%; respiration: within ±1 bpm of a chest-strap for 5 min, subject at 2 m facing the link"); (4) all results reported per-room, with at least one room the code has never seen; (5) published failure conditions next to every capability. A README that says "heart rate: not supported, here's the physics of why" will do more for credibility in 2026 than any demo video — because the most-starred project in this space just taught everyone what the absence of that sentence means.

---

## 8. Summary in one paragraph

Trade the missing antenna array for three kinds of diversity you *can* have — frequency (52–108 subcarriers), geometry (O(K²) links from K cheap nodes), and time (100 Hz beacon-disciplined sampling) — then let physics-first engines (variance tomography, Fresnel-aware respiration) do the headline work and confine machine learning to a small, camera-taught, cross-room-evaluated gesture head. Five nodes gets a working disturbance map and a trustworthy breathing monitor; twenty nodes gets you honest through-wall tracking; no number of these nodes gets you DensePose, and saying so out loud is what will make the project the one that finally *works*.

---

## Key sources

- Wilson & Patwari, *Radio Tomographic Imaging with Wireless Networks*, IEEE TMC 2010 — https://span.ece.utah.edu/uploads/RTI_version_3.pdf
- Wilson & Patwari, *Through-Wall Tracking Using Variance-Based Radio Tomography*, arXiv:0909.5417
- *Passive localization based on radio tomography images with CNN utilizing WiFi RSSI* (14 ESP32 nodes), Scientific Reports 2025 — https://www.nature.com/articles/s41598-025-99694-2
- Wang et al., *Human respiration detection with commodity WiFi: do location and orientation matter?*, UbiComp 2016 (Fresnel model)
- Zeng et al., *FarSense* (CSI-ratio model), IMWUT 2019 — arXiv:1907.03994
- Zheng et al., *Widar3.0* (body-coordinate velocity profiles), MobiSys 2019 / TPAMI 2022
- Geng, Huang, De la Torre, *DensePose From WiFi*, arXiv:2301.00250 (hardware & cross-domain numbers)
- AdaPose few-shot domain adaptation — arXiv:2309.16964
- *PulseFi* — ESP32 breathing 0.09 bpm MAE / HR 0.50 bpm MAE, arXiv:2510.24744 (2025)
- *COVID-Beat*, J. Comput. Design & Eng. 2022 — ESP32 respiration pipeline
- PhaseBeat (phase-difference vitals; why 2 antennas), ACM Health 2020
- Espressif esp-csi (official toolchain, 100 Hz ESP-NOW reference) — https://github.com/espressif/esp-csi
- *Same Signal, Different Story* (ESP32 AGC characterization, L1-norm fix), arXiv 2026
- esp-ppb phase-coherent ESP32 nodes — https://github.com/jonathanmuller/esp-ppb ; ESPARGOS — https://espargos.net
- IEEE 802.11bf-2025, published Sept 26 2025 — https://standards.ieee.org/ieee/802.11bf/11574/
- RuView reproducibility record — https://agentpedia.codes/blog/ruview-guide ; CNX Software, Mar 2026
