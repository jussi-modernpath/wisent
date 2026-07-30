# Recordings

Honesty rule 2: every claim ships with the evidence it was derived from. These back the
M1 numbers in `docs/roadmap.md` and the null controls in `docs/signed-doppler.md`.

Each `.npz` holds the **untouched wire bytes** (`port0/raw`) plus wall-clock anchors and
the decoded frame table. The raw stream is the source of truth — everything else can be
re-derived:

```python
import numpy as np
from wisent.csi_io import FrameParser, frames_to_segments
z = np.load("recordings/seqlock.npz")
segments = frames_to_segments(FrameParser().feed(z["port0/raw"].tobytes()))
```

| file | what it is | what it proves |
|---|---|---|
| `seqlock.npz` | 120 s, 1 beacon (MCS0-LGI) + 1 listener | The system clock. 14064 frames, 0 CRC errors, one gap-free **12053-row segment on the beacon seq grid**. Empty-room `estimate_bpm` correctly declines (2.35 < gate 3.0). |
| `twonode10min.npz` | 10 min, 2 MAC-filtered listeners ~1 m apart | The M1 acceptance numbers on 2 of the required 4 listeners: 117k frames, CRC 0.0000%, 0 gaps, **48082 rows cross-node aligned**. Also holds the 10 wild-seq frames (~0.01%) behind the `rx_state` fix. |
| `twonode.npz` | 120 s, 2 listeners, boards adjacent | First cross-node capture; motion-energy correlation r=0.557. A person was at the desk (burst 50–90 s) — the motion gate localizing it. |
| `spread1m.npz` | 180 s, nodes ~1 m apart, MAC filter **off** | Reception 99.7%/99.9% at realistic RSSI. Retained *with* its ~9 bogus-seq frames: it is the evidence for the 4-byte-header fix. |
| `first_light.npz`, `soak10min.npz` | 60 s / 600 s, **no wisent beacon** — ambient AP traffic only | The radio, framing, link and recorder work. `seq` is `0xFFFFFFFF` throughout (degraded `t_us` base) and CSI width is mixed 64/128. Nothing more. |
| `walk_null_fail1.npz`, `walk_null_pass.npz` | signed-Doppler null controls, nobody walking | The first (4/4 phantom signed readings, a ~36 Hz fan line) and the last (0/4, PASS) of a 4-run series. The failures are why `ratios.py` has three gates — see `docs/signed-doppler.md`. |

| `walk_vrti.npz` | 237 s, 5 nodes, 7 links, operator walking 3→2→1→0 | The first real VRTI evaluation, and a clean **negative**: `locate()` tracked the path at chance (x-monotonicity 55%). Node 2's links moved 1.3–2.9× even with a person standing at node 2. |

## Read these honestly

- **No capture here demonstrates VRTI, LinkBVP, or breathing.** VRTI needs nodes
  surrounding a space; every capture has them on one desk ≤ 1 m apart, which is not link
  geometry. The breathing gates behaving correctly on an empty room is a true negative,
  not a sensing result.
- Reception figures were taken at −11 dBm (adjacent) and ~1 m, **not** the 3 m LOS the
  acceptance test specifies.
- `twonode.npz` is **not** a valid breathing recording: a person was present, both links
  report a confident sub-Hz periodicity that *disagrees* between links (28.8 vs 34.0 bpm),
  and there is no ground truth. Deciding what that signal is, is M3's job.
- Notes were scrubbed of the test network's SSID before archiving; third-party
  BSSIDs/MACs are anonymised anywhere they appear (README honesty rule 5).
