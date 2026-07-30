// wisent beacon — ESP-NOW broadcast at 100 Hz. This node IS the system clock.
// Target: arduino-esp32 v3.x (ESP-IDF v5).
// Verified 2026-07-29 on ESP32-S3 (arduino-esp32 3.3.11), FQBN:
//   esp32:esp32:esp32s3:CDCOnBoot=default
// Measured: hz=100.00 sustained, overrun=0, sendfail=0; MAC print confirmed against
// eFuse. Not yet confirmed: that a listener actually RECEIVES these (needs a 2nd board).

#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>

static const uint8_t WISENT_CHANNEL = 6;
static const uint32_t BEACON_HZ = 100;
static const uint16_t WISENT_MAGIC = 0xC51D;

typedef struct __attribute__((packed)) {
  uint16_t magic;      // 0xC51D
  uint8_t version;     // 1
  uint8_t tx_node_id;  // 0 = beacon
  uint32_t seq;        // the system clock
} wisent_beacon_t;

static const uint8_t BCAST[6] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};
static wisent_beacon_t pkt = {WISENT_MAGIC, 1, 0, 0};

void setup() {
  Serial.begin(921600);
  WiFi.mode(WIFI_STA);
  WiFi.disconnect();  // no AP association; we only broadcast

  // Lock channel BEFORE esp_now_init. esp_wifi_set_channel() itself is confirmed working
  // on this silicon (a listener set to ch 1 reported channel==1 in every CSI frame);
  // that the channel still holds *after* esp_now_init needs a second board to observe.
  esp_wifi_set_channel(WISENT_CHANNEL, WIFI_SECOND_CHAN_NONE);
  esp_wifi_set_max_tx_power(78);  // 19.5 dBm, locked — calibration depends on it

  if (esp_now_init() != ESP_OK) {
    Serial.println("# esp_now_init failed");
    ESP.restart();
  }
  esp_now_peer_info_t peer = {};
  memcpy(peer.peer_addr, BCAST, 6);
  peer.channel = WISENT_CHANNEL;
  peer.ifidx = WIFI_IF_STA;
  peer.encrypt = false;
  esp_now_add_peer(&peer);

  // Lock the PHY rate to MCS0-LGI (HT20). This is NOT a nicety — it is what makes the
  // system work at all. ESP-NOW otherwise transmits at a low 802.11b DSSS rate, and DSSS
  // frames carry no OFDM long training field, so the receiver generates NO CSI for them.
  // Measured 2026-07-29: without this, a listener hearing ~100 beacons/s produced only
  // ~9 CSI callbacks/s (and those came from unrelated ambient traffic).
  esp_now_rate_config_t rate = {};
  rate.phymode = WIFI_PHY_MODE_HT20;
  rate.rate = WIFI_PHY_RATE_MCS0_LGI;
  rate.ersu = false;
  rate.dcm = false;
  esp_err_t rc = esp_now_set_peer_rate_config(BCAST, &rate);
  Serial.printf("# esp_now_set_peer_rate_config(MCS0_LGI/HT20) -> %d (%s)\n",
                (int)rc, esp_err_to_name(rc));

  // Print the STA MAC as a pasteable C initializer: listener.ino's BEACON_MAC filter
  // needs exactly this value, and reading it here beats sniffing for it.
  uint8_t mac[6] = {0};
  esp_wifi_get_mac(WIFI_IF_STA, mac);
  Serial.printf("# wisent beacon up, %lu Hz, ch %u\n",
                (unsigned long)BEACON_HZ, (unsigned)WISENT_CHANNEL);
  Serial.printf("# BEACON_MAC = {0x%02X, 0x%02X, 0x%02X, 0x%02X, 0x%02X, 0x%02X}"
                "  <- paste into listener.ino\n",
                mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
}

void loop() {
  static uint32_t next_us = micros();
  static uint32_t overruns = 0, send_fail = 0, last_ms = 0, last_seq = 0;
  uint32_t period = 1000000UL / BEACON_HZ;

  pkt.seq++;
  if (esp_now_send(BCAST, (uint8_t *)&pkt, sizeof(pkt)) != ESP_OK) send_fail++;

  // Drift-free pacing: schedule against absolute time, not delay().
  next_us += period;
  int32_t wait = (int32_t)(next_us - micros());
  if (wait > 0) delayMicroseconds(wait);
  else { overruns++; next_us = micros(); }  // overrun; resync

  // M1 acceptance item 1 is "100 +/- 5 Hz sustained 10 min" — measure it here rather
  // than inferring it from a scope. hz is the ACHIEVED rate over the last window,
  // which is what the acceptance test actually asks about.
  uint32_t now_ms = millis();
  if (now_ms - last_ms >= 5000) {
    float hz = 1000.0f * (pkt.seq - last_seq) / (now_ms - last_ms);
    Serial.printf("# seq=%lu hz=%.2f overrun=%lu sendfail=%lu\n",
                  (unsigned long)pkt.seq, hz,
                  (unsigned long)overruns, (unsigned long)send_fail);
    last_ms = now_ms;
    last_seq = pkt.seq;
  }
}
