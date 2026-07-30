#!/usr/bin/env python3
"""Station-protocol ground truth run (roadmap M2, and the observability decider).

The operator is cued from station to station at KNOWN coordinates and steps in place at
each — so the position-vs-time function is known exactly, with no leg-timing ambiguity
(the failure that made the first observability verdict INCONCLUSIVE). One run scores:

  1. OBSERVABILITY — per link: when it was most active, was the operator actually in its
     sensitive region? Decides starvation-vs-front-end, i.e. whether more boards help.
  2. VRTI — locate() with per-link baselines and abstention, scored against each
     station's true position and quadrant (the M2 metric).
  3. A labelled recording for the measured-field dictionary (roadmap backlog).

Stationary stepping, not walking: a walker smears metres of travel across each analysis
window, which blurred the first walk test. Stepping in place puts strong motion energy at
a fixed, known coordinate.

Usage (defaults fit the current deployment — hubs on nodes 0 and 4):
    python scripts/station_test.py --out station.npz
Everyone else (and dogs) out of the room; phone in pocket, not in hand.
"""

import argparse
import json
import pathlib
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from wisent.csi_io import frames_to_segments, merge_ltf_blocks
from wisent.sanitize import sanitize
from wisent.features import motion_energy
from wisent.vrti import VRTI
from wisent.observability import link_observability, summarize
from live_capture import PortRecorder, read_serial, resolve_bauds

# Measured node geometry (config/room.yaml, 2026-07-30). Update if nodes move.
NODES = {0: (9.5, 4.5), 1: (9.0, 0.4), 2: (2.0, 0.4), 3: (0.2, 3.8), 4: (4.0, 2.2)}
DEFAULT_PORTS = ["/dev/cu.usbmodem5B901293711"]   # protocol v3: one gateway cable
# Stations for the 2026-07-30 geometry: one per quadrant + centre + the computed
# least-observable spot (6.2, 4.2). ~0.5 m off nodes so a peak ON a node can't fake it.
DEFAULT_STATIONS = "6.2,4.2 0.7,3.5 2.2,0.9 5.0,2.2 8.0,1.0"


def banner(msg):
    print(f"\n{'=' * 8} {msg} {'=' * 8}", flush=True)


# ---- across-the-room cues: a huge-font browser page + spoken announcements ----------
# The operator walks away from the laptop, so terminal banners are useless mid-run.
PHASE = {"text": "waiting to start", "sub": "", "until": 0.0, "color": "#8b949e",
         "t0": 0.0}
STATION_NAMES = {}          # (x, y) -> human name, filled in main()

CUE_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>station test</title>
<style>body{margin:0;background:#0e1116;color:#e6edf3;font-family:ui-monospace,Menlo,monospace;
display:flex;flex-direction:column;justify-content:center;align-items:center;height:100vh;
text-align:center;overflow:hidden}
#count{font-size:34vh;font-weight:800;line-height:1;margin:0}
#big{font-size:9vh;font-weight:700;margin:2vh 4vw 0}
#sub{font-size:4.5vh;color:#8b949e;margin-top:1vh}</style></head><body>
<div id="count">–</div><div id="big">connecting…</div><div id="sub"></div>
<script>
let off=0;
async function tick(){try{
 const d=await (await fetch('/phase',{cache:'no-store'})).json();
 off=d.now-Date.now()/1000;
 const rem=Math.max(0,d.until-(Date.now()/1000+off));
 document.getElementById('count').textContent=Math.ceil(rem);
 document.getElementById('count').style.color=d.color;
 document.getElementById('big').textContent=d.text;
 document.getElementById('big').style.color=d.color;
 document.getElementById('sub').textContent=d.sub;
}catch(e){document.getElementById('big').textContent='…';}}
setInterval(tick,250);tick();
</script></body></html>"""


def say(text):
    try:
        subprocess.Popen(["say", "-r", "190", text],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass                                   # cues are best-effort, never fatal


def start_cue_server(port=8765):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_GET(self):
            if self.path.startswith("/phase"):
                body = json.dumps({**PHASE, "now": time.time()}).encode()
                ct = "application/json"
            else:
                body = CUE_PAGE.encode(); ct = "text/html; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", ct)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers(); self.wfile.write(body)
    srv = ThreadingHTTPServer(("0.0.0.0", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def capture_with_schedule(ports, baud, stations, walk_s, dwell_s, out_path):
    stop = threading.Event()
    recs, threads = [], []
    bauds = resolve_bauds(ports, baud)
    for i, port in enumerate(ports):
        rec = PortRecorder(port, out_path.with_suffix(f".port{i}.bin"))
        t = threading.Thread(target=read_serial, args=(rec, port, bauds[i], stop),
                             daemon=True)
        recs.append(rec); threads.append(t); t.start()

    schedule = []                      # (t0, t1, kind, x, y)
    def phase(seconds, kind, xy=None, text="", sub="", color="#8b949e", speak=None,
              halfway=None):
        t0 = time.time()
        PHASE.update(text=text, sub=sub, until=t0 + seconds, color=color)
        banner(text + (f" — {sub}" if sub else ""))
        if speak:
            say(speak)
        if halfway and seconds > 12:
            time.sleep(seconds / 2)
            say(halfway)
            time.sleep(seconds - seconds / 2)
        else:
            time.sleep(seconds)
        schedule.append((t0, time.time(), kind,
                         xy[0] if xy else np.nan, xy[1] if xy else np.nan))

    nm = lambda xy: STATION_NAMES.get(xy, "")
    phase(15.0, "settle", text="STAND STILL", sub="at the desk — test starting",
          color="#8b949e", speak="Stand still at the desk. Fifteen seconds.")
    for n, (x, y) in enumerate(stations):
        label = nm((x, y)) or f"({x:.1f}, {y:.1f})"
        phase(walk_s, "walk", (x, y),
              text=f"WALK: {label}", sub=f"station {n + 1} of {len(stations)}",
              color="#3fb950", speak=f"Walk. Go {label}.")
        phase(dwell_s, "station", (x, y),
              text=f"STEP IN PLACE — {label}", sub="keep moving on the spot, swing your arms",
              color="#ff6b4a", speak="Step in place here. Keep moving.",
              halfway="Ten seconds left.")
    phase(10.0, "still", text="STAND STILL", sub="almost done",
          color="#d29922", speak="Done moving. Stand still.")
    PHASE.update(text="DONE — scoring…", sub="come back to the laptop",
                 until=time.time(), color="#58a6ff")
    say("Done. Come back to the laptop.")

    stop.set()
    for t in threads:
        t.join(timeout=2.0)
    for r in recs:
        r.close()
    return recs, schedule


def link_series(recs, fs):
    """{(tx, rx): (wall_times, motion_energy)} pooled across segments.

    Groups frames by RECEIVER first: under protocol v3 the gateway's single cable
    carries every node's frames, so a port is no longer one receiver (the pre-v3
    assumption that crashed the first station2 scoring). Runs the per-block-normalized
    merge — the pipeline the scale-toggle fix lives in.
    """
    from collections import defaultdict
    acc = {}
    for r in recs:
        frames = r.frames()
        if not frames:
            continue
        seq0 = min(f.seq for f in frames)
        # wall time of the earliest round: converts the operator schedule (wall clock)
        # into mesh time. THE TIME AXIS IS THE ROUND COUNTER — never t_us, which is five
        # different nodes' uptime clocks on one v3 cable (that mistake stretched a 165 s
        # recording to a fabricated 431 s), and never per-frame wall, which is arrival
        # time through backhaul batching.
        wall0 = min(x[6] for x in r.rows if int(x[0]) == seq0)
        byrx = defaultdict(list)
        for f in frames:
            byrx[f.node_id].append(f)
        for rx, fl in byrx.items():
            for seg in frames_to_segments(fl):
                if len(seg) < 100:
                    continue
                t, E = motion_energy(sanitize(merge_ltf_blocks(seg.amp), fs), fs,
                                     win_s=2.0, hop_s=0.5)
                if len(E) < 2:
                    continue
                tm = (seg.index[0] - seq0) / fs + t + wall0   # mesh time, wall-anchored
                pr = tuple(sorted(seg.link))
                acc.setdefault(pr, [[], []])
                acc[pr][0].extend(tm); acc[pr][1].extend(E)
    return {p: (np.array(v[0]), np.array(v[1])) for p, v in acc.items()}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ports", nargs="+", default=DEFAULT_PORTS)
    ap.add_argument("--baud", default="auto")
    ap.add_argument("--fs", type=float, default=50.0, help="TDM round rate")
    ap.add_argument("--stations", default=DEFAULT_STATIONS,
                    help='space-separated "x,y" in metres, in visiting order')
    ap.add_argument("--walk-s", type=float, default=8.0)
    ap.add_argument("--dwell-s", type=float, default=20.0)
    ap.add_argument("--room", default="10,4.5")
    ap.add_argument("--out", default="station_test.npz")
    args = ap.parse_args()

    stations = [tuple(float(v) for v in s.split(",")) for s in args.stations.split()]
    W, H_room = (float(v) for v in args.room.split(","))
    out_path = pathlib.Path(args.out)

    # Name every station by its NEAREST NODE — the operator knows where the boards
    # are, not what the station numbers mean. Ties broken by distance; the blind-spot
    # station gets a landmark suffix so "between node 0 and node 4" is unambiguous.
    import math
    for xy in stations:
        ranked = sorted(NODES.items(), key=lambda kv: math.dist(xy, kv[1]))
        n1, d1 = ranked[0][0], math.dist(xy, ranked[0][1])
        if d1 <= 1.2:
            STATION_NAMES[xy] = f"next to node {n1}"
        else:
            n2 = ranked[1][0]
            STATION_NAMES[xy] = f"between node {n1} and node {n2}, nearer node {n1}"

    banner(f"STATION TEST — {len(stations)} stations, hubs on {len(args.ports)} port(s)")
    print("Route: " + " -> ".join(f"({x:.1f},{y:.1f})" for x, y in stations))
    srv = start_cue_server()
    print("\nCUE DISPLAY:  http://127.0.0.1:8765  (opening now — make the window big; "
          "voice cues speak too)\n")
    subprocess.Popen(["open", "http://127.0.0.1:8765"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(4.0)          # let the page open before the schedule starts
    recs, schedule = capture_with_schedule(args.ports, args.baud, stations,
                                           args.walk_s, args.dwell_s, out_path)

    # ---- assemble ----
    fs = args.fs
    S = link_series(recs, fs)
    pairs = sorted(S)
    floors = np.array([np.percentile(S[p][1], 10) for p in pairs])
    tomo = VRTI([(np.array(NODES[a]), np.array(NODES[b])) for a, b in pairs],
                (0, W), (0, H_room), nx=40, ny=20)

    banner("scoring: VRTI at each station")
    q = lambda x, y: (int(x > W / 2), int(y > H_room / 2))
    errs, quad_ok, abstained = [], 0, 0
    for (t0, t1, kind, x, y) in schedule:
        if kind != "station":
            continue
        y_vec = []
        for i, p in enumerate(pairs):
            w, E = S[p]
            sel = (w >= t0 + 2.0) & (w < t1)          # skip the first 2 s of settling in
            y_vec.append(np.median(E[sel]) if sel.sum() >= 3 else floors[i])
        est, _ = tomo.locate(np.array(y_vec), baseline=floors)
        if est is None:
            abstained += 1
            print(f"  station ({x:.1f},{y:.1f}): ABSTAINED")
            continue
        err = float(np.hypot(est[0] - x, est[1] - y))
        errs.append(err)
        ok = q(*est) == q(x, y)
        quad_ok += ok
        print(f"  station ({x:.1f},{y:.1f}): est ({est[0]:.1f},{est[1]:.1f})  "
              f"err {err:.2f} m  quadrant {'OK' if ok else 'WRONG'}")
    n_st = len(stations)
    if errs:
        print(f"  -> median error {np.median(errs):.2f} m | quadrant {quad_ok}/{n_st} "
              f"({100 * quad_ok / n_st:.0f}%; M2 bar is >=80% of windows) | "
              f"abstained {abstained}/{n_st}")

    banner("scoring: per-link observability (exact timings — no assumptions)")
    lo = min(w.min() for w, _ in S.values())
    grid = np.arange(lo, max(w.max() for w, _ in S.values()), 1.0)
    energies = np.column_stack([np.interp(grid, S[p][0], S[p][1]) for p in pairs])
    # exact path: settle/still hold at first/last station edge; walks interp between
    path_xy, bounds = [], []
    moves = [(t0, t1, x, y) for (t0, t1, k, x, y) in schedule if k in ("walk", "station")]
    path_xy = [(moves[0][2], moves[0][3])] + [(m[2], m[3]) for m in moves]
    t_start, t_end = moves[0][0], moves[-1][1]
    bounds = [m[1] for m in moves[:-1]]
    res = link_observability(grid - lo, energies,
                             [(NODES[a], NODES[b]) for a, b in pairs],
                             path_xy, t0=t_start - lo, t1=t_end - lo,
                             leg_bounds=[b - lo for b in bounds])
    for p, r in zip(pairs, res):
        print(f"  link {p}: ratio {r['ratio']:>5.2f}  sig {r['signal_db']:>5.1f} dB  {r['verdict']}")
    good = [i for i, (a, b) in enumerate(pairs) if 2 not in (a, b)]
    print("\n" + summarize(res, good_links=good))

    # ---- archive ----
    payload = {"note": np.str_(
        "Station-protocol ground truth: operator stepped in place at known coordinates, "
        f"schedule script-cued (exact timings). Stations: {args.stations}. "
        "Analysis: VRTI per-station + observability with known leg bounds."),
        "schedule": np.array([(a, b, k, x, y) for a, b, k, x, y in schedule], dtype=object),
        "stations": np.array(stations, dtype=np.float64)}
    for i, r in enumerate(recs):
        payload.update(r.arrays(f"port{i}"))
    np.savez_compressed(out_path, **payload)
    for r in recs:
        pathlib.Path(r.raw_path).unlink(missing_ok=True)
    print(f"\narchived {out_path} ({out_path.stat().st_size / 1e6:.1f} MB) — "
          f"move into recordings/ if this run is worth keeping")


if __name__ == "__main__":
    main()
