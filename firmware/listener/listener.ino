// wisent listener — captures CSI from beacon frames, streams binary frames over UART.
// Target: arduino-esp32 v3.x (ESP-IDF v5).
// Verified 2026-07-29 on ESP32-S3 (arduino-esp32 3.3.11), FQBN:
//   esp32:esp32:esp32s3:CDCOnBoot=default        <- Serial must be UART0, see README
// CSI callback fires and frames decode with 0 CRC errors; the beacon-seq path is still
// unverified because that needs a second board running beacon.ino.
// Protocol: docs/architecture.md §Protocol — keep in lockstep with host/wisent/csi_io.py.

#include <WiFi.h>
#include <esp_wifi.h>

// Overridable at build time so a deployment can be retuned to the quietest channel
// without editing the sketch:
//   arduino-cli compile --build-property "build.extra_flags=-DWISENT_CHANNEL=1 -DNODE_ID=2"
#ifndef WISENT_CHANNEL
#define WISENT_CHANNEL 6
#endif
#ifndef NODE_ID
// MUST be unique per board (1..4). The host keys every link on node_id; two boards
// flashed with the same id silently merge into one nonsense stream.
#define NODE_ID 1
#endif

// The beacon's sequence number is the system clock — without it, listeners cannot be
// aligned to each other and VRTI/LinkBVP have no multi-link input at all (the host
// falls back to local micros(), which is single-node only). So this is ON by default.
// If your IDF version has no info->payload / payload_len on wifi_csi_info_t, this is
// the ONE line to comment out; the host handles seq-less frames, in degraded mode, and
// live_capture.py will tell you that is what happened.
#define WISENT_TRY_PAYLOAD_SEQ 1
// Set this to your beacon's STA MAC — beacon.ino prints it as a pasteable initializer,
// or override at build time (no spaces inside the braces):
//   --build-property "build.extra_flags=-DBEACON_MAC_BYTES={0xAC,0xA7,0x04,0xEE,0x1A,0x34}"
// {0,0,0,0,0,0} disables the filter (accept all). Bring-up measurements 2026-07-29: with
// the filter off, ambient traffic adds ~20 extra CSI/s on top of the beacon's 100/s, and
// ~0.03% of ambient frames contain the 2-byte magic in their payload by chance, yielding
// garbage seq values (the 4-byte header check below cuts that, the MAC filter kills it).
#ifndef BEACON_MAC_BYTES
#define BEACON_MAC_BYTES {0, 0, 0, 0, 0, 0}
#endif
static uint8_t BEACON_MAC[6] = BEACON_MAC_BYTES;

// ---- ring buffer (callback runs in WiFi task; loop() drains) ----
static const int RING = 32;
static const int MAX_PAIRS = 192;  // HT40 all-LTF worst case is fewer; headroom
typedef struct {
  uint32_t seq;        // from payload if visible, else 0xFFFFFFFF
  uint32_t t_us;
  int8_t rssi;
  uint8_t channel;
  uint8_t n_pairs;
  uint8_t flags;       // bit0 first_word_invalid, bit1 ht40, bit2 seq_from_payload
  int8_t iq[MAX_PAIRS * 2];
} csi_rec_t;
static volatile int head = 0, tail = 0;
static csi_rec_t ring[RING];
static volatile uint32_t dropped = 0, received = 0, seq_locked = 0, runts = 0, badpkt = 0;
// Learn-mode bookkeeping: a latch that cannot be released would strand a listener on a
// beacon board that has been unplugged, which is the same silent rx=0 failure the latch
// was meant to prevent. So a LEARNED mac (never a compile-pinned one) is released after
// BEACON_TIMEOUT_MS without a seq lock, and the listener re-learns whichever beacon is on
// the air now. Swapping beacon boards therefore needs no reflash and no power cycle.
static const uint32_t BEACON_TIMEOUT_MS = 5000;
static bool beacon_pinned = false;          // set in setup(), before any learning
static volatile uint32_t last_lock_ms = 0;

static bool mac_is_zero(const uint8_t *m) {
  static const uint8_t zero[6] = {0};
  return memcmp(m, zero, 6) == 0;
}

// All-zero BEACON_MAC means LEARN MODE: accept everything until a frame turns up whose
// payload carries a valid wisent header, then latch that transmitter's MAC and filter on
// it from then on. Swapping the beacon board no longer means reflashing every listener —
// which is a real workflow hazard, because a pinned listener facing a new beacon goes
// silently deaf (rx=0) and looks identical to a broken radio.
// Latching is safe because the header match is 4 bytes, not 2: chance matches in ambient
// traffic run ~1 in 4 billion per byte offset, versus ~0.03% of frames for the bare magic.
static bool mac_match(const uint8_t *a, const uint8_t *b) {
  if (mac_is_zero(b)) return true;
  return memcmp(a, b, 6) == 0;
}

// Deliberately empty. Its existence — not its body — is what makes promiscuous mode
// deliver packets, and therefore what makes CSI callbacks happen at all. Do no work
// here: it runs in the WiFi task on every single frame in the air.
static void promisc_cb(void *buf, wifi_promiscuous_pkt_type_t type) {}

static void csi_cb(void *ctx, wifi_csi_info_t *info) {
  if (!info || !info->buf || !mac_match(info->mac, BEACON_MAC)) return;
  // Reject receptions the PHY flags as errored (rx_state != 0). The CSI callback fires
  // even for FCS-failed frames, and their payload bytes are garbage — the UART CRC is
  // then computed over already-corrupted RAM and happily validates it. Measured
  // 2026-07-29: ~0.01% of frames (10 in 117k, all at weak RSSI during operator motion)
  // arrived "CRC-valid" with random seq values before this check.
  if (info->rx_ctrl.rx_state != 0) { badpkt++; return; }
  // A zero/odd-length CSI buffer would frame as n_pairs = 0, which the host rejects as
  // an implausible header. Drop it here and count it instead of emitting a bad frame.
  if (info->len < 2) { runts++; return; }
  received++;
  int next = (head + 1) % RING;
  if (next == tail) { dropped++; return; }
  csi_rec_t *r = &ring[head];

  int pairs = info->len / 2;
  if (pairs > MAX_PAIRS) pairs = MAX_PAIRS;
  memcpy(r->iq, info->buf, pairs * 2);
  r->n_pairs = (uint8_t)pairs;
  r->t_us = (uint32_t)micros();
  r->rssi = info->rx_ctrl.rssi;
  r->channel = info->rx_ctrl.channel;
  r->flags = 0;
  if (info->first_word_invalid) r->flags |= 0x01;
  // rx_ctrl.cwb confirmed present in arduino-esp32 3.3.11 / IDF v5 for ESP32-S3
  // (esp_wifi_types_native.h: "Channel Bandwidth of the packet. 0: 20MHz; 1: 40MHz").
  if (info->rx_ctrl.cwb) r->flags |= 0x02;

  // Beacon seq lives in the ESP-NOW payload. wifi_csi_info_t exposes ->payload on
  // recent IDF v5; ESP-NOW vendor frames put our struct at a fixed offset.
  // VERIFIED 2026-07-29 on ESP32-S3: this scan locks. With the beacon at MCS0-LGI, 99.7%
  // of transmitted beacons produced a seq-tagged CSI frame at the listener. The magic is
  // found within the first 48 bytes of info->payload, so the 48-byte scan window holds.
  r->seq = 0xFFFFFFFFUL;
#ifdef WISENT_TRY_PAYLOAD_SEQ
  if (info->payload && info->payload_len >= 8) {
    for (int off = 0; off + 8 <= info->payload_len && off < 48; off++) {
      // Match the full 4-byte wisent_beacon_t header {magic LE, version=1, tx_node_id=0},
      // not just the magic. Measured 2026-07-29 with the MAC filter off: ~0.03% of
      // ambient frames contained the bare 2-byte magic by chance, producing garbage seq
      // values (e.g. spans of 4.2e9). Four bytes cuts that false-match rate 65536-fold.
      if (info->payload[off] == 0x1D && info->payload[off + 1] == 0xC5 &&
          info->payload[off + 2] == 0x01 && info->payload[off + 3] == 0x00) {
        memcpy((void *)&r->seq, info->payload + off + 4, 4);  // wisent_beacon_t.seq
        r->flags |= 0x04;
        seq_locked++;
        if (mac_is_zero(BEACON_MAC)) memcpy(BEACON_MAC, info->mac, 6);  // learn & latch
        last_lock_ms = millis();
        break;
      }
    }
  }
#endif
  head = next;
}

static uint8_t crc8(const uint8_t *d, size_t n) {  // poly 0x07
  uint8_t c = 0;
  while (n--) {
    c ^= *d++;
    for (int i = 0; i < 8; i++) c = (c & 0x80) ? (uint8_t)((c << 1) ^ 0x07) : (uint8_t)(c << 1);
  }
  return c;
}

// Report every radio call. A silently-failing esp_wifi_* call looks exactly like "no one
// is transmitting", and that ambiguity costs hours on the bench.
#define WCHK(call) do { esp_err_t _e = (call); \
  if (_e != ESP_OK) Serial.printf("# ERR %s -> %d (%s)\n", #call, (int)_e, esp_err_to_name(_e)); \
} while (0)

void setup() {
  Serial.begin(921600);
  beacon_pinned = !mac_is_zero(BEACON_MAC);
  last_lock_ms = millis();
  WiFi.mode(WIFI_STA);
  WiFi.disconnect();
  // Order matters: promiscuous must be ON before the channel is set, or the channel does
  // not stick on an unassociated STA.
  WCHK(esp_wifi_set_promiscuous(true));
  // A promiscuous RX callback MUST be registered even though we ignore the packets.
  // Without one, promiscuous mode accepts nothing, the CSI callback never fires, and the
  // symptom is a perfectly healthy-looking listener reporting rx=0 forever. Measured
  // 2026-07-29: registering this no-op is the difference between 0 and ~100 CSI/s.
  WCHK(esp_wifi_set_promiscuous_rx_cb(promisc_cb));
  wifi_promiscuous_filter_t pf = {.filter_mask = WIFI_PROMIS_FILTER_MASK_ALL};
  WCHK(esp_wifi_set_promiscuous_filter(&pf));
  WCHK(esp_wifi_set_channel(WISENT_CHANNEL, WIFI_SECOND_CHAN_NONE));

  wifi_csi_config_t cfg = {};
  cfg.lltf_en = true;
  cfg.htltf_en = true;
  cfg.stbc_htltf2_en = false;
  cfg.ltf_merge_en = false;      // keep LTFs separate
  cfg.channel_filter_en = false; // subcarrier independence is load-bearing
  cfg.manu_scale = false;
  // NOTE: on ESP32-C5/C6 this struct is wifi_csi_acquire_config_t instead — see roadmap M5.
  WCHK(esp_wifi_set_csi_config(&cfg));
  WCHK(esp_wifi_set_csi_rx_cb(csi_cb, NULL));
  WCHK(esp_wifi_set_csi(true));

  uint8_t ch = 0; wifi_second_chan_t sec;
  esp_wifi_get_channel(&ch, &sec);
  Serial.printf("# wisent listener up: node=%u want_ch=%u actual_ch=%u\n",
                (unsigned)NODE_ID, (unsigned)WISENT_CHANNEL, (unsigned)ch);
}

void loop() {
  static uint8_t out[16 + MAX_PAIRS * 2 + 1];
  while (tail != head) {
    csi_rec_t *r = &ring[tail];
    size_t n = 16 + (size_t)r->n_pairs * 2;
    out[0] = 0x1D; out[1] = 0xC5;           // magic 0xC51D LE
    out[2] = 1;                              // version
    out[3] = NODE_ID;
    memcpy(out + 4, (const void *)&r->seq, 4);
    memcpy(out + 8, (const void *)&r->t_us, 4);
    out[12] = (uint8_t)r->rssi;
    out[13] = r->channel;
    out[14] = r->n_pairs;
    out[15] = r->flags;
    memcpy(out + 16, (const void *)r->iq, (size_t)r->n_pairs * 2);
    out[n] = crc8(out + 2, n - 2);
    Serial.write(out, n + 1);
    tail = (tail + 1) % RING;
  }
  // Measured 2026-07-29 on ESP32-S3 @921600: the link sustains 72.1 kB/s unpaced —
  // ~6x one HT20 listener (12.1 kB/s) and ~3x HT40 (23.3 kB/s). Batching is therefore
  // NOT needed at 100 Hz on this hardware; revisit only if a slower bridge shows up.
  // Release a stale learned latch so a swapped-out beacon cannot strand this listener.
  if (!beacon_pinned && !mac_is_zero(BEACON_MAC) &&
      millis() - last_lock_ms > BEACON_TIMEOUT_MS) {
    memset(BEACON_MAC, 0, 6);
    last_lock_ms = millis();
    Serial.println("\n# beacon lost - re-learning");
  }

  // Stats as ASCII comment lines. The host resyncs past them (validated in
  // scripts/validate_protocol.py) and does not charge them to the CRC error budget.
  // seq= is the one to watch during bring-up: if it stays 0 the payload offset scan
  // failed, there is no system clock, and multi-node imaging is off the table.
  static uint32_t last = 0;
  if (millis() - last > 5000) {
    last = millis();
    Serial.printf("\n# rx=%lu drop=%lu seq=%lu runt=%lu badpkt=%lu beacon=%02X:%02X:%02X:%02X:%02X:%02X\n",
                  (unsigned long)received, (unsigned long)dropped,
                  (unsigned long)seq_locked, (unsigned long)runts,
                  (unsigned long)badpkt,
                  BEACON_MAC[0], BEACON_MAC[1], BEACON_MAC[2],
                  BEACON_MAC[3], BEACON_MAC[4], BEACON_MAC[5]);
  }
}
