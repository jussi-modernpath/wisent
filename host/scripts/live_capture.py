#!/usr/bin/env python3
"""Multi-port CSI recorder (roadmap M1 item 3).

One reader thread per listener USB port -> raw bytes to disk as they arrive ->
a single .npz holding the raw wire stream, wall-clock anchors, and decoded frame
tables. The raw stream is the point: honesty rule 2 says every claim ships with the
evidence it was derived from, and only the untouched bytes can be re-derived from.

  # record 4 listeners for 10 minutes (M1 acceptance run)
  python scripts/live_capture.py --ports /dev/cu.usbserial-* --duration 600 --out rec.npz

  # re-run the exact same decode path over bytes captured earlier (no boards needed)
  python scripts/live_capture.py --replay rec.node1.bin --out replay.npz

Live capture needs pyserial (`pip install pyserial`); it is imported lazily so the
rest of the toolkit stays numpy+scipy only. This script NEVER imports wisent.sim —
enforced by scripts/validate_synthetic.py.
"""

import argparse
import pathlib
import sys
import threading
import time

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from wisent.csi_io import (CsiFrame, FrameParser, SEQ_UNAVAILABLE,
                           frames_to_segments, align_segments)

GAP_WARN_S = 1.0   # M1 acceptance: "no gaps > 1 s"


class PortRecorder:
    """Reads one port, mirrors every byte to disk, decodes frames as they arrive."""

    def __init__(self, name: str, raw_path: pathlib.Path):
        self.name = name
        self.raw_path = raw_path
        self.parser = FrameParser()
        self.raw = open(raw_path, "wb")
        self.anchors = []          # (byte_offset, wall_clock) per read chunk
        self.bytes_read = 0
        self.rows = []             # (seq, t_us, rssi, channel, flags, node_id, wall)
        self.iq = []
        self.gaps = []             # (wall_clock_before, wall_clock_after)
        self.t_last_frame = None
        self.error = None

    def consume(self, chunk: bytes, now: float):
        if not chunk:
            return
        self.raw.write(chunk)
        self.anchors.append((self.bytes_read, now))
        self.bytes_read += len(chunk)
        for f in self.parser.feed(chunk):
            if self.t_last_frame is not None and now - self.t_last_frame > GAP_WARN_S:
                self.gaps.append((self.t_last_frame, now))
            self.t_last_frame = now
            self.rows.append((f.seq, f.t_us, f.rssi, f.channel, f.flags, f.node_id, now,
                              f.tx_id))
            self.iq.append(f.iq)

    def close(self):
        self.raw.close()

    # -- reporting -----------------------------------------------------------
    @property
    def node_ids(self):
        return sorted({r[5] for r in self.rows})

    @property
    def links(self):
        """(tx_id, rx_id) pairs seen — under TDM one port carries several links."""
        return sorted({(int(r[7]), int(r[5])) for r in self.rows})

    def frames(self):
        """Decoded frames, rebuilt for segmenting."""
        return [CsiFrame(node_id=r[5], seq=r[0], t_us=r[1], rssi=r[2],
                         channel=r[3], flags=r[4], iq=iq, tx_id=int(r[7]))
                for r, iq in zip(self.rows, self.iq)]

    def arrays(self, prefix: str):
        """npz payload for this port."""
        rows = np.array(self.rows, dtype=np.float64) if self.rows else np.zeros((0, 8))
        out = {
            f"{prefix}/raw": np.fromfile(self.raw_path, dtype=np.uint8),
            f"{prefix}/raw_anchors": np.array(self.anchors, dtype=np.float64).reshape(-1, 2),
            f"{prefix}/seq": rows[:, 0].astype(np.int64),
            f"{prefix}/t_us": rows[:, 1].astype(np.int64),
            f"{prefix}/rssi": rows[:, 2].astype(np.int16),
            f"{prefix}/channel": rows[:, 3].astype(np.uint8),
            f"{prefix}/flags": rows[:, 4].astype(np.uint8),
            f"{prefix}/node_id": rows[:, 5].astype(np.uint8),
            f"{prefix}/wall_clock": rows[:, 6],
            f"{prefix}/tx_id": rows[:, 7].astype(np.uint8),
            f"{prefix}/gaps": np.array(self.gaps, dtype=np.float64).reshape(-1, 2),
            f"{prefix}/crc_errors": np.int64(self.parser.crc_errors),
            f"{prefix}/bad_headers": np.int64(self.parser.bad_headers),
            f"{prefix}/resync_bytes": np.int64(self.parser.resync_bytes),
            f"{prefix}/frames_ok": np.int64(self.parser.frames_ok),
        }
        # I/Q only stacks when every frame has the same width; otherwise the raw
        # stream remains the source of truth rather than a padded fiction.
        widths = {len(x) for x in self.iq}
        if len(widths) == 1:
            out[f"{prefix}/iq"] = np.stack(self.iq).astype(np.complex64)
        return out


BAUD_CANDIDATES = (4000000, 921600)


def detect_baud(port: str, candidates=BAUD_CANDIDATES, probe_s: float = 1.2):
    """-> (baud, n_frames): the baud at which this port yields parseable wisent frames.

    The fleet is deliberately mixed — protocol-v3 nodes run 4 Mbaud (needed to carry
    relayed backhaul traffic), older v2 nodes run 921600. Guessing wrong produces a silent
    stream of garbage that looks exactly like a dead board, so probe rather than assume.
    """
    import serial as _serial
    best, best_n = None, 0
    for b in candidates:
        try:
            h = _serial.Serial()
            h.port = port
            h.baudrate = b
            h.timeout = 0.05
            h.dtr = False          # DTR/RTS low: asserted, they hold the board in the
            h.rts = False          # ROM bootloader and it stays mute (hardware note)
            h.open()
        except Exception:
            continue
        time.sleep(0.4)
        h.reset_input_buffer()
        t0 = time.perf_counter()
        buf = b""
        while time.perf_counter() - t0 < probe_s:
            buf += h.read(65536)
        h.close()
        n = len(FrameParser().feed(buf))
        if n > best_n:
            best, best_n = b, n
    return best, best_n


def resolve_bauds(ports, baud):
    """One baud per port. `baud="auto"` probes each; anything else is used verbatim."""
    if baud != "auto":
        return [int(baud)] * len(ports)
    out = []
    for port in ports:
        b, n = detect_baud(port)
        if b is None:
            print(f"  {port}: no parseable frames at any known baud — check the board")
            b = BAUD_CANDIDATES[0]
        else:
            print(f"  {port}: {b} baud ({n} frames in probe)")
        out.append(b)
    return out


def read_serial(rec: PortRecorder, port: str, baud: int, stop: threading.Event):
    try:
        import serial  # lazy: only live capture needs pyserial
    except ImportError:
        rec.error = ("pyserial not installed — `pip install pyserial` "
                     "(needed only for live capture)")
        stop.set()
        return
    try:
        # Open with DTR/RTS held low. On ESP32 boards those lines are wired to the
        # auto-reset circuit (EN/BOOT); asserting them on open reboots the listener,
        # or drops it into the ROM bootloader where it goes silent. Measured on an
        # ESP32-S3: asserting DTR mid-capture stops the stream dead.
        ser = serial.Serial()
        ser.port, ser.baudrate, ser.timeout = port, baud, 0.1
        ser.dtr = False
        ser.rts = False
        ser.open()
        with ser:
            while not stop.is_set():
                chunk = ser.read(4096)
                rec.consume(chunk, time.time())
    except Exception as e:                       # noqa: BLE001 — report, never crash the run
        rec.error = f"{type(e).__name__}: {e}"
        stop.set()


def capture(ports, baud, duration, out_path):
    stop = threading.Event()
    recs, threads = [], []
    bauds = resolve_bauds(ports, baud)
    for i, port in enumerate(ports):
        rec = PortRecorder(port, out_path.with_suffix(f".port{i}.bin"))
        t = threading.Thread(target=read_serial, args=(rec, port, bauds[i], stop),
                             daemon=True)
        recs.append(rec)
        threads.append(t)
        t.start()

    t0 = time.time()
    try:
        while not stop.is_set() and time.time() - t0 < duration:
            time.sleep(1.0)
            print(f"  t={time.time() - t0:6.1f}s  " + "  ".join(
                f"[{pathlib.Path(r.name).name}] {r.parser.frames_ok:6d} fr "
                f"crc {r.parser.crc_error_rate:.3%} gaps {len(r.gaps)}" for r in recs))
    except KeyboardInterrupt:
        print("\n  interrupted — writing what was captured")
    stop.set()
    for t in threads:
        t.join(timeout=2.0)
    for r in recs:
        r.close()
    return recs, time.time() - t0


def replay(paths, out_path):
    """Feed previously recorded raw bytes through the identical decode path.

    Marked source="replay" in the output so a replayed file can never be mistaken
    for a live recording.
    """
    recs = []
    for i, p in enumerate(paths):
        path = pathlib.Path(p)
        rec = PortRecorder(str(path), out_path.with_suffix(f".replay{i}.bin"))
        data = path.read_bytes()
        now = time.time()
        for off in range(0, len(data), 4096):   # same chunking a serial read would give
            rec.consume(data[off:off + 4096], now + off / 4096 * 0.04)
        rec.close()
        recs.append(rec)
    return recs, 0.0


def summarize(recs, wall_s, source):
    """Print the M1 acceptance numbers. No verdict is printed for a replay."""
    print(f"\n--- capture summary ({source}) ---")
    total_frames = sum(r.parser.frames_ok for r in recs)
    total_crc = sum(r.parser.crc_errors for r in recs)
    segments = []
    for r in recs:
        if r.error:
            print(f"  {r.name}: ERROR {r.error}")
            continue
        nodes = r.node_ids
        rate = f"{r.parser.frames_ok / wall_s:.1f}/s" if wall_s > 0 else "rate n/a"
        no_seq = sum(1 for row in r.rows if row[0] == SEQ_UNAVAILABLE)
        print(f"  {pathlib.Path(r.name).name}: node(s) {nodes} links {r.links}, "
              f"{r.parser.frames_ok} frames "
              f"({rate}), CRC err {r.parser.crc_error_rate:.4%}, "
              f"gaps>{GAP_WARN_S}s: {len(r.gaps)}, no-seq frames: {no_seq}")
        all_frames = r.frames()
        # Report per LINK (tx_id, node_id). Keeping only the longest segment per receiver
        # meant a healthy-looking summary could describe one arbitrary transmitter while
        # every other link on that port went unchecked — under TDM each port carries
        # (K-1) links.
        for link in r.links:
            tx, node = link
            segs = frames_to_segments([f for f in all_frames
                                       if f.node_id == node and f.tx_id == tx])
            if not segs:
                print(f"      link {tx}->{node}: no usable segment")
                continue
            best = max(segs, key=len)
            total = sum(len(s) for s in segs)
            print(f"      link {tx}->{node}: {len(segs)} gap-free segment(s), "
                  f"{total} rows total, longest {len(best)} on the {best.timebase} base")
            segments.append(best)

    seq_segs = [s for s in segments if s.timebase == "seq"]
    if len(seq_segs) > 1:
        index, aligned = align_segments(seq_segs)
        print(f"  cross-node: {len(index)} rows aligned on beacon seq "
              f"across nodes {sorted(aligned)}")
    elif segments:
        print("  cross-node: not attempted — need >=2 nodes with beacon seq "
              "(rebuild listener.ino with WISENT_TRY_PAYLOAD_SEQ if seq is missing)")

    if source != "live":
        return
    # Only a run that actually captured something gets acceptance numbers. A summary
    # that looks green on an empty capture is the failure mode this project exists to
    # avoid, so say plainly that there is nothing to judge.
    if any(r.error for r in recs) or total_frames == 0:
        print("\n  M1 acceptance: NOT EVALUATED — "
              + ("a port errored out" if any(r.error for r in recs)
                 else "no frames were captured")
              + ". Nothing here is evidence of anything.")
        return
    crc_rate = total_crc / (total_frames + total_crc)
    gaps = sum(len(r.gaps) for r in recs)
    verdict = ("PASS" if crc_rate < 0.001 and gaps == 0 and wall_s >= 600
               else "not yet")
    print(f"\n  M1 acceptance [{verdict}]: CRC {crc_rate:.4%} (target <0.1%) | "
          f"gaps>{GAP_WARN_S}s {gaps} (target 0) | "
          f"{wall_s / 60:.1f} min recorded (target >=10 min)")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ports", nargs="+", help="serial ports, one per listener")
    ap.add_argument("--replay", nargs="+", help="raw .bin captures to re-decode")
    ap.add_argument("--baud", default="auto",
                    help="bits/s, or 'auto' to probe (v3 nodes 4000000, v2 921600)")
    ap.add_argument("--duration", type=float, default=600.0, help="seconds")
    ap.add_argument("--out", default="capture.npz")
    ap.add_argument("--note", default="", help="free-text ground truth, e.g. "
                    "'empty room' or 'subject seated 2 m, facing link 1-2'")
    ap.add_argument("--keep-raw", action="store_true",
                    help="keep the sidecar .bin files after embedding them in the npz")
    args = ap.parse_args()

    if bool(args.ports) == bool(args.replay):
        ap.error("give exactly one of --ports (live) or --replay (recorded bytes)")

    out_path = pathlib.Path(args.out)
    if args.ports:
        print(f"recording {len(args.ports)} port(s) at {args.baud} baud "
              f"for {args.duration:.0f}s -> {out_path}")
        recs, wall_s = capture(args.ports, args.baud, args.duration, out_path)
        source = "live"
    else:
        recs, wall_s = replay(args.replay, out_path)
        source = "replay"

    payload = {
        "source": np.str_(source),
        "note": np.str_(args.note),
        "started_unix": np.float64(time.time() - wall_s),
        "wall_seconds": np.float64(wall_s),
        "n_ports": np.int64(len(recs)),
        "ports": np.array([r.name for r in recs], dtype=np.str_),
    }
    for i, r in enumerate(recs):
        payload.update(r.arrays(f"port{i}"))
    np.savez_compressed(out_path, **payload)
    print(f"\nwrote {out_path} ({out_path.stat().st_size / 1e6:.1f} MB)")

    if not args.keep_raw:
        for r in recs:
            pathlib.Path(r.raw_path).unlink(missing_ok=True)

    summarize(recs, wall_s, source)
    return 1 if any(r.error for r in recs) else 0


if __name__ == "__main__":
    sys.exit(main())
