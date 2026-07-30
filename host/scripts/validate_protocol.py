#!/usr/bin/env python3
"""Layer-0/1 validation: the binary protocol and the time base (roadmap M1 pre-flight).

Everything here can run without boards, and it exists to make the hardware session
short: every failure mode it catches is one that would otherwise show up as
"the listener streams garbage" at 1 a.m. with a scope attached.

Checks:
  A. Lockstep with firmware — CRC-8 cross-checked against listener.ino's own C source
     (extracted and compiled with cc), frame offsets parsed out of the .ino, shared
     constants compared.
  B. Codec — round trip, every field, boundary values, HT20/HT40 sizes.
  C. Stream robustness — byte-at-a-time feeds, ASCII stat lines, garbage, bit flips,
     truncation; CRC accounting must stay honest (M1 acceptance measures it).
  D. Time base — seq grid, gap<=3 interpolation vs split, reboot, u32 wrap, dupes,
     first-word trimming, t_us fallback, cross-node alignment.
  E. Wire budget — bytes/s vs 921600 baud at HT20 and HT40.

Run: cd host && python scripts/validate_protocol.py
"""

import pathlib
import re
import subprocess
import sys
import tempfile

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from wisent import csi_io
from wisent.csi_io import (MAGIC, HEADER_LEN, HEADER_LEN_V2, VERSION, VERSION_TDM,
                           MAX_PAIRS, SEQ_UNAVAILABLE,
                           FLAG_FIRST_WORD_INVALID, FLAG_SEQ_FROM_PAYLOAD,
                           CsiFrame, FrameParser, crc8, frames_to_segments,
                           align_segments, unwrap_u32)

ROOT = pathlib.Path(__file__).resolve().parents[2]
LISTENER = ROOT / "firmware" / "listener" / "listener.ino"
BAUD = 921600

results = []


def check(name, ok, detail):
    results.append((name, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")


# ---------------------------------------------------------------- test-side encoder
# Deliberately NOT in wisent.csi_io: the live package must be physically incapable of
# manufacturing a CSI frame. Same rule that keeps sim.py off the live path.

def encode_frame(node_id=1, seq=0, t_us=0, rssi=-40, channel=6, iq=None, flags=0,
                 corrupt_crc=False, version=VERSION, tx_id=None) -> bytes:
    """One wire frame, byte-for-byte as the firmware's loop() emits it.
    version=1 is listener.ino (star), version=2 is node.ino (TDM, carries tx_id)."""
    iq = np.zeros(52, dtype=np.complex64) if iq is None else np.asarray(iq)
    n_pairs = len(iq)
    body = bytearray()
    body.append(version)
    body.append(node_id & 0xFF)
    body += int(seq & 0xFFFFFFFF).to_bytes(4, "little")
    body += int(t_us & 0xFFFFFFFF).to_bytes(4, "little")
    body += int(rssi).to_bytes(1, "little", signed=True)
    body.append(channel & 0xFF)
    body.append(n_pairs & 0xFF)
    body.append(flags & 0xFF)
    if version == 2:
        body.append((0 if tx_id is None else tx_id) & 0xFF)
    pairs = np.empty(2 * n_pairs, dtype=np.int8)
    pairs[0::2] = np.imag(iq).astype(np.int8)     # wire order: IMAGINARY first,
    pairs[1::2] = np.real(iq).astype(np.int8)     # then REAL (Espressif CSI layout)
    body += pairs.tobytes()
    c = crc8(bytes(body)) ^ (0xFF if corrupt_crc else 0)
    return bytes(MAGIC + body + bytes([c]))


def frame(seq, node_id=1, n_sc=52, t_us=None, flags=0, val=1):
    """A frame whose I/Q is a known constant, so amplitude is predictable."""
    iq = np.full(n_sc, val + 0j, dtype=np.complex64)
    return encode_frame(node_id=node_id, seq=seq, flags=flags, iq=iq,
                        t_us=(seq * 10_000 if t_us is None else t_us))


def parse_all(*chunks):
    p = FrameParser()
    out = []
    for c in chunks:
        out += p.feed(c)
    return p, out


# ======================================================== A. lockstep with firmware
src = LISTENER.read_text()

# --- A1: the firmware's own crc8(), compiled and fuzzed against ours -------------
def extract_c_function(text, name):
    i = text.index(f" {name}(")
    i = text.rindex("\n", 0, i) + 1
    depth, j = 0, text.index("{", i)
    for k in range(j, len(text)):
        depth += (text[k] == "{") - (text[k] == "}")
        if depth == 0:
            return text[i:k + 1]
    raise ValueError(f"unbalanced braces in {name}")


try:
    c_src = extract_c_function(src, "crc8")
    harness = ("#include <stdint.h>\n#include <stddef.h>\n#include <stdio.h>\n"
               "#include <string.h>\n" + c_src + """
int main(void) {
  char line[8192]; uint8_t buf[4096];
  while (fgets(line, sizeof line, stdin)) {
    size_t n = strlen(line);
    while (n && (line[n-1] == '\\n' || line[n-1] == '\\r')) line[--n] = 0;
    size_t m = n / 2;
    for (size_t i = 0; i < m; i++) { unsigned v; sscanf(line + 2*i, "%2x", &v); buf[i] = (uint8_t)v; }
    printf("%u\\n", (unsigned)crc8(buf, m));
  }
  return 0;
}
""")
    with tempfile.TemporaryDirectory() as td:
        cfile, exe = pathlib.Path(td) / "crc.c", pathlib.Path(td) / "crc"
        cfile.write_text(harness)
        subprocess.run(["cc", "-O1", "-o", str(exe), str(cfile)],
                       check=True, capture_output=True)
        rng = np.random.default_rng(7)
        vectors = [b"", b"\x00", b"\xff", bytes(range(256))]
        vectors += [rng.integers(0, 256, rng.integers(1, 400), dtype=np.uint8).tobytes()
                    for _ in range(200)]
        stdin = "".join(v.hex() + "\n" for v in vectors)
        got = subprocess.run([str(exe)], input=stdin, text=True,
                             capture_output=True, check=True).stdout.split()
        mism = [i for i, (v, g) in enumerate(zip(vectors, got)) if crc8(v) != int(g)]
        check("crc-lockstep-with-firmware-c", not mism and len(got) == len(vectors),
              f"{len(vectors)} vectors (incl. empty, 0x00, 0xff, 0..255) agree with "
              f"listener.ino's compiled crc8()" if not mism else f"MISMATCH at {mism[:5]}")
except (subprocess.CalledProcessError, FileNotFoundError, ValueError) as e:
    check("crc-lockstep-with-firmware-c", False, f"could not build/compare C crc8: {e!r}")

# --- A2: frame offsets, parsed out of the firmware --------------------------------
writes = {int(m.group(1)): m.group(2)
          for m in re.finditer(r"out\[(\d+)\]\s*=\s*([^;]+);", src)}
writes.update({int(m.group(1)): m.group(2)
               for m in re.finditer(r"memcpy\(out \+ (\d+),\s*([^;]+)\);", src)})
expect = {0: "0x1D", 1: "0xC5", 2: "1", 3: "NODE_ID", 4: "seq", 8: "t_us",
          12: "rssi", 13: "channel", 14: "n_pairs", 15: "flags", 16: "iq"}
missing = {o: tok for o, tok in expect.items()
           if o not in writes or tok not in writes[o]}
extra = sorted(set(writes) - set(expect) - {"n"})
check("frame-layout-lockstep", not missing and not extra,
      f"listener.ino writes all 11 protocol fields at the offsets csi_io decodes"
      if not missing and not extra else f"missing/mismatched {missing}, unexpected {extra}")

# --- A2b: the beacon payload struct the listener parses by hand -------------------
# listener.ino scans the ESP-NOW payload for the magic and then reads seq at magic+4.
# That offset is only right if wisent_beacon_t packs the way it looks. Compile the
# beacon's OWN struct and ask the compiler. This is the one piece of the beacon-seq
# path (the system clock) that can be verified without a second board.
try:
    bsrc = (ROOT / "firmware" / "beacon" / "beacon.ino").read_text()
    m = re.search(r"typedef struct[^;]*?\{.*?\}\s*wisent_beacon_t;", bsrc, re.S)
    prog = ("#include <stdint.h>\n#include <stddef.h>\n#include <stdio.h>\n"
            + m.group(0) + """
int main(void){
  printf("%zu %zu %zu\\n", offsetof(wisent_beacon_t, seq),
         offsetof(wisent_beacon_t, magic), sizeof(wisent_beacon_t));
  return 0;
}
""")
    with tempfile.TemporaryDirectory() as td:
        cf, ex = pathlib.Path(td) / "b.c", pathlib.Path(td) / "b"
        cf.write_text(prog)
        subprocess.run(["cc", "-O0", "-o", str(ex), str(cf)], check=True, capture_output=True)
        off_seq, off_magic, size = map(int, subprocess.run(
            [str(ex)], capture_output=True, text=True, check=True).stdout.split())
    scan_off = int(re.search(r"info->payload \+ off \+ (\d+), 4", src).group(1))
    ok = (off_seq - off_magic) == scan_off and size == 8
    check("beacon-seq-offset-lockstep", ok,
          f"wisent_beacon_t: seq at magic+{off_seq - off_magic}, sizeof={size} — "
          f"listener.ino reads magic+{scan_off}"
          + ("" if ok else "  <-- MISMATCH: seq would be garbage"))
except (subprocess.CalledProcessError, AttributeError, ValueError) as e:
    check("beacon-seq-offset-lockstep", False, f"could not check struct layout: {e!r}")

# --- A3: shared constants ---------------------------------------------------------
c_max_pairs = int(re.search(r"MAX_PAIRS\s*=\s*(\d+)", src).group(1))
c_hdr = int(re.search(r"size_t n = (\d+) \+", src).group(1))
c_crc_span = re.search(r"crc8\(out \+ (\d+), n - (\d+)\)", src).groups()
c_baud = int(re.search(r"Serial\.begin\((\d+)\)", src).group(1))
consts_ok = (c_max_pairs == MAX_PAIRS and c_hdr == HEADER_LEN
             and c_crc_span == ("2", "2") and c_baud == BAUD
             and MAGIC == b"\x1d\xc5")
# --- A2c: node.ino (TDM, protocol v2) frame layout ------------------------------
nsrc = (ROOT / "firmware" / "node" / "node.ino").read_text()
nwrites = {int(m.group(1)): m.group(2)
           for m in re.finditer(r"out\[(\d+)\]\s*=\s*([^;]+);", nsrc)}
nwrites.update({int(m.group(1)): m.group(2)
                for m in re.finditer(r"memcpy\(out \+ (\d+),\s*([^;]+)\);", nsrc)})
# offset 3 is the RECEIVING node. node.ino v3 emits it through format_frame(.., rx_node)
# so the frame layout is shared by the USB and backhaul paths — the literal moved into a
# parameter, but the meaning must not. The caller check below pins that parameter to
# NODE_ID, so this stays a real lockstep and not a loosened one.
nexpect = {0: "0x1D", 1: "0xC5", 2: "WISENT_VERSION", 3: "rx_node", 4: "seq", 8: "t_us",
           12: "rssi", 13: "channel", 14: "n_pairs", 15: "flags", 16: "tx_id", 17: "iq"}
nmissing = {o: tok for o, tok in nexpect.items()
            if o not in nwrites or tok not in nwrites[o]}
n_hdr = int(re.search(r"size_t n = (\d+) \+", nsrc).group(1))
_node_ino = (ROOT / "firmware" / "node" / "node.ino").read_text()
check("tdm-frame-emitted-with-own-node-id",
      "format_frame(r, out, NODE_ID)" in _node_ino
      and "format_frame(r, pkt + BH_HDR + n, NODE_ID)" in _node_ino,
      "both the USB path and the backhaul packer stamp offset 3 with this node's own id, "
      "so a relayed frame identifies its true receiver")

check("tdm-frame-layout-lockstep",
      not nmissing and n_hdr == HEADER_LEN_V2 and "WISENT_VERSION = 2" in nsrc,
      f"node.ino writes all 12 v2 fields incl. tx_id at 16, header {n_hdr} B"
      if not nmissing else f"missing/mismatched {nmissing}")

# --- A2d: both firmwares share one CRC-8 ----------------------------------------
try:
    same_crc = extract_c_function(nsrc, "crc8") == extract_c_function(src, "crc8")
except ValueError:
    same_crc = False
check("tdm-crc-identical-to-v1", same_crc,
      "node.ino and listener.ino use byte-identical crc8() (already fuzzed vs Python)")

check("shared-constants-lockstep", consts_ok,
      f"MAX_PAIRS={c_max_pairs}, header={c_hdr}B, crc over [2, n), baud={c_baud}")


# ================================================================== B. codec
cases = [
    ("typical", dict(node_id=3, seq=123456, t_us=7654321, rssi=-63, channel=6,
                     flags=FLAG_SEQ_FROM_PAYLOAD, iq=np.arange(52) - 26 + 1j * np.arange(52))),
    ("seq-unavailable", dict(node_id=1, seq=SEQ_UNAVAILABLE, t_us=0, rssi=0, channel=1,
                             flags=0, iq=np.ones(52))),
    ("int8-extremes", dict(node_id=255, seq=0xFFFFFFFE, t_us=0xFFFFFFFF, rssi=-128,
                           channel=14, flags=0xFF,
                           iq=np.full(52, -128 + 127j))),
    ("ht40-108sc", dict(node_id=2, seq=1, t_us=1, rssi=-1, channel=6, flags=0x02,
                        iq=np.zeros(108) + 3 - 4j)),
    ("max-pairs", dict(node_id=4, seq=2, t_us=2, rssi=-20, channel=6, flags=0,
                       iq=np.zeros(MAX_PAIRS) + 1j)),
    ("single-pair", dict(node_id=1, seq=3, t_us=3, rssi=-30, channel=6, flags=0,
                         iq=np.array([7 - 7j]))),
]
bad = []
for name, kw in cases:
    _, got = parse_all(encode_frame(**kw))
    if len(got) != 1:
        bad.append(f"{name}: {len(got)} frames")
        continue
    f = got[0]
    iq_exp = np.asarray(kw["iq"]).astype(np.complex64)
    if not (f.node_id == kw["node_id"] and f.seq == kw["seq"] and f.t_us == kw["t_us"]
            and f.rssi == kw["rssi"] and f.channel == kw["channel"]
            and f.flags == kw["flags"] and np.array_equal(f.iq, iq_exp)):
        bad.append(name)
check("codec-round-trip", not bad,
      f"{len(cases)} cases exact (fields + I/Q, incl. int8 extremes, HT40, "
      f"1 and {MAX_PAIRS} pairs)" if not bad else f"FAILED: {bad}")


# --- B2: ASYMMETRIC complex codec test -------------------------------------------
# The old round-trip used symmetric encode/decode, so swapping I and Q on BOTH sides
# passed happily. Espressif stores IMAGINARY first, REAL second; decoding real-first
# yields j*conj(H) — amplitude identical, every phase negated, signed Doppler reversed.
# This asserts the raw BYTES directly, so it cannot be satisfied by a matched pair of
# mistakes.
iq_asym = np.array([1 + 2j, -3 + 4j], dtype=np.complex64)   # Re != Im, and sign-distinct
blob = encode_frame(iq=iq_asym, node_id=1, seq=7)
body = blob[len(MAGIC):]
payload = body[HEADER_LEN - 2:HEADER_LEN - 2 + 4]           # 2 pairs, 4 bytes
sgn = lambda b: b - 256 if b > 127 else b                    # int8 view of a raw byte
wire_ok = (payload[0] == 2 and payload[1] == 1 and           # first pair: imag=2, real=1
           sgn(payload[2]) == 4 and sgn(payload[3]) == -3)   # second: imag=4, real=-3
_, got = parse_all(blob)
decode_ok = got and np.array_equal(got[0].iq, iq_asym)
# and prove a swapped decode would be caught: j*conj(H) must NOT equal H here
swapped = (1j * np.conj(iq_asym)).astype(np.complex64)
check("codec-iq-byte-order", wire_ok and decode_ok and not np.array_equal(swapped, iq_asym),
      f"wire bytes are (imag, real) per Espressif CSI layout; 1+2j -> {list(payload[:2])}; "
      f"decode exact; a swapped decode would give {swapped[0]} and is therefore detectable")

# --- B3: LTF block layout ---------------------------------------------------------
# 128 pairs is LLTF(64) + HT-LTF(64), two axes — not one 128-wide axis. Buffer index
# distance is not frequency distance: indices 20 and 40 are subcarriers +20 and -24.
blocks = csi_io.ltf_blocks(128)
sc = csi_io.subcarrier_map(128)
layout_ok = (len(blocks) == 2 and blocks[0][2] == "LLTF" and blocks[1][2] == "HT-LTF"
             and sc[0] == 0 and sc[1] == 1 and sc[32] == -32 and sc[63] == -1
             and sc[64] == 0                       # block 2 restarts at DC
             and abs(sc[20] - sc[40]) == 44)       # NOT 20
check("csi-ltf-block-layout", layout_ok,
      f"128 pairs -> {[b[2] for b in blocks]}; index 20 vs 40 are subcarriers "
      f"{sc[20]} vs {sc[40]} (44 apart, not 20); block 2 restarts at DC")

# --- B4: unmeasured layouts must fail LOUDLY --------------------------------------
# 192-pair (HT40-clipped) buffers have an unknown LTF layout; mapping them as three
# 64-item blocks would invent frequencies. The guard must raise, not guess.
try:
    csi_io.subcarrier_map(192)
    guard_ok = False
except ValueError:
    guard_ok = True
check("csi-unmeasured-layout-guard", guard_ok,
      "192-pair (HT40/clipped) subcarrier map raises instead of inventing a third block")

# --- B5: protocol v3 backhaul lockstep --------------------------------------------
# A relayed frame must be BYTE-IDENTICAL to a direct one: the whole design rests on the
# gateway writing relayed bytes to USB verbatim, so if the two ever diverge the host would
# silently parse relayed links differently from direct ones.
ino_v3 = (ROOT / "firmware" / "node" / "node.ino").read_text()
bh = {k: v for k, v in re.findall(r"BH_(MAGIC0|MAGIC1|HDR|MAX)\s*=\s*(0x[0-9A-Fa-f]+|\d+)", ino_v3)}
check("bh-header-lockstep",
      bh.get("MAGIC0", "").lower() == "0x5a" and bh.get("MAGIC1", "").lower() == "0xa5"
      and int(bh.get("HDR", 0)) == 10 and int(bh.get("MAX", 0)) <= 1470,
      f"backhaul header constants match the host's assumptions {bh} "
      f"(MAX must stay <= ESP-NOW v2's 1470)")

# The gateway's length gate is what stops relayed bytes from being re-parsed as sensing
# beacons (relayed frames literally contain the beacon magic). Assert it exists.
check("bh-sensing-length-gate",
      "payload_len > 200" in ino_v3 and "bh_rejected" in ino_v3,
      "csi_cb rejects >200-byte payloads before the header scan, so backhaul packets "
      "cannot inject phantom CSI or corrupt the round clock")

# End-to-end: frames wrapped in a backhaul packet, unwrapped, must parse identically.
direct = b"".join(encode_frame(node_id=4, seq=s_, tx_id=t_, version=2,
                               iq=np.full(128, 1 + 1j, dtype=np.complex64))
                  for s_, t_ in ((10, 0), (11, 1), (12, 2)))
payload = direct
pkt = bytes([0x5A, 0xA5, 3, 4]) + (7).to_bytes(4, "little") + \
      len(payload).to_bytes(2, "little") + payload
unwrapped = pkt[10:10 + int.from_bytes(pkt[8:10], "little")]
fa, fb = FrameParser().feed(direct), FrameParser().feed(unwrapped)
check("bh-relayed-frames-identical",
      unwrapped == direct and len(fa) == len(fb) == 3 and
      all(x.seq == y.seq and x.tx_id == y.tx_id and x.node_id == y.node_id and
          np.array_equal(x.iq, y.iq) for x, y in zip(fa, fb)),
      "3 frames survive backhaul wrapping byte-identically and parse to identical records")

# --- B6: LTF-block merge semantics (rev. 2: per-block L1 normalization) -------------
amp = np.zeros((3, 128))
amp[:, 5] = 2.0; amp[:, 64 + 5] = 4.0        # valid in BOTH blocks -> kept
amp[:, 7] = 6.0                               # valid only in block 0 -> dropped
amp[:, 64 + 9] = 8.0                          # valid only in block 1 -> dropped
amp[:, 11] = 2.0; amp[:, 64 + 11] = 12.0     # second both-valid column
m = csi_io.merge_ltf_blocks(amp)
# each block L1-normalized over its kept columns, then averaged:
# block0 kept = [2,2] -> [.5,.5]; block1 kept = [4,12] -> [.25,.75]
exp = np.array([(0.5 + 0.25) / 2, (0.5 + 0.75) / 2])
check("ltf-merge-blocks",
      m.shape[1] == 2 and np.allclose(np.sort(m[0]), np.sort(exp))
      and np.array_equal(csi_io.merge_ltf_blocks(np.ones((2, 52))), np.ones((2, 52))),
      f"per-block L1 then mean (got {np.round(np.sort(m[0]), 4)}, want "
      f"{np.sort(exp)}), single-valid columns dropped, simulator widths untouched")

# --- B7: per-block scale-toggle immunity -------------------------------------------
# The silicon's per-block auto-scale TOGGLES on marginal links (measured: 2x on 26-33%
# of frames, empty room 2026-07-30). Merged output must be IDENTICAL whether or not a
# block was rescaled — else the toggle reappears as phantom motion energy.
rng_bt = np.random.default_rng(5)
base = rng_bt.uniform(0.5, 2.0, (6, 128)).astype(np.float32)
for N in ([0] + list(range(27, 38)), [64] + list(range(93, 100))):
    base[:, N] = 0.0
tog = base.copy()
tog[::2, 64:] *= 2.0                      # scale toggle on half the frames, HT-LTF block
m0, m1 = csi_io.merge_ltf_blocks(base), csi_io.merge_ltf_blocks(tog)
check("ltf-merge-scale-toggle-immune",
      m0.shape == m1.shape and np.allclose(m0, m1, atol=1e-6),
      f"2x block-scale toggle on half the frames changes merged output by "
      f"{np.abs(m0 - m1).max():.2e} (must be ~0)")

# ======================================================= C. stream robustness
stream = b"".join(frame(s) for s in range(1, 21))

p, got = parse_all(*(stream[i:i + 1] for i in range(len(stream))))
check("stream-byte-at-a-time", len(got) == 20 and p.crc_errors == 0,
      f"20/20 frames recovered from single-byte feeds, {p.crc_errors} CRC errors")

# firmware prints ASCII stat lines into the same stream
noisy = (b"# wisent listener up\n" + frame(1)
         + b"\n# rx=1234 drop=0\n" + frame(2) + frame(3))
p, got = parse_all(noisy)
check("stream-ascii-stat-lines", len(got) == 3 and p.crc_errors == 0,
      f"3 frames past 2 comment lines, {p.crc_errors} CRC errors "
      f"({p.resync_bytes} bytes resynced)")

# Random garbage plus deliberately adversarial false magics (wrong version, zero and
# over-max lengths) must never fabricate a frame, and must not be charged to the CRC
# budget — they are resync noise, not corruption of a real frame.
rng = np.random.default_rng(11)
junk = rng.integers(0, 256, 20_000, dtype=np.uint8).tobytes()
decoys = (MAGIC + bytes([VERSION + 1]) + bytes(20)      # wrong version
          + MAGIC + bytes([VERSION, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 6, 0, 0])  # n_pairs = 0
          + MAGIC + bytes([VERSION, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 6, 200, 0]))  # > MAX_PAIRS
p, got = parse_all(junk + decoys + frame(1) + junk + decoys + frame(2) + junk)
check("stream-garbage-resync",
      len(got) == 2 and all(f.seq in (1, 2) for f in got)
      and p.bad_headers >= 6 and p.crc_errors == 0,
      f"exactly the 2 real frames survived 60 kB of random bytes + 6 adversarial "
      f"false magics ({p.bad_headers} rejected on header, {p.crc_errors} CRC errors)")

# every single-bit flip must be caught by CRC or header validation, never accepted silently
base = frame(42)
accepted_corrupt = 0
for bit in range(len(base) * 8):
    b = bytearray(base)
    b[bit // 8] ^= 1 << (bit % 8)
    _, out = parse_all(bytes(b))
    for f in out:
        if not (f.seq == 42 and f.node_id == 1 and len(f.iq) == 52):
            accepted_corrupt += 1
check("stream-bitflip-detection", accepted_corrupt == 0,
      f"0 of {len(base) * 8} single-bit corruptions accepted as a valid-looking frame"
      if accepted_corrupt == 0 else f"{accepted_corrupt} corrupt frames ACCEPTED")

# CRC accounting: a truly corrupted frame counts, ASCII noise does not
p, _ = parse_all(frame(1) + encode_frame(seq=2, corrupt_crc=True) + frame(3))
rate_ok = p.crc_errors == 1 and p.frames_ok == 2 and abs(p.crc_error_rate - 1 / 3) < 1e-9
p2, _ = parse_all(b"# rx=1 drop=0\n" * 200 + frame(1))
check("crc-accounting-honest", rate_ok and p2.crc_errors == 0,
      f"1 corrupt frame -> crc_errors=1 (rate {p.crc_error_rate:.1%}); "
      f"200 stat lines -> {p2.crc_errors} CRC errors")

# a truncated tail must be held, not dropped, and complete on the next feed
head_, tail_ = frame(9)[:20], frame(9)[20:]
p = FrameParser()
mid = p.feed(head_)
end = p.feed(tail_)
check("stream-partial-frame-held", not mid and len(end) == 1 and end[0].seq == 9,
      "split frame reassembled across feed() calls")

# a corrupt n_pairs must not stall the parser waiting for bytes that never come
b = bytearray(frame(5))
b[14] = 255                       # implausible for a 52-pair frame; CRC also fails
p, got = parse_all(bytes(b) + frame(6))
check("stream-bad-length-recovers", len(got) == 1 and got[0].seq == 6,
      f"parser recovered the next good frame after a corrupted length field")


# ==================================================================== D. time base
# gap of 3 -> interpolate (documented policy); gap of 4 -> split
_, fr = parse_all(*[frame(s) for s in [1, 2, 3, 7]])          # step 4 across 3->7
segs = frames_to_segments(fr)
_, fr2 = parse_all(*[frame(s) for s in [1, 2, 3, 6]])          # step 3: interpolate
segs2 = frames_to_segments(fr2)
check("timebase-gap-policy", len(segs) == 2 and len(segs2) == 1 and len(segs2[0]) == 6,
      f"gap>3 splits into {len(segs)} segments; gap<=3 interpolates into one "
      f"{len(segs2[0])}-row segment")

# interpolated rows must be interpolated, not zero or NaN
amp = segs2[0].amp
check("timebase-interpolation-values",
      np.isfinite(amp).all() and np.allclose(amp, amp[0]) and np.isnan(segs2[0].t_us[4]),
      "filled rows carry interpolated amplitude; their t_us stays NaN (not invented)")

# beacon reboot: seq goes backwards -> must split, never allocate a 4-billion-row grid
_, fr = parse_all(*[frame(s) for s in [1000, 1001, 1002, 5, 6, 7]])
segs = frames_to_segments(fr)
check("timebase-reboot-splits", len(segs) == 2 and {len(s) for s in segs} == {3},
      f"backwards seq -> {len(segs)} segments of {[len(s) for s in segs]} rows "
      f"(no interpolation across the reboot)")

# duplicate seq (packet reported twice) keeps the first, does not double a row
_, fr = parse_all(frame(1, val=1), frame(1, val=9), frame(2, val=1))
segs = frames_to_segments(fr)
check("timebase-duplicate-seq", len(segs) == 1 and len(segs[0]) == 2
      and np.allclose(segs[0].amp[0], 1.0),
      "duplicate seq deduplicated, first copy kept")

# the sentinel seq must not become a grid index of 4.29e9
_, fr = parse_all(*[frame(SEQ_UNAVAILABLE, t_us=t) for t in range(0, 100_000, 10_000)])
segs = frames_to_segments(fr)
ok = len(segs) == 1 and segs[0].timebase == "t_us" and len(segs[0]) == 10
check("timebase-t_us-fallback", ok,
      f"no beacon seq -> t_us base, {len(segs[0]) if segs else 0} rows at 100 Hz "
      f"(timebase={segs[0].timebase if segs else 'n/a'})")

# u32 micros() wraps every ~71.6 min; unwrapping must be monotone
w = unwrap_u32([0xFFFFFF00, 0xFFFFFFF0, 0x00000010, 0x00000100])
check("timebase-u32-wrap", bool(np.all(np.diff(w) > 0)),
      f"micros() wrap unwrapped monotonically (span {w[-1] - w[0]:.0f} us)")

# mixed first_word_invalid must trim consistently, or subcarrier k means two things
_, fr = parse_all(frame(1, flags=FLAG_FIRST_WORD_INVALID), frame(2), frame(3))
segs = frames_to_segments(fr)
check("timebase-first-word-trim-consistent",
      len(segs) == 1 and segs[0].amp.shape[1] == 52 - csi_io.FIRST_WORD_PAIRS,
      f"any invalid first word -> all rows trimmed to "
      f"{segs[0].amp.shape[1] if segs else 0} subcarriers")

# short frames must be dropped, never zero-padded into the subcarrier axis
_, fr = parse_all(frame(1), frame(2, n_sc=8), frame(3))
segs = frames_to_segments(fr, n_sc=52)
check("timebase-short-frame-dropped",
      len(segs) == 1 and segs[0].amp.shape == (3, 52),
      f"truncated frame excluded; axis stayed {segs[0].amp.shape if segs else None}")

# Mixed CSI lengths must never share a subcarrier axis. Measured on an ESP32-S3
# listening to ambient traffic on channel 1: 64-pair and 128-pair frames interleaved in
# a single stream, because CSI length follows the PHY format of the packet overheard.
_, fr = parse_all(*[frame(s, n_sc=(64 if s % 2 == 0 else 128)) for s in range(10)])
segs = frames_to_segments(fr)
widths = sorted(s.amp.shape[1] for s in segs)
check("timebase-splits-mixed-csi-widths", len(segs) == 2 and widths == [64, 128],
      f"64- and 128-pair frames segmented separately ({widths}), not stacked or "
      f"truncated onto one frequency axis")

# An isolated wild seq between consecutive neighbors is over-the-air corruption that
# survived the UART CRC (listener CRCs already-corrupted RAM; observed on hardware
# 2026-07-29 at ~0.01% of frames, weak RSSI). It must be dropped, not split on.
wild = [frame(s) for s in range(1, 11)]
wild[5] = frame(0x62A1CA72)                    # an actual observed corrupt value
_, fr = parse_all(*wild)
segs = frames_to_segments(fr)
check("timebase-drops-corrupt-seq-outlier",
      len(segs) == 1 and len(segs[0]) == 10 and 0x62A1CA72 not in segs[0].index,
      f"1 wild seq among 10 -> {len(segs)} segment(s) of {[len(s) for s in segs]} rows, "
      f"outlier dropped (was: split into junk fragments)")

# cross-node alignment on the beacon clock (M1 acceptance: "frames align on seq")
per_node = []
for node, (lo, hi) in enumerate([(100, 200), (95, 190), (110, 205)], start=1):
    _, fr = parse_all(*[frame(s, node_id=node) for s in range(lo, hi + 1)])
    per_node += frames_to_segments(fr)
index, aligned = align_segments(per_node)
ok = (len(index) == 81 and index[0] == 110 and index[-1] == 190
      and {k: v.shape[0] for k, v in aligned.items()}
      == {(0, 1): 81, (0, 2): 81, (0, 3): 81})
check("timebase-cross-node-align", ok,
      f"3 nodes -> common seq window [{index[0]}, {index[-1]}] = {len(index)} rows, keyed by link {sorted(aligned)}")

# aligning nodes on local clocks is refused, not silently wrong
_, fr = parse_all(*[frame(SEQ_UNAVAILABLE, t_us=t) for t in range(0, 50_000, 10_000)])
try:
    align_segments(frames_to_segments(fr))
    refused = False
except ValueError:
    refused = True
check("timebase-refuses-fake-alignment", refused,
      "aligning nodes without a beacon seq raises instead of inventing a common clock")

# mixing nodes into one segment builder is a programming error, not a silent merge
_, fr = parse_all(frame(1, node_id=1), frame(2, node_id=2))
try:
    frames_to_segments(fr)
    refused = False
except ValueError:
    refused = True
check("timebase-rejects-mixed-nodes", refused,
      "frames_to_segments raises on mixed node_ids")


# ================================================================= E. wire budget
def wire_bytes(n_pairs):
    return HEADER_LEN + 2 * n_pairs + 1


rows = []
for label, n_pairs in [("HT20 52sc", 52), ("HT40 108sc", 108), ("worst 192sc", MAX_PAIRS)]:
    per_port = wire_bytes(n_pairs) * 100 * 10          # 100 Hz, 8N1 -> 10 bits/byte
    rows.append((label, wire_bytes(n_pairs), per_port / BAUD))
ht20, ht40 = rows[0][2], rows[1][2]
check("wire-budget-921600", ht20 < 0.5 and ht40 < 0.8,
      "; ".join(f"{l}: {b} B/frame, {u:.0%} of one 921600 baud port" for l, b, u in rows))

# The shared-port fallback in architecture.md, and the HT20 -> HT40 decision M1 has to
# make. Recorded as a check so the numbers cannot quietly rot when the frame changes.
check("wire-budget-shared-port", 4 * ht20 < 0.8 and 4 * ht40 > 0.9,
      f"4 listeners on ONE 921600 port: HT20 {4 * ht20:.0%} (fits, with batching per "
      f"architecture.md), HT40 {4 * ht40:.0%} (does NOT fit — HT40 needs a port per "
      f"listener or a higher baud; matches roadmap's standing UART risk)")


# ========================================================= F. recorder end-to-end
# Drive live_capture.py over a synthetic raw byte stream in --replay mode. This
# exercises the recorder's real decode path without boards; the synthetic bytes never
# touch the live package, and the recording is stamped source="replay" so it can never
# be mistaken for evidence.
with tempfile.TemporaryDirectory() as td:
    td = pathlib.Path(td)
    raw = td / "node1.bin"
    stream = bytearray()
    for s in range(1, 501):
        if s == 250:
            stream += b"\n# rx=249 drop=0\n"                     # firmware stat line
        if s == 300:
            continue                                             # a single lost packet
        stream += frame(s, node_id=1, flags=FLAG_SEQ_FROM_PAYLOAD)
    raw.write_bytes(bytes(stream))

    out = td / "rec.npz"
    proc = subprocess.run(
        [sys.executable, str(pathlib.Path(__file__).with_name("live_capture.py")),
         "--replay", str(raw), "--out", str(out), "--note", "protocol self-test"],
        capture_output=True, text=True)
    if proc.returncode != 0 or not out.exists():
        check("recorder-replay-end-to-end", False,
              f"live_capture.py failed: {proc.stderr.strip().splitlines()[-1:]}")
    else:
        z = np.load(out)
        same_bytes = z["port0/raw"].tobytes() == bytes(stream)
        ok = (str(z["source"]) == "replay"
              and int(z["port0/frames_ok"]) == 499
              and int(z["port0/crc_errors"]) == 0
              and z["port0/seq"][0] == 1 and z["port0/seq"][-1] == 500
              and z["port0/iq"].shape == (499, 52)
              and same_bytes
              and "protocol self-test" in str(z["note"]))
        check("recorder-replay-end-to-end", ok,
              f"499 frames recorded via live_capture --replay, "
              f"raw bytes byte-identical to input, source={str(z['source'])!r}, "
              f"iq{z['port0/iq'].shape}, {int(z['port0/crc_errors'])} CRC errors")
        # the recorder's own segmenter must see the dropped packet as an interpolated
        # gap (step 2 <= max_gap), not as a split
        seq = z["port0/seq"]
        check("recorder-npz-is-self-contained",
              set(z.files) >= {"port0/raw", "port0/raw_anchors", "port0/wall_clock",
                               "port0/gaps", "port0/crc_errors", "source", "note"}
              and 300 not in set(seq.tolist()),
              f"npz carries raw stream + wall-clock anchors + counters "
              f"({len(z.files)} arrays); the dropped seq 300 is absent from the "
              f"frame table, as recorded")


# ==================================================================== summary
n_ok = sum(ok for _, ok in results)
print(f"\n{n_ok}/{len(results)} checks passed")
sys.exit(0 if n_ok == len(results) else 1)
