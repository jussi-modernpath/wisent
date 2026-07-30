# Why your WiFi-sensing project sees nothing from your router's beacons

*A compatibility law for ESP32-class CSI, and a 60-second test for whether your
deployment can work at all.*

## The symptom

You want passive WiFi sensing. You have an ESP32, you point it at your router's channel,
you enable CSI, and you wait for the beacons — 10 a second, every second, from an access
point sitting 8 metres away at a comfortable −48 dBm.

You get **zero CSI callbacks**. Not weak ones. Zero.

Every API call returned `ESP_OK`. The beacons are arriving — a promiscuous packet counter
proves it, thousands of them, rock steady. Signal strength is excellent. Nothing in your
code is wrong.

This is worth knowing before you spend a weekend on it, because no amount of signal
processing will fix it.

## The mechanism

Five steps, and the failure is structural:

1. **Beacons are sent at the BSS's lowest *basic* (mandatory) rate** — not at the fast
   rate your laptop gets. That's true of all management and broadcast frames.
2. **If 802.11b is enabled, that lowest basic rate is 1 Mbps** — and essentially every
   consumer 2.4 GHz AP ships with 11b enabled, for range and compatibility.
3. **1 Mbps is DSSS, not OFDM.** DSSS is single-carrier. It has **no OFDM training
   field**.
4. **ESP32 CSI is computed *only* from the OFDM training fields** (L-LTF, HT-LTF). No
   training field, no channel estimate. There is nothing to compute CSI *from*.
5. Therefore: **a 1 Mbps DSSS beacon can never produce CSI, at any signal strength.**

The frames arrive perfectly and are radio-invisible to the sensor.

## The measurement

Auditing a consumer mesh network, channel 1, ~150 seconds:

```
AP        beacons   b/s   CSI   yield   sig_mode  rate   rssi
AP1-a         864   9.8     0      0%          0  0x00    -48
AP1-b         862   9.8     0      0%          0  0x00    -48
AP2-a         860   9.7     0      0%          0  0x00    -64
AP2-b         856   9.7     0      0%          0  0x00    -64
CSI TOTAL = 0
```

3442 beacons, zero CSI. `rate = 0x00` is 1 Mbps DSSS. Rate codes `0x00`–`0x07` are the
entire 802.11b set; anything in that range cannot yield CSI.

**The control matters more than the result.** Point the same sketch at a channel carrying
known OFDM traffic and CSI floods in — 3688 callbacks in 38 seconds. Without that control,
a zero is just as consistent with a broken audit as with a real finding.

And the other half of the curve, measured on the same hardware by forcing a transmitter to
each rate:

| PHY rate | CSI? | buffer |
|---|---|---|
| 1 Mbps DSSS | **none** | — |
| 6 Mbps OFDM (non-HT) | **yes** | 64 pairs |
| MCS0 (HT20) | **yes** | 128 pairs |

The non-HT case gives one training field instead of two — L-LTF only, versus L-LTF plus
HT-LTF. Those cover overlapping frequencies, so you keep almost the same usable subcarrier
span; what you lose is the second independent channel estimate, not half your frequency
diversity.

## The fix, and who can apply it

Force the AP's lowest **basic rate** to an OFDM rate: "disable 802.11b", "802.11g/n only",
"minimum bitrate 6 Mbps", or on hostapd `legacy_rates 0` / `beacon_rate=60`.

Whether you *can* is the whole ballgame, and it splits the installed base sharply:

**Controllable** — OpenWrt/hostapd, UniFi, MikroTik, Meraki, Aruba, Cisco, Ruckus,
EnGenius, TP-Link Omada. Enterprise and prosumer gear generally exposes a minimum-rate or
basic-rate control. Note the default is usually 11b-enabled anyway, so it's off until you
change it.

**Locked** — mainstream consumer mesh (eero, Google Nest, Deco, Orbi, Velop, Plume) and
essentially all ISP-supplied gateways. Cloud-managed radio settings don't include this.

**Consumer exceptions** — **ASUS** (Wireless → General → "Disable 11b"; on some Broadcom
models "N-only" mode is *not* sufficient) and **AVM FRITZ!Box** (radio-channel standard
selector "802.11n+g").

Standard workaround for a locked ISP box: bridge it and run your own AP.

There is also a band-level escape: **5 GHz has no DSSS at all.** Every 5 GHz beacon is
OFDM by construction, so on a dual-band sensing radio the problem doesn't arise — at the
cost of worse wall penetration, which matters if through-wall coverage is the point.

## Test your own deployment in 60 seconds

`firmware/mesh_audit` prints, per BSSID, beacons received versus CSI produced, plus the
PHY rate and a verdict:

- `DSSS - CANNOT yield CSI` → change the AP's basic rate, or give up on that AP
- `OFDM non-HT - harvestable` → expect ~64 pairs
- `HT - harvestable` → expect ~128 pairs

Run the control too: point it at known OFDM traffic and confirm the total is non-zero. A
silent zero is exactly the failure this is meant to make loud.

## What's solid here and what isn't

**Certain:** the mechanism. Each step is either standard-documented or measured, and the
OFDM branch was verified directly rather than inferred.

**Not a census:** how *common* the 1 Mbps DSSS default is comes from vendor defaults plus
one measured deployment — not a survey. The compatibility table is documentation-based:
it records what vendors *say* their controls do, and vendors document controls that don't
always behave (the ASUS "N-only isn't enough" case was caught by accident). Until a row
carries a **measured post-change beacon rate**, it's a hypothesis.

That's the gap contributions close. One audit run is one row: AP vendor and model, default
rate code, CSI yield, control applied, rate *after*, harvestable yes/no.

**Not novel physics.** That DSSS yields no CSI is known in the ESP32-CSI niche. What
seems under-documented is treating it as a *deployment constraint* — mapping which
real-world AP populations can be used at all, and shipping an instrument that answers it
in a minute. Academic work mostly runs Intel 5300-class NICs on *data* frames, where the
question never comes up.

---

*Please don't include BSSIDs or MAC addresses in contributed results — WiFi-positioning
databases map BSSID to street address, so publishing them publishes a location. Vendor and
model are enough.*
