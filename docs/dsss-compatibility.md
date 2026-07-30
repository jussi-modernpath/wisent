# The DSSS/CSI beacon-harvesting compatibility law

**Status: documented compatibility survey, 2026-07-29, with the mechanism's load-bearing
step verified on our own hardware (§1.1). Promotes the n=1 field finding
(`mesh-harvesting.md` §1) to a mechanism-certain, controllability-mapped result across ~20
AP platforms. Treat claims at the confidence levels stated per section.**

## The law, in one sentence

Passive WiFi sensing that reads CSI from **beacon frames** is gated by a single AP setting
— the lowest **basic (mandatory) rate** — because a 2.4 GHz beacon defaults to **1 Mbps
DSSS**, DSSS carries no OFDM training field, and ESP32-class CSI is computed *only* from
OFDM training fields; therefore beacon harvesting is **impossible until an admin forces the
beacon to an OFDM rate**, and most consumer/ISP gear does not expose that control.

## 1. Mechanism (confidence: certain — each link cited)

1. **Beacons and broadcast/management frames are sent at the BSS's lowest basic
   (mandatory) rate.** — mrncciew ("Beacons go lowest mandatory data rate configured");
   Metis.fi ("The lowest basic rate is used for all management frames — beacon, probe …
   — and for broadcasts and multicasts").
2. **With 802.11b enabled, the lowest basic rate is 1 Mbps and beacons default to 1 Mbps
   DSSS.** — mrncciew ("If you have 1Mbps as Basic/Mandatory rate on 2.4GHz … then beacons
   go that data rate").
3. **802.11b rates (1/2/5.5/11) use DSSS/CCK — single carrier, no OFDM training field;
   6 Mbps and up use OFDM, which carries L-STF/L-LTF (and HT-LTF for 802.11n).** —
   RF Wireless World; ShareTechnote WLAN PHY frames.
4. **ESP32 CSI is estimated from the LLTF / HT-LTF / STBC-HT-LTF fields only** — so a
   1 Mbps DSSS beacon yields *no* CSI, while a 6 Mbps *non-HT OFDM* beacon does (the LLTF
   is present in every OFDM PPDU). — Espressif ESP-IDF Wi-Fi vendor-features doc;
   corroborated by the jonathanmuller ESP32-CSI README ("you can't send at <6 Mb/s [and]
   there will be no CSI preamble").
5. **Disabling 802.11b (g/n-only, or minimum basic rate 6 Mbps) moves the beacon to
   6 Mbps non-HT OFDM → LLTF present → CSI-bearing.** — composition of 1+3+4; prescribed
   directly by Metis.fi.

Corollary: beacons ride *legacy* basic rates, never HT/VHT/HE-MCS — so even a WiFi-6 AP
emits a legacy beacon, DSSS or OFDM depending only on the basic-rate config. This is why
our channel-6 control run showed `sig_mode=0` with only the rate code changing.

### 1.1 Step 4 verified on hardware (2026-07-29)

Step 4 was the only link in the chain that was a *composition* of sources rather than a
measurement, and the whole revival path rests on it. We tested it directly by forcing one
wisent node to transmit at 6 Mbps ERP-OFDM instead of MCS0-LGI — which is exactly what an
admin setting "minimum basic rate = 6" does to a beacon — and reading the result at another
node:

| transmitter | PHY rate | CSI yield | I/Q pairs |
|---|---|---|---|
| nodes 1/2/3 | MCS0-LGI (HT20) | ~51–56 /s | **128** |
| node 4 | **6 Mbps ERP-OFDM (non-HT)** | **57.8 /s** | **64** |
| local mesh APs | 1 Mbps DSSS | **0** (0 of 3442 beacons) | — |

Three points on the rate→CSI curve, same silicon, same channel. Non-HT OFDM **does** yield
CSI, at half the *buffer* size — the quantitative prediction made before the test. This
also retroactively explains the interleaved 64- and 128-pair frames in ambient channel-1
traffic: non-HT and HT frames respectively.

**What that halving is, precisely — it is one training field versus two, NOT half the
frequency diversity.** A non-HT OFDM frame carries the L-LTF alone (~52 usable
subcarriers); an HT frame carries L-LTF *plus* HT-LTF (~52 + ~56). The two LTFs measure
**overlapping frequencies** — the same ~20 MHz — so the *usable subcarrier span is nearly
identical*. What a harvested link actually loses is the HT-LTF's **second, independent
channel estimate** (an SNR/averaging benefit) and a few edge subcarriers. Saying "half the
subcarriers" reads as half the diversity, and the measurement does not support that.

This matters for the tiering decision: the reason harvested links stay **slow tier** and
never feed LinkBVP is the **~10 Hz beacon rate**, not the subcarrier count. Rate is the
binding constraint; the LTF difference is second-order. (The host must still not assume a
fixed CSI width — `frames_to_segments` segments per width, and per LTF block.)

**Honest novelty scoping:** the *mechanism* (DSSS → no CSI) is folklore in the ESP32-CSI
niche — the jonathanmuller README states it plainly. The contribution here is (a) framing
it as a **deployment constraint on passive beacon-based sensing**, (b) the
**controllability survey** below mapping which real-world AP populations can be used at
all, and (c) the resulting **field-audit instrument** (`firmware/mesh_audit`) that turns
"will it work?" into a 60-second test. This is systematization + deployment-mapping, not a
claim to have discovered the physics.

## 2. Controllability survey (confidence: high — vendor docs cited; some cells unconfirmed)

Two control types matter: **(a) basic/minimum-rate control** (remove CCK from the mandatory
set → lowest basic rate becomes OFDM → beacon follows) and **(b) a direct beacon-rate
override** (only hostapd exposes this literally).

### Consumer mesh / whole-home — mostly LOCKED

| Product | Beacon off DSSS? | Control | Source |
|---|---|---|---|
| Amazon eero (all) | **No** | none exposed (cloud-managed) | eero support |
| Google Nest / Google WiFi | **No** | none exposed | Google support |
| TP-Link Deco (consumer) | **No** | only in Omada business line, not Deco | TP-Link |
| Netgear Orbi | **No / unconfirmed** | mode dropdown caps *max* rate, doesn't raise *min* | Netgear KB |
| Linksys Velop / Atlas | **No / unconfirmed** | broad mode only, no per-standard/rate control | Linksys |
| Plume / HomePass & ISP mesh | **No** | cloud-managed, no radio controls | Plume |
| **ASUS ZenWiFi / AiMesh / ASUSWRT** | **YES (direct)** | Wireless → General → **"Disable 11b"** (also Professional → Multicast Rate = OFDM 6) | ASUS FAQ; SNBForums |
| **AVM FRITZ!Box** | **YES (indirect)** | WLAN → Radio Channel → standard selector **"802.11n+g"** (excludes b) | AVM help |

ASUS caveat: on some Broadcom models "N-only" mode is *not* sufficient — the **"Disable
11b" checkbox** is the reliable control, and AiMesh may apply it per-node inconsistently.
FRITZ!Box caveat: the selector's wording/presence varies by FRITZ!OS version.

### ISP-provided gateways — LOCKED as a rule

No surveyed operator box (Xfinity/Technicolor, BT Smart Hub 2, and by pattern AT&T, Sky,
Vodafone Station, Telekom Speedport, Nordic Telia/DNA/Elisa) exposes a mode or basic-rate
control; firmware trends toward "automatic" radio management that removes it. Confirmed
concretely for Xfinity and BT; the rest is a strong pattern, not a per-box spec. Standard
workaround: bridge/modem-mode the ISP box and run your own AP.

### Prosumer / enterprise / open — CONTROLLABLE

**Read the "confirmed?" column as documentation, not measurement.** No row below has a
post-change beacon rate measured by `mesh_audit`; every one is a vendor's claim about its
own product, and vendors document controls that do not always do what they say — the ASUS
"N-only is insufficient, use Disable 11b" case is exactly that, caught once by accident.
Until a row carries an **audited** rate it is a hypothesis. The contributed table in
`firmware/mesh_audit/README.md` is where documented becomes verified.

| Platform | Control | Beacon move: **documented** | **audited?** | Source |
|---|---|---|---|
| **OpenWrt / hostapd** | `basic_rates` / `legacy_rates 0`; direct **`beacon_rate=60`** | **Yes** (`beacon_rate` needs driver support; basic-rate path always works) | **not yet** | hostapd / OpenWrt |
| Ubiquiti UniFi | "Minimum Data Rate Control"; newer "disable CCK" | Yes (basic-rate model) | **not yet** | Ubiquiti |
| MikroTik RouterOS | `basic-rates-b` / `supported-rates-b` | Yes | **not yet** | MikroTik |
| Cisco Meraki | per-SSID "Minimum bitrate" | **Yes, explicit** ("management and broadcast frames at the lowest selected rate") | **not yet** | Meraki |
| Aruba Instant / ArubaOS | `g-basic-rates` / `g-tx-rates` | Yes | **not yet** | Aruba |
| Cisco Catalyst 9800 / AireOS | `ap dot11 24ghz rate … disable/mandatory` | Yes (mandatory-rate model) | **not yet** | Cisco |
| Ruckus | "BSS Min Rate" (`bss-minrate`) | **Yes, explicit** (`mgmt-tx-rate = bss-minrate`) | **not yet** | Ruckus |
| EnGenius Cloud | "Minimum Bit Rate" per radio | **Yes, explicit** ("AP sends beacons based on the minimum bit rate") | **not yet** | EnGenius |
| TP-Link Omada (business) | "802.11 Rate Control": disable CCK + Mgmt Rate Control | **Yes, explicit** ("support rate carried in the beacon frame") | **not yet** | TP-Link Omada |
| Cambium cnPilot | `rates min-unicast` | Partial (floor governs mgmt/broadcast) | **not yet** | Cambium |

The literal `beacon_rate` override exists **only on hostapd/OpenWrt**; every commercial
platform relies on the basic-rate mechanism. Common default across enterprise gear is 11b
enabled / 1 Mbps floor — i.e. beacons are DSSS until an admin raises the floor.

## 3. The law, stated for the paper

> **Passive beacon-harvested CSI sensing on ESP32-class hardware is feasible only where the
> AP's lowest basic rate can be forced to an OFDM rate (≥ 6 Mbps).** This partitions the
> installed base sharply: **enterprise / prosumer / open (OpenWrt, UniFi, Meraki, Aruba,
> Cisco, Ruckus, MikroTik, EnGenius, Omada) = controllable**; **mainstream consumer mesh
> and ISP gateways = locked**, with **ASUS and AVM FRITZ!Box the only consumer exceptions**.
> The mechanism is certain and its OFDM branch is measured (§1.1); the *default* is DSSS
> everywhere; the constraint is therefore a per-deployment configuration fact, not a
> property of the sensor — and it is invisible until measured, which is why the 60-second
> `mesh_audit` gate exists.

## 4. Remaining honesty caveats

- Prevalence of the *default* (1 Mbps DSSS) is asserted from mechanism + vendor defaults,
  **not a field census**. The crowd-sourced table is what turns "usually" into a number.
- **The survey is documentation-based, not measurement-based.** It records what vendors
  *document*, and vendors document controls that do not always do what they say — the ASUS
  "N-only is insufficient, use Disable 11b" case is exactly that failure mode caught once.
  Every "controllable" cell is a hypothesis until someone runs `mesh_audit` after applying
  the control. The contributed table should therefore record **both** the claimed control
  **and** the measured beacon rate afterwards.
- "Couldn't confirm" cells (Orbi, Velop, some ISP boxes) are absence of evidence, not
  evidence of absence.
- Novelty is systematization and mapping, not physics discovery (§1).

## 5. Sources

Mechanism: mrncciew 802.11 beacon frame (https://mrncciew.com/2014/10/08/802-11-mgmt-beacon-frame/);
Metis.fi rates (https://metis.fi/en/2018/09/rates/); RF Wireless World CCK/DSSS/OFDM
(https://www.rfwireless-world.com/terminology/cck-vs-dsss-vs-ofdm); ShareTechnote WLAN PHY
frames; Espressif ESP-IDF Wi-Fi vendor features
(https://docs.espressif.com/projects/esp-idf/en/latest/esp32/api-guides/wifi-driver/wifi-vendor-features.html);
jonathanmuller ESP32-CSI README
(https://github.com/jonathanmuller/ESP32-gather-channel-state-information-CSI-).
Controllability: eero (support.eero.com/hc/en-us/articles/207613326); Google Nest
(support.google.com/googlehome/answer/6293481); TP-Link Omada rate control
(tp-link.com/us/support/faq/4302/); ASUS Professional FAQ (asus.com/support/faq/1011438/)
+ SNBForums disable-11b threads; AVM FRITZ!Box radio-channel help (help.avm.de);
OpenWrt/hostapd `beacon_rate` & `legacy_rates`
(forum.openwrt.org/t/how-to-set-up-beacon-data-rate/148719, /t/wireless-option-legacy-rates-0/54403);
UniFi (help.ui.com/hc/en-us/articles/32065480092951); MikroTik
(help.mikrotik.com/docs/spaces/ROS/pages/8978446); Meraki minimum-bitrate
(documentation.meraki.com/Wireless/.../Minimum_Bitrate_Control); Aruba dot11g-radio-profile
(arubanetworks.com/techdocs); Cisco Catalyst 9800 band config
(cisco.com/.../m_config_band_select_ewlc.html); Ruckus data rates
(support.wyebot.com/vendor-specific/ruckus/data-rates); EnGenius radio config
(doc.engenius.ai); Cambium min-unicast (community.cambiumnetworks.com/t/.../91237).
ISP: Xfinity forum (forums.xfinity.com/.../63aa53c7ebc7551628365876); BT Smart Hub 2
(community.bt.com/.../2315917).

*(No SSIDs / BSSIDs / MACs in this document — honesty rule 5.)*
