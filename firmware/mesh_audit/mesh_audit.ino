// wisent mesh_audit — go/no-go for AP-beacon harvesting (docs/mesh-harvesting.md §1).
//
// Per BSSID: how many BEACON frames arrive, and how many produce a CSI callback?
// A beacon sent as 802.11b DSSS (sig_mode=0, rate=0) has no OFDM training field and can
// NEVER produce CSI on this silicon — so a 0% yield column means harvesting that AP is
// impossible, not merely lossy. Fix is on the AP: disable 11b / force 11g/n-only rates.
//
// Run for ~60 s on the mesh's 2.4 GHz channel, then read the table:
//   arduino-cli compile --fqbn esp32:esp32:esp32s3:CDCOnBoot=default \
//       --build-property "build.extra_flags=-DAUDIT_CH=1" firmware/mesh_audit
//   arduino-cli upload -p <port> --fqbn esp32:esp32:esp32s3:CDCOnBoot=default firmware/mesh_audit
//
// SUCCESS CRITERION is "yield > 0 with a plausible pairs count", NOT "looks like the
// wisent beacon". A beacon forced to 6 Mbps OFDM is still non-HT, so it yields L-LTF-only
// CSI — about HALF the I/Q pairs of an 11n frame (~64 vs ~128). sig_mode stays 0; what
// changes is the rate code (0x00 = 1 Mbps DSSS -> 0x0B = 6 Mbps OFDM).
//
// SANITY CHECK: CSI TOTAL must be non-zero when pointed at a channel carrying known-good
// OFDM traffic (e.g. the wisent beacon's channel). If it is zero there too, the audit
// itself is broken and the table means nothing.
#include <WiFi.h>
#include <esp_wifi.h>
#ifndef AUDIT_CH
#define AUDIT_CH 1
#endif
#define MAXAP 12
static uint8_t macs[MAXAP][6];
static uint32_t n_beacon[MAXAP], n_csi[MAXAP], n_data[MAXAP];
static uint8_t sigmode[MAXAP], phyrate[MAXAP], mcs_[MAXAP], csipairs[MAXAP];
static int8_t rssi_[MAXAP];
static int n_ap = 0;
static uint32_t csi_total=0, csi_unmatched=0;
static uint32_t csi_sig[4]={0,0,0,0};  // u32: a u8 wraps in seconds at 100 CSI/s

static int slot(const uint8_t *m, bool create) {
  for (int i = 0; i < n_ap; i++) if (memcmp(macs[i], m, 6) == 0) return i;
  if (!create || n_ap >= MAXAP) return -1;
  memcpy(macs[n_ap], m, 6); return n_ap++;
}
static void pcb(void *buf, wifi_promiscuous_pkt_type_t t) {
  wifi_promiscuous_pkt_t *p = (wifi_promiscuous_pkt_t *)buf;
  const uint8_t *fr = p->payload;
  uint8_t type = (fr[0] >> 2) & 0x3, sub = (fr[0] >> 4) & 0xF;
  const uint8_t *a2 = fr + 10;                 // transmitter address
  if (type == 0 && sub == 8) {                 // management / beacon
    int i = slot(a2, true);
    if (i < 0) return;
    n_beacon[i]++;
    sigmode[i] = p->rx_ctrl.sig_mode; phyrate[i] = p->rx_ctrl.rate;
    mcs_[i] = p->rx_ctrl.mcs; rssi_[i] = p->rx_ctrl.rssi;
  } else if (type == 2) {                      // data
    int i = slot(a2, false);
    if (i >= 0) n_data[i]++;
  }
}
static void ccb(void *ctx, wifi_csi_info_t *info) {
  if (!info || info->rx_ctrl.rx_state != 0) return;
  csi_total++;
  if (info->rx_ctrl.sig_mode < 4) csi_sig[info->rx_ctrl.sig_mode]++;
  int i = slot(info->mac, false);              // only count APs we saw beacon from
  if (i >= 0) { n_csi[i]++; csipairs[i] = (uint8_t)(info->len / 2); } else csi_unmatched++;
}
void setup(){
  Serial.begin(921600); delay(1500);
  WiFi.mode(WIFI_STA); WiFi.disconnect(); delay(100);
  esp_wifi_set_promiscuous(true);
  esp_wifi_set_promiscuous_rx_cb(pcb);
  wifi_promiscuous_filter_t f = { .filter_mask = WIFI_PROMIS_FILTER_MASK_ALL };
  esp_wifi_set_promiscuous_filter(&f);
  esp_wifi_set_channel(AUDIT_CH, WIFI_SECOND_CHAN_NONE);
  wifi_csi_config_t c = {};
  c.lltf_en=true; c.htltf_en=true; c.stbc_htltf2_en=false;
  c.ltf_merge_en=false; c.channel_filter_en=false; c.manu_scale=false;
  esp_wifi_set_csi_config(&c); esp_wifi_set_csi_rx_cb(ccb, NULL); esp_wifi_set_csi(true);
  Serial.printf("# mesh audit on ch %d\n", AUDIT_CH);
}
void loop(){
  static uint32_t last=0, t0=millis();
  if (millis()-last > 10000) {
    last=millis();
    float secs=(millis()-t0)/1000.0f;
    Serial.printf("\n# CSI TOTAL=%lu (unmatched-to-AP=%lu) by sig_mode: 11bg=%u 11n=%u ?=%u 11ac=%u\n",
      (unsigned long)csi_total,(unsigned long)csi_unmatched,(unsigned)csi_sig[0],(unsigned)csi_sig[1],(unsigned)csi_sig[2],(unsigned)csi_sig[3]);
    Serial.printf("# t=%.0fs  BSSID              beacons  b/s   CSI  csi/s  yield rate rssi pairs  verdict\n", secs);
    for (int i=0;i<n_ap;i++) {
      // PHY rate codes 0x00-0x07 are the 802.11b DSSS set (1/2/5.5/11 Mbps, long+short
      // preamble). DSSS carries no OFDM training field, and ESP32 CSI is computed from
      // L-LTF/HT-LTF — so those beacons can NEVER produce CSI, however strong they are.
      const char *v;
      if (sigmode[i] == 0 && phyrate[i] <= 0x07) v = "DSSS - CANNOT yield CSI, fix AP basic rates";
      else if (n_csi[i] == 0)                    v = "OFDM but no CSI yet - keep watching";
      else if (sigmode[i] == 0)                  v = "OFDM non-HT - harvestable (L-LTF only)";
      else                                       v = "HT - harvestable";
      Serial.printf("# %02X:%02X:%02X:%02X:%02X:%02X  %7lu %5.1f %5lu %6.2f %5.0f%% 0x%02X %4d %5u  %s\n",
        macs[i][0],macs[i][1],macs[i][2],macs[i][3],macs[i][4],macs[i][5],
        (unsigned long)n_beacon[i], n_beacon[i]/secs, (unsigned long)n_csi[i], n_csi[i]/secs,
        n_beacon[i] ? 100.0*n_csi[i]/n_beacon[i] : 0.0,
        phyrate[i], rssi_[i], csipairs[i], v);
    }
  }
}
