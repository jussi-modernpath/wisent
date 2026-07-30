#!/usr/bin/env python3
"""Signed-Doppler ground-truth walk test (docs/signed-doppler.md, next step 3).

Pre-registered protocol: a person stands ~3 m from the beacon/listener pair, walks
STRAIGHT TOWARD the boards on cue, stops, walks straight back, repeated N times. Each
walking segment is labeled by the script's own schedule; per segment the ratio channel
produces a signed Doppler estimate, and the pre-registered acceptance is:

    sign correct >= 90% of CONFIDENT segments, in BOTH directions.

A negative result is a result — it gets written up either way. Raw frames + the label
schedule are archived in the output npz (honesty rule 2).

INCOMPLETE BY DESIGN, and it must not be quoted as full validation. A straight
toward/away walk is the EASIEST case: maximum |v.g| and maximum delay difference dtau.
docs/prior-art-signed-doppler.md 3.2 requires three more conditions before the M4
cross-room claims rest on anything: (i) oblique paths, (ii) a TANGENTIAL pass where
v is perpendicular to g for some links, so their signed term vanishes and the correct
answer is "no confident reading" rather than a confident wrong sign, and (iii) a
strong-LoS vs cluttered room contrast, since delay-difference visibility is what limits
the published single-antenna cross-frequency method. Passing this script validates the
best case only.

Usage:
  python scripts/walk_test.py --port /dev/cu.usbmodemXXXX --passes 4 --out walk.npz
  python scripts/walk_test.py --port ... --passes 2 --null   # nobody walks: every
                                       # segment should yield NO confident reading
"""

import argparse
import pathlib
import sys
import time

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from wisent.csi_io import (FrameParser, FLAG_SEQ_FROM_PAYLOAD,
                           frames_to_segments)
from wisent.ratios import (link_signed_doppler, corroborated_signed_doppler,
                           spectral_lines)

# Sample rate is DERIVED, never assumed. It was hardcoded to 100 Hz for the star
# topology; TDM rounds run at 50 Hz, and a 2x rate error doubles every inferred Doppler
# frequency — silently, since nothing else changes. Overridable with --fs.
DEFAULT_FS = 50.0


def banner(msg):
    print(f"\n{'=' * 8} {msg} {'=' * 8}", flush=True)


def collect(ser, parser, frames, until):
    """Read the port until wall-clock `until`, appending (frame, wall) pairs."""
    while time.time() < until:
        chunk = ser.read(4096)
        now = time.time()
        for f in parser.feed(chunk):
            frames.append((f, now))


def segment_matrix(frames, t0, t1, fs):
    """Complex CSI for locked frames in [t0, t1), via the TRUSTED time base.

    Delegates to csi_io.frames_to_segments(keep_complex=True) rather than re-implementing
    gridding: that function already dedupes repeats, interpolates gaps of <= 3 rounds,
    splits on longer gaps and on reboots, and keeps CSI widths apart. Hand-rolled stacking
    (the previous behaviour here) silently compressed time on packet loss, shifting every
    inferred Doppler frequency — exactly the error this test exists to avoid.
    """
    sel = [f for f, w in frames if t0 <= w < t1 and (f.flags & FLAG_SEQ_FROM_PAYLOAD)]
    if len(sel) < int(2 * fs):
        return None
    segs = [g for g in frames_to_segments(sel, keep_complex=True)
            if g.iq is not None and len(g) >= int(2 * fs)]
    if not segs:
        return None
    return max(segs, key=len).iq          # longest gap-free run in this window


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", required=True)
    ap.add_argument("--baud", type=int, default=921600)
    ap.add_argument("--fs", type=float, default=DEFAULT_FS,
                    help="link sample rate (TDM round rate); 50 for TDM, 100 for star")
    ap.add_argument("--passes", type=int, default=8, help="toward+away pairs. Default 8 (16 segments): at 4 passes the pre-registered >=90% bar had almost no power — a true-95%-accurate method failed it 34% of the time.")
    ap.add_argument("--walk-s", type=float, default=5.0, help="seconds per walking leg")
    ap.add_argument("--turn-s", type=float, default=3.0, help="seconds to turn around")
    ap.add_argument("--out", default="walk_test.npz")
    ap.add_argument("--null", action="store_true",
                    help="control run: nobody walks; every segment must gate out")
    args = ap.parse_args()

    import serial
    ser = serial.Serial()
    ser.port, ser.baudrate, ser.timeout = args.port, args.baud, 0.05
    ser.dtr = False          # asserted DTR/RTS reboots the board — see live_capture.py
    ser.rts = False
    ser.open()
    time.sleep(0.5)
    ser.reset_input_buffer()

    parser = FrameParser()
    frames = []
    schedule = []            # (t0, t1, label)

    mode = "NULL CONTROL — NOBODY MOVES" if args.null else "WALK TEST"
    banner(mode)
    if not args.null:
        print("Stand ~3 m from the boards, facing them, on a clear straight line.\n"
              "Follow the prompts. Walk at a normal steady pace (~1 m/s).")
    # Settling period doubles as the per-run STILL BASELINE: any spectral line present
    # now (fans, compressors — a ~36 Hz fan-class line lives in this room) is
    # environment, and gets notched out of every walking segment's spectrum.
    banner("baseline — everyone stand STILL (8 s)")
    t_base = time.time()
    collect(ser, parser, frames, t_base + 8.0)
    FS = args.fs
    H_still = segment_matrix(frames, t_base, time.time(), FS)
    notch = spectral_lines(H_still, FS, fmin=4.0) if H_still is not None else []
    if notch:
        print(f"  still-baseline interference lines notched: "
              f"{sorted(round(f, 1) for f in notch)} Hz")
    else:
        print("  no interference lines in still baseline")

    for n in range(args.passes):
        for label in ("toward", "away"):
            verb = ("WALK TOWARD the boards NOW" if label == "toward"
                    else "WALK AWAY from the boards NOW")
            banner(f"pass {n + 1}/{args.passes}: {verb}" if not args.null
                   else f"pass {n + 1}/{args.passes}: (nobody moves — '{label}' slot)")
            t0 = time.time()
            collect(ser, parser, frames, t0 + args.walk_s)
            schedule.append((t0, time.time(), label))
            banner("STOP — stand still / turn around")
            collect(ser, parser, frames, time.time() + args.turn_s)

    ser.close()

    # ---- scoring ----
    banner("scoring")
    rows, correct, confident = [], 0, 0
    for i, (t0, t1, label) in enumerate(schedule):
        H = segment_matrix(frames, t0, t1, FS)
        if H is None:
            rows.append((i, label, None, 0.0, "too few frames"))
            print(f"  seg {i:2d} [{label:6s}]: too few frames")
            continue
        # Walking-band floor: the protocol prescribes ~1 m/s, i.e. ~16 Hz bistatic —
        # fmin=4 keeps even a slow shuffle visible while excluding the sub-2 Hz band
        # where a seated person's sway lives (measured: an operator at a computer 1 m
        # away produces real, confident readings there — not an artifact, just not the
        # thing this test scores). Prominence 3.0 = the same gate breathing uses.
        f_hat, conf, note = corroborated_signed_doppler(
            H, FS, fmin=4.0, prominence_ratio=3.0, notch=notch)
        if f_hat is None:
            rows.append((i, label, None, conf, note))
            print(f"  seg {i:2d} [{label:6s}]: {note} (conf {conf:.1f})")
            continue
        confident += 1
        want_pos = (label == "toward")   # approach => f_signed > 0 (ratios.py convention)
        ok = (f_hat > 0) == want_pos
        correct += ok
        rows.append((i, label, f_hat, conf, "OK" if ok else "WRONG SIGN"))
        print(f"  seg {i:2d} [{label:6s}]: f={f_hat:+7.2f} Hz (conf {conf:.1f}) "
              f"-> {'OK' if ok else 'WRONG SIGN'}")

    print()
    p = parser
    print(f"link health: {p.frames_ok} frames, CRC err {p.crc_error_rate:.3%}")
    if args.null:
        ok_null = confident == 0
        print(f"NULL CONTROL: {confident} confident readings out of {len(schedule)} "
              f"segments -> {'PASS (all gated out)' if ok_null else 'FAIL — phantom motion signs'}")
        verdict = "null-pass" if ok_null else "null-fail"
    elif confident:
        acc = correct / confident
        # Pre-registered bar (rev. 2026-07-29, BEFORE any walk test has been run):
        # Clopper–Pearson 95% lower bound on sign accuracy >= 80%. A raw ">=90% of
        # segments" bar at n=8 had no statistical power (7/8 fails; a true-95% method
        # fails 34% of the time). CP makes the sample size part of the verdict.
        from scipy.stats import beta as _beta
        lb = _beta.ppf(0.05, correct, confident - correct + 1) if correct else 0.0
        verdict_ok = lb >= 0.80
        print(f"SIGN ACCURACY: {correct}/{confident} = {acc:.0%}; "
              f"Clopper-Pearson 95% lower bound {lb:.0%} "
              f"-> {'PASS' if verdict_ok else 'not yet'} (pre-registered bar: LB >= 80%)")
        per = {}
        for _, label, f_hat, conf, note in rows:
            if note in ("OK", "WRONG SIGN"):
                per.setdefault(label, [0, 0])
                per[label][1] += 1
                per[label][0] += (note == "OK")
        for label, (k, n_) in sorted(per.items()):
            print(f"   {label:6s}: {k}/{n_}")
        verdict = f"{correct}/{confident}"
    else:
        print("no confident segments — nothing to score")
        verdict = "no-data"

    # ---- archive: raw frames + schedule + verdict ----
    out = pathlib.Path(args.out)
    np.savez_compressed(
        out,
        note=np.str_(f"signed-doppler walk test ({mode}), verdict {verdict}; "
                     f"protocol: docs/signed-doppler.md; fs={args.fs} Hz"),
        schedule=np.array(schedule, dtype=object),
        walls=np.array([w for _, w in frames]),
        seqs=np.array([f.seq for f, _ in frames], dtype=np.int64),
        flags=np.array([f.flags for f, _ in frames], dtype=np.uint8),
        iq=np.array([f.iq for f, _ in frames], dtype=object)
        if len({len(f.iq) for f, _ in frames}) > 1
        else np.stack([f.iq for f, _ in frames]),
    )
    print(f"\narchived {out} ({out.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
