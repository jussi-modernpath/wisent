// wisent node — unified round-robin TDM transceiver (roadmap M3).
// Every node both TRANSMITS and RECEIVES, so K nodes give K(K-1)/2 links instead of the
// star topology's K-1. Target: arduino-esp32 v3.x (ESP-IDF v5), ESP32-S3.
//   arduino-cli compile --fqbn esp32:esp32:esp32s3:CDCOnBoot=default \
//       --build-property "build.extra_flags=-DNODE_ID=1" firmware/node
//
// NODE_ID 0 is the MASTER: it owns the round counter, which is the system clock. Slaves
// hear the master's frame and transmit in their own slot, stamping the SAME round number
// — so every transmission in a round shares one index and the host can align all links on
// it without any cross-node clock discipline.
//
//   round N:  [slot 0: master]  [slot 1: node 1]  [slot 2: node 2]  [slot 3: node 3]
//             |<------------------- 1/ROUND_HZ ------------------->|
//
// RATE BUDGET (why ROUND_HZ defaults to 50, not 100). Each node transmits once per round,
// so EVERY LINK is sampled at the full round rate — but each receiver then streams
// (N_NODES-1) frames per round. Measured UART ceiling on this hardware is 72.1 kB/s:
//   128-pair frames: 50 Hz -> 41 kB/s OK | 75 Hz -> 62 kB/s over | 100 Hz -> 82 kB/s over
//   64-pair frames:  100 Hz -> 44 kB/s OK
// 50 Hz gives Nyquist 25 Hz = |v.g| < 3.08 m/s, comfortably past the ~16 Hz a person
// walking head-on produces. Raise ROUND_HZ only if you also shrink the CSI width.
//
// Protocol v2 (docs/architecture.md §Protocol): frames carry tx_id, header is 17 bytes.
// Payload version is 2, so v1 beacon frames from the old firmware are ignored outright.

#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>

#ifndef NODE_ID
#define NODE_ID 0          // 0 = master/clock; 1..N_NODES-1 = slaves. UNIQUE PER BOARD.
#endif
#ifndef N_NODES
#define N_NODES 4          // slots reserved; an unflashed slot is simply idle airtime
#endif
#ifndef ROUND_HZ
#define ROUND_HZ 50
#endif
#ifndef TX_NONHT
#define TX_NONHT 0
#endif
#ifndef WISENT_CHANNEL
#define WISENT_CHANNEL 6
#endif

// ---- wireless backhaul (protocol v3, optional; BACKHAUL=0 keeps v2 behaviour) ---------
// Only ONE node needs a USB cable. Remote nodes ship their already-formatted USB frames to
// the GATEWAY over ESP-NOW unicast, and the gateway writes those bytes to USB verbatim —
// so the host wire format is UNCHANGED and csi_io needs no modification. The link key
// (tx_id, node_id) already says which node measured a frame, so a relayed frame is
// self-describing; nothing about it is second-class.
//
// Why this fits (measured, not assumed):
//   radio : 4 remote nodes x 4 links x 50 Hz x 275 B = 220 kB/s. At MCS5 that is one
//           ~330 us packet per node per round -> ~7% channel duty. ESP-NOW v2 carries
//           1470 B/packet, so five whole frames ride in one packet.
//   USB   : gateway emits its own 55 kB/s + 220 kB/s relayed = 275 kB/s. Measured host
//           link: 96.8 kB/s @921600 (too slow), 318 kB/s @3M, 423 kB/s @4M, both with
//           ZERO corrupt bytes over multi-MB checks. Hence SERIAL_BAUD 4000000.
// Sensing beacons stay locked at MCS0-LGI: rate config is PER PEER, so backhaul running
// at MCS5 cannot disturb the calibration invariant the CSI depends on.
#ifndef GATEWAY_ID
#define GATEWAY_ID 0       // the one node with a USB cable
#endif
#ifndef BACKHAUL
#define BACKHAUL 0         // 1 = relay to gateway over the air; 0 = every node on USB (v2)
#endif
#ifndef BACKHAUL_RATE
// MCS0-LGI, the SAME modulation as the sensing beacons — deliberately, not lazily.
// Measured 2026-07-29: with backhaul at MCS5 the -77 dBm node relayed 5.6 frames/s while
// its 8-byte MCS0 beacons arrived at 58.8/s from the identical radio. A 1400-byte MCS5
// packet needs ~-70 dBm; an 8-byte MCS0 one survives far below that. Rate, not distance,
// was the whole failure. Capacity says we can afford robustness: one node's round of CSI
// is ~1100 B = 1.35 ms at 6.5 Mbps, so four remotes need 5.4 ms of the 14.5 ms backhaul
// phase — 2.7x headroom. Speed we do not need cost us 90% of a link's data.
#define BACKHAUL_RATE WIFI_PHY_RATE_MCS0_LGI
#endif
#ifndef SERIAL_BAUD
#define SERIAL_BAUD 4000000
#endif

// Accept any transmitter id up to MAX_NODES, independent of N_NODES (which only sets
// slot timing). Gating acceptance on N_NODES meant adding a 5th node required reflashing
// every existing node before they would even record it — a needless fleet-wide update.
#define MAX_NODES 16
static const uint32_t ROUND_US = 1000000UL / ROUND_HZ;
static const uint32_t SLOT_US = ROUND_US / N_NODES;
static const uint8_t WISENT_VERSION = 2;

// With backhaul the round splits in two: a SENSING phase where every node emits its short
// beacon, then a BACKHAUL phase where each remote node gets an exclusive window to ship
// frames. The split is what keeps backhaul from blinding a node during someone else's
// beacon — a radio cannot receive while it transmits, and unslotted 1470-byte packets
// would overlap beacons ~11% of the time, i.e. an 11% CSI loss.
static const uint32_t SENSE_SLOT_US = 1000;
static const uint32_t SENSE_PHASE_US = SENSE_SLOT_US * N_NODES + 500;
static const uint32_t BH_SLOT_US =
    (ROUND_US > SENSE_PHASE_US && N_NODES > 1) ? (ROUND_US - SENSE_PHASE_US) / (N_NODES - 1) : 0;
#if BACKHAUL
static const uint32_t TX_OFFSET_US = SENSE_SLOT_US;   // sensing beacons packed up front
#else
static const uint32_t TX_OFFSET_US = SLOT_US;         // v2: beacons spread over the round
#endif

typedef struct __attribute__((packed)) {
  uint16_t magic;      // 0xC51D
  uint8_t version;     // 2 = TDM
  uint8_t tx_node_id;  // who transmitted this
  uint32_t seq;        // ROUND number — identical for every TX within one round
} wisent_tdm_t;

static const uint8_t BCAST[6] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};

#if BACKHAUL
// Backhaul packet: 10-byte header + a run of WHOLE USB frames.
//   0..1 magic 0x5A 0xA5 | 2 version | 3 src node | 4..7 packet seq | 8..9 payload bytes
// Whole frames only: a lost packet then costs whole frames instead of desynchronising the
// host's byte stream mid-frame. Packet seq gives honest per-source loss accounting.
static const uint8_t BH_MAGIC0 = 0x5A, BH_MAGIC1 = 0xA5;
static const size_t BH_HDR = 10;
static const size_t BH_MAX = 1400;              // ESP-NOW v2 ceiling is 1470
static const size_t INBOX = 49152;              // gateway: ~0.18 s of relayed bytes
static uint8_t inbox[INBOX];
static volatile size_t ib_head = 0, ib_tail = 0;
static uint8_t gw_mac[6];
static volatile bool gw_seen = false;
static bool gw_ready = false;
static uint32_t bh_tx_seq = 0, bh_sent = 0, bh_fail = 0;
static volatile uint32_t bh_acked = 0, bh_noack = 0;
static volatile uint32_t bh_relayed = 0, bh_overflow = 0, bh_lost = 0, bh_rejected = 0;
static volatile uint32_t bh_last_seq[MAX_NODES];
#endif

// ---- ring buffer (CSI callback runs in the WiFi task; loop() drains) ----
static const int RING = 48;
static const int MAX_PAIRS = 192;
typedef struct {
  uint32_t seq;        // round number
  uint32_t t_us;
  int8_t rssi;
  uint8_t channel;
  uint8_t n_pairs;
  uint8_t flags;       // bit0 first_word_invalid, bit1 ht40, bit2 seq_from_payload
  uint8_t tx_id;       // WHICH node transmitted — the other half of the link key
  int8_t iq[MAX_PAIRS * 2];
} csi_rec_t;
static volatile int head = 0, tail = 0;
static csi_rec_t ring[RING];
static volatile uint32_t dropped = 0, received = 0, runts = 0, badpkt = 0, foreign = 0;
static volatile uint32_t rx_from[MAX_NODES];

// ---- round synchronisation (written in the WiFi task, read in loop) ----
static volatile uint32_t cur_round = 0;
static volatile uint32_t master_us = 0;   // micros() when the master's frame arrived
static volatile bool have_sync = false;
static volatile bool sent_this_round = false;
static uint32_t tx_count = 0, missed_rounds = 0, coasted = 0;
// A slave that only advances its round on a HEARD master frame loses the entire round
// for every missed master frame — and that cost lands on every link from this slave, on
// top of ordinary per-link loss. So slaves COAST: free-run the round clock between master
// frames (±20 ppm crystal drift is ~±20 us over 1 s, against a 5 ms slot) and snap back
// to the master's round number on the next heard frame. Coasting is BOUNDED: after
// MAX_COAST self-advanced rounds without hearing the master, stop and wait for resync —
// an unbounded free-run would let a dead master leave every slave inventing its own
// diverging clock, which is worse than silence.
static const uint32_t MAX_COAST = 3;
static volatile uint32_t coast_run = 0;

// Deliberately empty: its EXISTENCE is what makes promiscuous mode deliver packets, and
// therefore what makes CSI callbacks fire at all. Do no work here.
static void promisc_cb(void *buf, wifi_promiscuous_pkt_type_t type) {}

static void csi_cb(void *ctx, wifi_csi_info_t *info) {
  if (!info || !info->buf) return;
  if (info->rx_ctrl.rx_state != 0) { badpkt++; return; }   // FCS-failed: payload is garbage
  if (info->len < 2) { runts++; return; }
#if BACKHAUL
  // Reject backhaul traffic from the SENSING path by length. This is not optional: a
  // backhaul packet CONTAINS relayed frames that begin with the very {0x1D,0xC5,ver,tx}
  // header the scan below looks for, so without a length gate the scan would match relayed
  // bytes and inject phantom CSI records — and, worse, corrupt the round clock. Beacons
  // are 8-byte payloads; backhaul packets are >1 kB, so the discriminator is exact rather
  // than probabilistic.
  if (info->payload_len > 200) { bh_rejected++; return; }
#endif

  // Identify the transmitter from the payload. No MAC filter is needed or wanted: with
  // K transmitters there is no single MAC to pin, and matching the 4-byte header
  // {magic, version, tx_id} is both stricter and topology-independent. Anything without
  // it is ambient traffic and is dropped here rather than wasting UART.
  int tx = -1;
  uint32_t rnd = 0;
  if (info->payload && info->payload_len >= 8) {
    for (int off = 0; off + 8 <= info->payload_len && off < 48; off++) {
      if (info->payload[off] == 0x1D && info->payload[off + 1] == 0xC5 &&
          info->payload[off + 2] == WISENT_VERSION &&
          info->payload[off + 3] < MAX_NODES) {
        tx = info->payload[off + 3];
        memcpy(&rnd, info->payload + off + 4, 4);
        break;
      }
    }
  }
  if (tx < 0) { foreign++; return; }
  if (tx == NODE_ID) return;                 // never our own echo

  // Master frame => authoritative round start; also clears any coasting run.
  if (tx == 0) {
    if (rnd != cur_round) sent_this_round = false;
    cur_round = rnd;
    master_us = (uint32_t)micros();
    have_sync = true;
    coast_run = 0;
  }

  received++;
  rx_from[tx]++;
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
  r->tx_id = (uint8_t)tx;
  r->seq = rnd;
  r->flags = 0x04;                            // seq always comes from the payload here
  if (info->first_word_invalid) r->flags |= 0x01;
  if (info->rx_ctrl.cwb) r->flags |= 0x02;
  head = next;
}

#if BACKHAUL
// ESP-NOW receive path. Two jobs: learn the gateway's MAC from its own sensing beacon (so
// no MAC is ever hardcoded or configured), and, on the gateway, queue relayed bytes.
static void on_espnow_recv(const esp_now_recv_info_t *info, const uint8_t *data, int len) {
  if (!info || !data || len < 4) return;
  if (!gw_seen && NODE_ID != GATEWAY_ID && len == (int)sizeof(wisent_tdm_t) &&
      data[0] == 0x1D && data[1] == 0xC5 && data[2] == WISENT_VERSION &&
      data[3] == GATEWAY_ID) {
    memcpy(gw_mac, info->src_addr, 6);
    gw_seen = true;                       // peer is added in loop(); not from this context
    return;
  }
  if (NODE_ID != GATEWAY_ID) return;
  if (len < (int)BH_HDR || data[0] != BH_MAGIC0 || data[1] != BH_MAGIC1) return;
  uint16_t n = 0; memcpy(&n, data + 8, 2);
  if ((size_t)len < BH_HDR + n) return;
  uint8_t src = data[3];
  uint32_t sq = 0; memcpy(&sq, data + 4, 4);
  if (src < MAX_NODES) {
    uint32_t prev = bh_last_seq[src];
    if (prev && sq > prev + 1) bh_lost += sq - prev - 1;   // honest loss accounting
    bh_last_seq[src] = sq;
  }
  for (uint16_t i = 0; i < n; i++) {
    size_t nx = (ib_head + 1) % INBOX;
    if (nx == ib_tail) { bh_overflow++; return; }
    inbox[ib_head] = data[BH_HDR + i];
    ib_head = nx;
  }
  bh_relayed++;
}
#endif

static uint8_t crc8(const uint8_t *d, size_t n) {  // poly 0x07
  uint8_t c = 0;
  while (n--) {
    c ^= *d++;
    for (int i = 0; i < 8; i++) c = (c & 0x80) ? (uint8_t)((c << 1) ^ 0x07) : (uint8_t)(c << 1);
  }
  return c;
}

// Format one CSI record into the on-the-wire USB frame. Used BOTH for direct USB output
// and for backhaul payloads — one formatter means a relayed frame is byte-identical to a
// direct one, which is why the host parser needs no notion of relaying.
static size_t format_frame(const csi_rec_t *r, uint8_t *out, uint8_t rx_node) {
  size_t n = 17 + (size_t)r->n_pairs * 2;
  out[0] = 0x1D; out[1] = 0xC5;
  out[2] = WISENT_VERSION;
  out[3] = rx_node;                          // receiver
  memcpy(out + 4, (const void *)&r->seq, 4);
  memcpy(out + 8, (const void *)&r->t_us, 4);
  out[12] = (uint8_t)r->rssi;
  out[13] = r->channel;
  out[14] = r->n_pairs;
  out[15] = r->flags;
  out[16] = r->tx_id;                        // link key is (tx_id, node_id)
  memcpy(out + 17, (const void *)r->iq, (size_t)r->n_pairs * 2);
  out[n] = crc8(out + 2, n - 2);
  return n + 1;
}

#if BACKHAUL
// Ship buffered frames to the gateway inside this node's backhaul window. The CSI ring is
// the outbox: frames are formatted straight into the packet, so no second buffer exists to
// get out of sync with it.
static void maybe_backhaul() {
  if (NODE_ID == GATEWAY_ID || !gw_ready || !have_sync || BH_SLOT_US == 0) return;
  uint32_t phase = (uint32_t)micros() - master_us;
  int idx = (NODE_ID > GATEWAY_ID) ? NODE_ID - 1 : NODE_ID;
  uint32_t start = SENSE_PHASE_US + (uint32_t)idx * BH_SLOT_US;
  if (phase < start || phase > start + BH_SLOT_US) return;
  if (tail == head) return;

  static uint8_t pkt[BH_HDR + BH_MAX];
  size_t n = 0;
  int t0 = tail;                             // rollback point: see the send check below
  int t = tail;
  while (t != head) {
    csi_rec_t *r = &ring[t];
    size_t need = 17 + (size_t)r->n_pairs * 2 + 1;
    if (n + need > BH_MAX) break;            // whole frames only
    n += format_frame(r, pkt + BH_HDR + n, NODE_ID);
    t = (t + 1) % RING;
  }
  if (n == 0) return;
  pkt[0] = BH_MAGIC0; pkt[1] = BH_MAGIC1;
  pkt[2] = WISENT_VERSION + 1;               // 3 = backhaul
  pkt[3] = NODE_ID;
  bh_tx_seq++;
  memcpy(pkt + 4, &bh_tx_seq, 4);
  uint16_t n16 = (uint16_t)n;
  memcpy(pkt + 8, &n16, 2);
  // Only consume the ring once the frames are actually QUEUED. The first version advanced
  // `tail` while packing, so an ESP_ERR_ESPNOW_NO_MEM (which is exactly what a weak link's
  // retry backlog produces) silently destroyed those frames — data loss with no counter
  // attached to it. Now a failed send leaves the ring intact: the frames are retried next
  // window, and if the ring genuinely overflows it is `drop` that reports it, honestly.
  if (esp_now_send(gw_mac, pkt, BH_HDR + n) == ESP_OK) {
    tail = t;
    bh_sent++;
  } else {
    tail = t0;
    bh_fail++;
  }
}

// Queuing is not delivery: esp_now_send returning ESP_OK only means the packet entered the
// TX queue. The send callback is where the MAC reports whether it was ACKed, and the gap
// between the two is precisely what a marginal link looks like.
static void on_espnow_sent(const wifi_tx_info_t *info, esp_now_send_status_t status) {
  if (status == ESP_NOW_SEND_SUCCESS) bh_acked++; else bh_noack++;
}
#endif

static void transmit(uint32_t round_no) {
  static wisent_tdm_t pkt = {0xC51D, WISENT_VERSION, NODE_ID, 0};
  pkt.seq = round_no;
  if (esp_now_send(BCAST, (uint8_t *)&pkt, sizeof(pkt)) == ESP_OK) tx_count++;
}

void setup() {
  // Default TX buffer is smaller than one frame, so every write blocks until the UART
  // drains. 4 kB queues ~15 frames and lets Serial.write return immediately at our
  // offered load, which is what keeps slot timing honest.
  // A 274-byte frame takes ~0.9 ms to clock out even at 4 Mbaud, so a small TX buffer
  // makes every write block and wrecks slot timing. The gateway carries 5x the traffic of
  // a v2 node, hence the larger buffer there.
#if BACKHAUL
  Serial.setTxBufferSize(NODE_ID == GATEWAY_ID ? 16384 : 4096);
#else
  Serial.setTxBufferSize(4096);
#endif
  Serial.begin(SERIAL_BAUD);
  WiFi.mode(WIFI_STA);
  WiFi.disconnect();

  // Promiscuous BEFORE the channel, and a callback WITH it, or CSI never fires.
  esp_wifi_set_promiscuous(true);
  esp_wifi_set_promiscuous_rx_cb(promisc_cb);
  wifi_promiscuous_filter_t pf = {.filter_mask = WIFI_PROMIS_FILTER_MASK_ALL};
  esp_wifi_set_promiscuous_filter(&pf);
  esp_wifi_set_channel(WISENT_CHANNEL, WIFI_SECOND_CHAN_NONE);
  esp_wifi_set_max_tx_power(78);

  wifi_csi_config_t cfg = {};
  cfg.lltf_en = true;
  cfg.htltf_en = true;
  cfg.stbc_htltf2_en = false;
  cfg.ltf_merge_en = false;
  cfg.channel_filter_en = false;   // subcarrier independence is load-bearing
  cfg.manu_scale = false;
  esp_wifi_set_csi_config(&cfg);
  esp_wifi_set_csi_rx_cb(csi_cb, NULL);
  esp_wifi_set_csi(true);

  if (esp_now_init() != ESP_OK) { Serial.println("# esp_now_init failed"); ESP.restart(); }
  esp_now_peer_info_t peer = {};
  memcpy(peer.peer_addr, BCAST, 6);
  peer.channel = WISENT_CHANNEL;
  peer.ifidx = WIFI_IF_STA;
  peer.encrypt = false;
  esp_now_add_peer(&peer);

  // Lock the PHY rate to MCS0-LGI. Without it ESP-NOW sends 802.11b DSSS, which has no
  // OFDM training field and therefore produces NO CSI at the receiver at all.
  // TX_NONHT=1 forces 6 Mbps ERP-OFDM (non-HT) instead of MCS0-LGI (HT20). This is what
  // an AP admin's "minimum basic rate = 6" does to a beacon, so it lets us TEST whether
  // non-HT OFDM really yields CSI — the load-bearing step of the beacon-harvest revival
  // path (docs/dsss-compatibility.md 1, step 4).
  esp_now_rate_config_t rate = {};
#if TX_NONHT
  rate.phymode = WIFI_PHY_MODE_11G;
  rate.rate = WIFI_PHY_RATE_6M;
#else
  rate.phymode = WIFI_PHY_MODE_HT20;
  rate.rate = WIFI_PHY_RATE_MCS0_LGI;
#endif
  esp_err_t rc = esp_now_set_peer_rate_config(BCAST, &rate);
#if BACKHAUL
  esp_now_register_recv_cb(on_espnow_recv);
  if (NODE_ID != GATEWAY_ID) esp_now_register_send_cb(on_espnow_sent);
#endif

  uint8_t mac[6] = {0};
  esp_wifi_get_mac(WIFI_IF_STA, mac);
  uint8_t ch = 0; wifi_second_chan_t sec;
  esp_wifi_get_channel(&ch, &sec);
  Serial.printf("\n# wisent TDM node %u/%u %s | round %u Hz, slot %lu us | ch %u | "
                "mac %02X:%02X:%02X:%02X:%02X:%02X | rate_cfg %d\n",
                (unsigned)NODE_ID, (unsigned)N_NODES,
                NODE_ID == 0 ? "MASTER" : "slave",
                (unsigned)ROUND_HZ, (unsigned long)SLOT_US, (unsigned)ch,
                mac[0], mac[1], mac[2], mac[3], mac[4], mac[5], (int)rc);
#if BACKHAUL
  Serial.printf("# backhaul ON: gateway=%u, role=%s, baud=%lu, sense_phase=%lu us, "
                "bh_slot=%lu us\n", (unsigned)GATEWAY_ID,
                NODE_ID == GATEWAY_ID ? "GATEWAY (relays to USB)" : "remote (relays by air)",
                (unsigned long)SERIAL_BAUD, (unsigned long)SENSE_PHASE_US,
                (unsigned long)BH_SLOT_US);
#endif
}

// Slot scheduling must be checked BETWEEN frame writes, not once per loop(): a 274-byte
// Serial.write blocks ~3 ms at 921600, and draining a full ring would otherwise overrun
// the slot entirely. Measured before this fix: slaves missed 62% of their slots.
static void maybe_transmit() {
  if (NODE_ID == 0) {
    // Master free-runs the round clock. Absolute scheduling, so it cannot drift.
    static uint32_t next_us = 0;
    static bool init = false;
    if (!init) { next_us = (uint32_t)micros(); init = true; }
    if ((int32_t)((uint32_t)micros() - next_us) >= 0) {
      cur_round++;
      transmit(cur_round);
      next_us += ROUND_US;
      if ((int32_t)((uint32_t)micros() - next_us) >= 0) next_us = (uint32_t)micros();  // overrun
    }
    return;
  }
  if (have_sync && coast_run < MAX_COAST &&
      (int32_t)((uint32_t)micros() - (master_us + ROUND_US)) >= 0) {
    // A full round elapsed without a master frame: advance locally (bounded).
    master_us += ROUND_US;
    cur_round++;
    sent_this_round = false;
    coast_run++;
    coasted++;
  }
  if (have_sync && !sent_this_round) {
    // Slave waits its slot offset after the master's frame, then stamps the SAME round.
    // The slot is a POLITENESS offset, not a hard deadline: loop() shares the CPU with a
    // WiFi task fielding ~500 CSI callbacks/s (ambient traffic we discard), so it cannot
    // be relied on to poll inside a 5 ms window — enforcing one cost 72% of transmissions.
    // Transmit whenever the offset has passed; ESP-NOW's CSMA handles the rare overlap.
    // `late` counts slots that slipped past their nominal end, purely as a health signal.
    uint32_t due = master_us + (uint32_t)NODE_ID * TX_OFFSET_US;
    if ((int32_t)((uint32_t)micros() - due) >= 0) {
      if ((int32_t)((uint32_t)micros() - (due + TX_OFFSET_US)) >= 0) missed_rounds++;
      transmit(cur_round);
      sent_this_round = true;
    }
  }
}

void loop() {
  maybe_transmit();

#if BACKHAUL
  // Register the gateway as a unicast peer once its beacon has revealed its MAC. Unicast
  // (not broadcast) so the MAC layer ACKs and retries backhaul for us.
  if (gw_seen && !gw_ready) {
    esp_now_peer_info_t gp = {};
    memcpy(gp.peer_addr, gw_mac, 6);
    gp.channel = WISENT_CHANNEL;
    gp.ifidx = WIFI_IF_STA;
    gp.encrypt = false;
    if (esp_now_add_peer(&gp) == ESP_OK) {
      esp_now_rate_config_t br = {};
      br.phymode = WIFI_PHY_MODE_HT20;
      br.rate = BACKHAUL_RATE;               // per-peer: beacons stay at MCS0-LGI
      esp_now_set_peer_rate_config(gw_mac, &br);
      gw_ready = true;
      Serial.printf("# gateway peer %02X:%02X:%02X:%02X:%02X:%02X ready\n",
                    gw_mac[0], gw_mac[1], gw_mac[2], gw_mac[3], gw_mac[4], gw_mac[5]);
    }
  }
  maybe_backhaul();

  // Gateway: relay received bytes to USB verbatim — they are already whole USB frames.
  if (NODE_ID == GATEWAY_ID) {
    static uint8_t chunk[512];
    while (ib_tail != ib_head) {
      size_t n = 0;
      while (ib_tail != ib_head && n < sizeof(chunk)) {
        chunk[n++] = inbox[ib_tail];
        ib_tail = (ib_tail + 1) % INBOX;
      }
      Serial.write(chunk, n);
      maybe_transmit();
    }
  }
#endif

  // ---- drain our own CSI: to USB if we have the cable, else to the air ----
  static uint8_t out[17 + MAX_PAIRS * 2 + 1];
#if BACKHAUL
  if (NODE_ID != GATEWAY_ID) {
    maybe_backhaul();          // remote nodes never write CSI to USB
  } else
#endif
  while (tail != head) {
    maybe_transmit();          // never let a long drain swallow our slot
    csi_rec_t *r = &ring[tail];
    size_t n = format_frame(r, out, NODE_ID);
    Serial.write(out, n);
    tail = (tail + 1) % RING;
  }

  static uint32_t last = 0;
  if (millis() - last > 5000) {
    last = millis();
    Serial.printf("\n# node=%u round=%lu tx=%lu rx=%lu drop=%lu late=%lu coast=%lu bad=%lu foreign=%lu from=[",
                  (unsigned)NODE_ID, (unsigned long)cur_round, (unsigned long)tx_count,
                  (unsigned long)received, (unsigned long)dropped,
                  (unsigned long)missed_rounds, (unsigned long)coasted,
                  (unsigned long)badpkt, (unsigned long)foreign);
    for (int i = 0; i < N_NODES; i++) Serial.printf("%lu%s", (unsigned long)rx_from[i],
                                                    i == N_NODES - 1 ? "" : ",");
    Serial.println("]");
#if BACKHAUL
    if (NODE_ID == GATEWAY_ID)
      Serial.printf("# bh gateway relayed=%lu lost=%lu overflow=%lu rejected=%lu\n",
                    (unsigned long)bh_relayed, (unsigned long)bh_lost,
                    (unsigned long)bh_overflow, (unsigned long)bh_rejected);
    else
      Serial.printf("# bh node=%u sent=%lu fail=%lu acked=%lu noack=%lu peer=%s\n",
                    (unsigned)NODE_ID, (unsigned long)bh_sent, (unsigned long)bh_fail,
                    (unsigned long)bh_acked, (unsigned long)bh_noack,
                    gw_ready ? "ready" : "WAITING");
#endif
  }
}
