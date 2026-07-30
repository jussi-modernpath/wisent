#!/usr/bin/env python3
"""Live browser UI for the wisent mesh — walk around and watch the engines respond.

    cd host && python scripts/live_ui.py
    then open http://127.0.0.1:8760

Reads the gateway's cable (protocol v3 relays every node's links down one cable), runs the
real live-path DSP, and serves a self-contained page. **No new dependencies**: the server
is `http.server` from the stdlib and the page is plain canvas/JS, so `requirements.txt`
stays numpy+scipy as CLAUDE.md requires.

WHAT IT SHOWS, and what each panel is honestly worth:
  * per-link motion energy over that link's own rolling floor — the most trustworthy panel,
    a direct measurement with no inversion in between.
  * the VRTI matched-field map and its locate() estimate, WITH the abstention gate live.
    Remember the measured result: at 7 links this engine ran at chance, and at 10 links it
    is unproven. Treat the dot as a hypothesis under test, not a position readout. When it
    abstains, that is the engine working.
  * breathing (fused + per-link peaks) only after enough history, with the inter-link
    disagreement exposed rather than averaged away.

Deliberately NOT shown: signed Doppler / LinkBVP velocity. That channel fails its own
hardware cross-check (~51% self-disagreement, docs/signed-doppler.md) and putting a heading
arrow on screen would imply a confidence the measurements do not support.
"""

import argparse
import json
import pathlib
import re
import sys
import threading
import time
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from wisent.csi_io import FrameParser, merge_ltf_blocks
from wisent.sanitize import sanitize
from wisent.features import motion_energy
from wisent.vrti import VRTI
from wisent.breathing import estimate_bpm

DEFAULT_PORT = "/dev/cu.usbmodem5B901293711"
FALLBACK_NODES = {0: (9.5, 4.5), 1: (9.0, 0.4), 2: (2.0, 0.4), 3: (0.0, 4.0), 4: (4.0, 2.2)}
FALLBACK_ROOM = (10.0, 4.5)


def load_room(path):
    """Node coords + room size from config/room.yaml, without requiring PyYAML.

    A tiny targeted reader rather than a dependency: CLAUDE.md pins the host to
    numpy+scipy. Falls back to the last known geometry and says so.
    """
    nodes, size = {}, None
    try:
        txt = pathlib.Path(path).read_text()
    except OSError:
        return dict(FALLBACK_NODES), FALLBACK_ROOM, f"{path} not found — using fallback"
    m = re.search(r"size_m:\s*\[([\d.]+),\s*([\d.]+)\]", txt)
    if m:
        size = (float(m.group(1)), float(m.group(2)))
    cur = None
    for line in txt.splitlines():
        line = line.split("#")[0]
        nm = re.search(r"node_id:\s*(\d+)", line)
        if nm:
            cur = int(nm.group(1))
        xm = re.search(r"xy_m:\s*\[\s*([\d.+-]+)\s*,\s*([\d.+-]+)\s*\]", line)
        if xm and cur is not None:
            nodes[cur] = (float(xm.group(1)), float(xm.group(2)))
            cur = None
    if not nodes:
        return dict(FALLBACK_NODES), size or FALLBACK_ROOM, "could not parse nodes — fallback"
    return nodes, size or FALLBACK_ROOM, f"{len(nodes)} nodes from {pathlib.Path(path).name}"


class Engine:
    """Rolling per-link buffers + the live DSP. One lock guards `snapshot`."""

    def __init__(self, nodes, room, fs, hist_s=90.0):
        self.nodes, self.room, self.fs = nodes, room, fs
        # Buffers are keyed by DIRECTED link (tx, rx), never by pair: the two directions
        # are measured by different receivers and arrive via different backhaul batches,
        # so pooling them interleaved direction-difference noise into the variance AND
        # doubled the effective sample rate while the DSP assumed 50 Hz. (Found while
        # chasing display latency, 2026-07-30.)
        self.buf = defaultdict(lambda: deque(maxlen=int(fs * 4)))     # (T,S) amp rows
        self.floor_hist = defaultdict(lambda: deque(maxlen=int(hist_s / 0.25)))
        self.energy_hist = defaultdict(lambda: deque(maxlen=120))
        self.rssi = defaultdict(lambda: deque(maxlen=400))
        self.seen_rounds = deque(maxlen=600)
        self.bytes_ = 0
        self.frames = 0
        self.crc = 0
        self.t_start = time.time()
        self.trail = deque(maxlen=40)
        self.peak_hist = deque(maxlen=3)      # persistence gate for the displayed dot
        self.lock = threading.Lock()
        self.snapshot = {"ready": False}
        self.tomo = None
        self.tomo_pairs = []

    def add_frames(self, frames, nbytes, crc):
        self.bytes_ += nbytes
        self.crc = crc
        for f in frames:
            self.frames += 1
            pair = tuple(sorted((f.tx_id, f.node_id)))
            amp = np.abs(f.iq).astype(np.float32)
            self.buf[(f.tx_id, f.node_id)].append(amp)     # directed
            self.rssi[pair].append(f.rssi)
            self.seen_rounds.append(f.seq)

    def _rebuild_tomo(self, pairs):
        if pairs == self.tomo_pairs:
            return
        links = [(np.array(self.nodes[a]), np.array(self.nodes[b]))
                 for a, b in pairs if a in self.nodes and b in self.nodes]
        if len(links) < 3:
            return
        self.tomo = VRTI(links, (0, self.room[0]), (0, self.room[1]), nx=44, ny=20)
        self.tomo_pairs = pairs
        # per-link gain calibration (config/vrti_gains.json, fitted on station2's
        # labelled dwells): LOO-validated 2/5 -> 5/5 quadrants, median 3.05 -> 0.75 m.
        self.gains = None
        try:
            import json as _json
            cal = _json.load(open(pathlib.Path(__file__).resolve().parents[2]
                                  / "config" / "vrti_gains.json"))
            gm = {tuple(q): g for q, g in zip(cal["pairs"], cal["log_gains"])}
            if all(q in gm for q in pairs):
                self.gains = np.array([gm[q] for q in pairs])
                print(f"gain calibration loaded: {len(pairs)} links, "
                      f"gamma {cal['gamma']:.1f} ({cal['provenance']})")
        except Exception as e:
            print(f"gain calibration not loaded: {type(e).__name__}: {e}")

    def step(self):
        fs = self.fs
        # Per DIRECTED link: latest 1-s-window energy over that direction's own floor.
        # Latency budget (was ~2.5-3 s end to end, which at walking speed means the dot
        # trails the walker by metres): 1 s window (evidence age ~0.5 s) + 0.25 s step
        # + 0.75 s persistence + 0.3 s poll ~= 1.5 s worst case now.
        dir_ratio = {}
        for dlink in sorted(self.buf):
            rowsbuf = list(self.buf[dlink])
            if len(rowsbuf) < int(fs * 1.5):
                continue
            w = rowsbuf[0].shape[0]
            rowsbuf = [r for r in rowsbuf if r.shape[0] == w]     # CSI width varies per packet
            if len(rowsbuf) < int(fs * 1.5):
                continue
            amp = merge_ltf_blocks(np.vstack(rowsbuf[-int(fs * 3.0):]))
            try:
                t, E = motion_energy(sanitize(amp, fs, detrend_win_s=1.0), fs,
                                     win_s=1.0, hop_s=0.5)
            except Exception:
                continue
            if len(E) == 0:
                continue
            e = float(E[-1])
            self.floor_hist[dlink].append(e)
            if len(self.floor_hist[dlink]) >= 12:
                fl = float(np.percentile(self.floor_hist[dlink], 15))
                if fl > 0:
                    dir_ratio[dlink] = e / fl

        pairs, energies, floors, rows = [], [], [], []
        seen_pairs = sorted({tuple(sorted(d)) for d in dir_ratio})
        for pair in seen_pairs:
            cands = {d: r for d, r in dir_ratio.items() if tuple(sorted(d)) == pair}
            best_dir, ratio = max(cands.items(), key=lambda kv: kv[1])
            pairs.append(pair); energies.append(ratio); floors.append(1.0)
            rows.append({
                "pair": f"{pair[0]}-{pair[1]}", "a": pair[0], "b": pair[1],
                "ratio": ratio,
                "dir": f"{best_dir[0]}->{best_dir[1]}",
                "rssi": float(np.median(self.rssi[pair])) if self.rssi[pair] else None,
                "n": len(self.rssi[pair]),
            })

        est, heat, why = None, None, "warming up"
        usable = [i for i, f in enumerate(floors) if f and f > 0]
        if self.tomo is not None or len(usable) >= 3:
            self._rebuild_tomo([pairs[i] for i in usable])
        if self.tomo is not None and len(usable) >= 3 and \
                self.tomo_pairs == [pairs[i] for i in usable]:
            y = np.array([energies[i] for i in usable])   # already ratios over own floor
            try:
                # Deliberately NOT tomo.image(): it renormalizes color to the CURRENT
                # frame's max every call, with no evidence gate — so a frame where every
                # link sits at ~1.05x (pure noise) gets painted exactly as saturated-hot
                # as a frame with a real 5x detection. That produced hotspots that jumped
                # every second with nobody in the room (confirmed on a synthetic
                # no-motion test: the "hot" link was noise-driven every step, ratio
                # ~1.05-1.07x) and made a genuine detection visually indistinguishable
                # from that noise. locate()'s score map is gated by the SAME thresholds
                # that decide the dot (min_excess, min_margin) and is all-zero whenever
                # it abstains, so reusing it makes "map lights up" and "dot appears"
                # happen for the same reason, on the same evidence.
                p, score = self.tomo.locate(y, baseline=np.ones(len(y)),
                                            gains=getattr(self, "gains", None))
                hot = sorted([(y[k], self.tomo_pairs[k]) for k in range(len(y))
                              if y[k] >= 2.0], reverse=True)
                hotdesc = ", ".join(f"{a}-{b}={v:.1f}x" for v, (a, b) in hot[:3])
                if p is None:
                    why = "ABSTAINED — no link shows evidence above its floor"
                    heat = None       # don't paint a map the engine itself disowns
                    self.peak_hist.append(None)
                elif len(hot) < 2:
                    # A single hot link cannot localize: its peak is just its own
                    # ellipse, and hopping between lone links IS the side-to-side
                    # jumping observed live (60-sample watch, 2026-07-30: 28 distinct
                    # positions, each driven by whichever link flickered highest).
                    why = (f"single-link evidence ({hotdesc}) — not localizable; "
                           f"waiting for a second link to corroborate")
                    heat = None
                    self.peak_hist.append(None)
                else:
                    self.peak_hist.append((float(p[0]), float(p[1])))
                    pts = [q for q in self.peak_hist if q]
                    stable = (len(pts) == self.peak_hist.maxlen and
                              max(np.hypot(a[0]-b[0], a[1]-b[1])
                                  for a in pts for b in pts) <= 1.5)
                    if stable:
                        est = (float(p[0]), float(p[1]))
                        why = f"estimate from {hotdesc} (unproven engine — see notes)"
                        self.trail.append(est)
                        heat = score
                    else:
                        # Physical movers persist between 0.5 s steps; fade flicker
                        # does not. Show the evidence state, withhold the dot.
                        why = (f"transient peak ({hotdesc}) — not persistent yet; "
                               f"a real mover stabilizes here within ~1.5 s")
                        heat = score
            except Exception as e:
                why = f"vrti error: {type(e).__name__}"

        bpm, conf, linkpk = None, 0.0, []
        if len(self.buf) >= 2:
            cl = []
            for dlink in sorted(self.buf):
                rb = list(self.buf[dlink])
                if len(rb) < int(fs * 3.5):
                    continue
                w = rb[0].shape[0]
                rb = [r for r in rb if r.shape[0] == w]
                if len(rb) < int(fs * 3.5):
                    continue
                try:
                    cl.append(sanitize(merge_ltf_blocks(np.vstack(rb)), fs,
                                       detrend_win_s=8.0))
                except Exception:
                    pass
            if len(cl) >= 2:
                try:
                    bpm, conf, linkpk = estimate_bpm(cl, fs, return_links=True)
                except Exception:
                    pass

        el = max(time.time() - self.t_start, 1e-6)
        uniq = len(set(self.seen_rounds))
        snap = {
            "ready": True,
            "room": list(self.room),
            "nodes": {str(k): list(v) for k, v in self.nodes.items()},
            "links": rows,
            "n_links": len(rows),
            "est": est,
            "why": why,
            "trail": list(self.trail),
            "heat": (np.round(heat, 5).tolist() if heat is not None else None),
            "heat_shape": (list(heat.shape) if heat is not None else None),
            "bpm": bpm, "bpm_conf": conf,
            "link_bpm": [[float(a), float(b)] for a, b in linkpk] if linkpk else [],
            "stats": {
                "kbps": self.bytes_ / el / 1000.0,
                "fps": self.frames / el,
                "crc": self.crc,
                "rounds": uniq,
                "uptime": el,
            },
        }
        with self.lock:
            self.snapshot = snap

    def get(self):
        with self.lock:
            return self.snapshot


PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<title>wisent live</title><style>
:root{--bg:#0e1116;--fg:#e6edf3;--dim:#8b949e;--card:#161b22;--line:#30363d;
      --hot:#ff6b4a;--ok:#3fb950;--warn:#d29922;--acc:#58a6ff}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:13px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace}
header{padding:10px 14px;border-bottom:1px solid var(--line);display:flex;gap:18px;align-items:baseline;flex-wrap:wrap}
h1{font-size:14px;margin:0;letter-spacing:.08em;text-transform:uppercase}
.s{color:var(--dim)} .s b{color:var(--fg);font-weight:600}
main{display:grid;grid-template-columns:minmax(420px,1.45fr) minmax(320px,1fr);gap:14px;padding:14px}
@media(max-width:900px){main{grid-template-columns:1fr}}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px}
.card h2{font-size:11px;margin:0 0 10px;color:var(--dim);letter-spacing:.1em;text-transform:uppercase}
canvas{width:100%;height:auto;display:block;border-radius:6px;background:#0a0d12}
.bar{display:grid;grid-template-columns:44px 1fr 62px;gap:8px;align-items:center;margin:3px 0}
.tr{height:16px;background:#0a0d12;border-radius:3px;overflow:hidden;position:relative}
.fill{height:100%;border-radius:3px;transition:width .25s}
.v{text-align:right;color:var(--dim);font-size:11px}
.note{color:var(--dim);font-size:11px;margin-top:10px;border-top:1px solid var(--line);padding-top:8px}
.big{font-size:22px} .abst{color:var(--warn)} .muted{color:var(--dim)}
table{width:100%;border-collapse:collapse;font-size:11px}
td,th{text-align:right;padding:2px 4px}th{color:var(--dim);font-weight:400}
td:first-child,th:first-child{text-align:left}
</style></head><body>
<header><h1>wisent live</h1>
<div class="s">links <b id="nl">–</b></div><div class="s">rate <b id="fps">–</b>/s</div>
<div class="s"><b id="kbps">–</b> kB/s</div><div class="s">CRC <b id="crc">–</b></div>
<div class="s">rounds <b id="rounds">–</b></div><div class="s" id="conn">connecting…</div></header>
<main>
 <div>
  <div class="card"><h2>Room &amp; VRTI matched field</h2>
   <canvas id="map" width="880" height="420"></canvas>
   <div class="note" id="why">–</div>
   <div class="note">Grey lines = quiet links; orange/red lines = links above 2× their
    floor (labelled with the ratio). A person between two nodes can light one link and sit
    in another's Fresnel null — patchiness is physics, not a fault; the many-link layout
    exists because of it.</div>
   <div class="note">The dot is a <b>hypothesis under test</b>. This engine ran at chance
    with 7 links; 10 links is unproven. An <span class="abst">ABSTAINED</span> readout is
    the abstention gate doing its job, not a fault.</div>
  </div>
 </div>
 <div>
  <div class="card"><h2>Per-link motion energy ÷ own floor</h2><div id="bars"></div>
   <div class="note">The most trustworthy panel: a direct measurement, no inversion.
    Walk near a link and its bar should rise.</div></div>
  <div class="card" style="margin-top:14px"><h2>Breathing</h2>
   <div><span class="big" id="bpm">–</span> <span class="muted" id="bconf"></span></div>
   <div id="lbpm" class="note"></div>
   <div class="note">Per-link peaks are shown <b>unaveraged</b>: scatter means the links
    disagree, which is information. Needs stillness and ~30 s of history.</div></div>
  <div class="card" style="margin-top:14px"><h2>Link health</h2>
   <table id="tbl"><thead><tr><th>pair</th><th>RSSI</th><th>ratio</th><th>frames</th></tr>
   </thead><tbody></tbody></table></div>
 </div>
</main>
<script>
const M={l:46,r:14,t:14,b:30};
function hsl(v){ // 0..1 -> cool to hot
  const h=(1-Math.min(Math.max(v,0),1))*220; return `hsl(${h},85%,${28+30*Math.min(v,1)}%)`;}
function draw(d){
 const c=document.getElementById('map'),g=c.getContext('2d');
 const W=c.width,H=c.height,[rw,rh]=d.room;
 g.clearRect(0,0,W,H); const pw=W-M.l-M.r, ph=H-M.t-M.b;
 const sx=x=>M.l+x/rw*pw, sy=y=>M.t+y/rh*ph; // inverted: y=0 (node0) renders at bottom
 if(d.heat){ // heat is (ny, nx): row index is Y, column index is X (numpy meshgrid order)
  const ny=d.heat_shape[0], nx=d.heat_shape[1]; let mx=0;
  for(const row of d.heat) for(const v of row) if(v>mx) mx=v;
  const cw=pw/nx, chh=ph/ny;
  for(let j=0;j<ny;j++) for(let i=0;i<nx;i++){
    const v=mx>0?d.heat[j][i]/mx:0; if(v<=0.02) continue;
    g.fillStyle=hsl(v); g.globalAlpha=.72;
    g.fillRect(M.l+i*cw, M.t+j*chh, cw+.6, chh+.6);}  // j=0 (y=0) at top, matches sy()
  g.globalAlpha=1;}
 // link lines, colored by live ratio: the raw spatial evidence, no inversion.
 // Sensitivity is an INTERFERENCE PATTERN, not a beam — a person between two nodes can
 // legitimately light one link and sit in another's Fresnel null. These lines let you
 // judge that geometry yourself.
 if(d.links) for(const l of d.links){
  const A=d.nodes[l.a], B=d.nodes[l.b];
  if(!A||!B) continue;
  const rt=l.ratio||1, t=Math.min(Math.max((rt-1)/4,0),1);
  if(rt>=2){ g.strokeStyle=`rgba(255,${Math.round(170-130*t)},60,${0.3+0.6*t})`;
             g.lineWidth=1.5+3.5*t; }
  else     { g.strokeStyle='rgba(120,140,160,0.14)'; g.lineWidth=1; }
  g.beginPath();g.moveTo(sx(A[0]),sy(A[1]));g.lineTo(sx(B[0]),sy(B[1]));g.stroke();
  if(rt>=2){ g.fillStyle='#ffb86b'; g.font='10px ui-monospace';
    g.fillText(rt.toFixed(1)+'x',(sx(A[0])+sx(B[0]))/2+5,(sy(A[1])+sy(B[1]))/2-5); }
 }
 g.strokeStyle='#30363d'; g.lineWidth=1; g.strokeRect(M.l,M.t,pw,ph);
 g.fillStyle='#8b949e'; g.font='10px ui-monospace';
 for(let x=0;x<=rw;x++){g.fillText(x+'m',sx(x)-8,H-10);}
 for(let y=0;y<=rh;y++){g.fillText(y+'',6,sy(y)+3);}
 if(d.trail&&d.trail.length>1){g.strokeStyle='rgba(88,166,255,.45)';g.lineWidth=1.5;g.beginPath();
  d.trail.forEach((p,i)=>i?g.lineTo(sx(p[0]),sy(p[1])):g.moveTo(sx(p[0]),sy(p[1])));g.stroke();}
 for(const [id,p] of Object.entries(d.nodes)){
  g.fillStyle='#161b22';g.strokeStyle='#58a6ff';g.lineWidth=2;
  g.beginPath();g.arc(sx(p[0]),sy(p[1]),11,0,7);g.fill();g.stroke();
  g.fillStyle='#e6edf3';g.font='bold 11px ui-monospace';g.textAlign='center';
  g.fillText(id,sx(p[0]),sy(p[1])+4);g.textAlign='left';}
 if(d.est){const [x,y]=d.est;
  g.strokeStyle='#ff6b4a';g.lineWidth=3;g.beginPath();g.arc(sx(x),sy(y),16,0,7);g.stroke();
  g.fillStyle='#ff6b4a';g.beginPath();g.arc(sx(x),sy(y),5,0,7);g.fill();
  g.font='11px ui-monospace';g.fillText(`${x.toFixed(1)}, ${y.toFixed(1)}`,sx(x)+21,sy(y)+4);}
}
function bars(d){
 const host=document.getElementById('bars');
 const rows=[...d.links].sort((a,b)=>(b.ratio||0)-(a.ratio||0));
 host.innerHTML=rows.map(r=>{
  const rt=r.ratio, pct=rt?Math.min(100,Math.log10(Math.max(rt,1))/Math.log10(30)*100):0;
  const col=!rt?'#30363d':rt>=4?'var(--hot)':rt>=2?'var(--warn)':'var(--ok)';
  return `<div class="bar"><span>${r.pair}</span>
   <span class="tr"><span class="fill" style="width:${pct}%;background:${col}"></span></span>
   <span class="v">${rt?rt.toFixed(1)+'×':'—'}</span></div>`;}).join('');
 const tb=document.querySelector('#tbl tbody');
 tb.innerHTML=rows.map(r=>`<tr><td>${r.pair}</td><td>${r.rssi?r.rssi.toFixed(0):'—'}</td>
  <td>${r.ratio?r.ratio.toFixed(1):'—'}</td><td>${r.n}</td></tr>`).join('');
}
async function tick(){
 try{
  const d=await (await fetch('/state',{cache:'no-store'})).json();
  document.getElementById('conn').textContent='live';
  if(!d.ready){document.getElementById('why').textContent='collecting baseline…';return;}
  document.getElementById('nl').textContent=d.n_links;
  document.getElementById('fps').textContent=d.stats.fps.toFixed(0);
  document.getElementById('kbps').textContent=d.stats.kbps.toFixed(0);
  document.getElementById('crc').textContent=d.stats.crc;
  document.getElementById('rounds').textContent=d.stats.rounds;
  const w=document.getElementById('why');
  w.innerHTML=d.est?d.why:`<span class="abst">${d.why}</span>`;
  draw(d);bars(d);
  document.getElementById('bpm').textContent=d.bpm?d.bpm.toFixed(1)+' bpm':'—';
  document.getElementById('bconf').textContent=d.bpm?`(prominence ${d.bpm_conf.toFixed(1)})`
    :'no confident peak';
  document.getElementById('lbpm').textContent=d.link_bpm.length
    ? 'per-link: '+d.link_bpm.map(p=>p[0].toFixed(1)).join(' · ')+' bpm' : '';
 }catch(e){document.getElementById('conn').textContent='disconnected';}
}
setInterval(tick,300);tick();
</script></body></html>"""


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", default=DEFAULT_PORT, help="gateway serial port")
    ap.add_argument("--baud", default="auto")
    ap.add_argument("--fs", type=float, default=50.0)
    ap.add_argument("--http", type=int, default=8760)
    ap.add_argument("--room", default=str(pathlib.Path(__file__).resolve().parents[2]
                                         / "config" / "room.yaml"))
    args = ap.parse_args()

    nodes, room, src = load_room(args.room)
    print(f"geometry: {src}  room {room[0]}x{room[1]} m")

    from live_capture import resolve_bauds
    baud = resolve_bauds([args.port], args.baud)[0]
    print(f"serial:   {args.port} @ {baud}")

    eng = Engine(nodes, room, args.fs)
    stop = threading.Event()

    def reader():
        import serial
        while not stop.is_set():
            try:
                h = serial.Serial()
                h.port = args.port; h.baudrate = baud; h.timeout = 0.05
                h.dtr = False; h.rts = False
                h.open()
                fp = FrameParser()
                time.sleep(0.3); h.reset_input_buffer()
                while not stop.is_set():
                    chunk = h.read(65536)
                    if chunk:
                        eng.add_frames(fp.feed(chunk), len(chunk), fp.crc_errors)
                h.close()
            except Exception as e:
                print(f"serial: {type(e).__name__}: {e}; retrying in 2 s")
                time.sleep(2.0)

    def dsp():
        while not stop.is_set():
            t0 = time.perf_counter()
            try:
                eng.step()
            except Exception as e:
                print(f"dsp: {type(e).__name__}: {e}")
            time.sleep(max(0.0, 0.25 - (time.perf_counter() - t0)))

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path.startswith("/state"):
                body = json.dumps(eng.get()).encode()
                ct = "application/json"
            else:
                body = PAGE.encode()
                ct = "text/html; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", ct)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    threading.Thread(target=reader, daemon=True).start()
    threading.Thread(target=dsp, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", args.http), H)
    print(f"\n  open  http://127.0.0.1:{args.http}\n  Ctrl-C to stop\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        stop.set()
        print("\nstopped")


if __name__ == "__main__":
    main()
