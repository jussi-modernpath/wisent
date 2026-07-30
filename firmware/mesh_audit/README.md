# mesh_audit — 60-second AP harvestability test

Answers one question for any deployment: **can this room's WiFi be used as a passive
sensing source at all?** For most consumer gear the answer is no, and the reason is a
single AP setting. Background and the vendor map: `docs/dsss-compatibility.md`.

```bash
FQBN=esp32:esp32:esp32s3:CDCOnBoot=default          # classic ESP32: esp32:esp32:esp32
arduino-cli compile --fqbn $FQBN \
    --build-property "build.extra_flags=-DAUDIT_CH=1" firmware/mesh_audit
arduino-cli upload -p /dev/cu.usbmodem* --fqbn $FQBN firmware/mesh_audit
# then read the serial output at 921600 for ~60 s (DTR/RTS low — see README)
```

Set `AUDIT_CH` to your AP's 2.4 GHz channel. Per BSSID it prints beacons received, CSI
callbacks produced, the PHY rate code, and a verdict.

## Reading the result

| verdict | meaning |
|---|---|
| `DSSS - CANNOT yield CSI` | rate code ≤ 0x07 (1/2/5.5/11 Mbps). **No signal processing can fix this** — DSSS has no OFDM training field. Change the AP's basic rate or give up on this AP. |
| `OFDM non-HT - harvestable (L-LTF only)` | expect ~**64** I/Q pairs, half an HT frame |
| `HT - harvestable` | expect ~**128** pairs |
| `OFDM but no CSI yet` | capable rate, no traffic yet — keep watching |

**Sanity check before trusting a zero:** point the same sketch at a channel with known
OFDM traffic (e.g. a wisent beacon) and confirm `CSI TOTAL` is non-zero. If it is zero
there too, the audit is broken and the table means nothing. A silent zero is exactly the
failure this instrument exists to make loud.

## The fix, when the verdict is DSSS

Force the AP's lowest **basic (mandatory) rate** to an OFDM rate — "disable 802.11b",
"802.11g/n only", "minimum bitrate 6 Mbps", or `legacy_rates 0` / `beacon_rate=60` on
hostapd. Which platforms expose this is mapped in `docs/dsss-compatibility.md` §2:
enterprise/prosumer/open generally yes; consumer mesh and ISP gateways generally no, with
ASUS ("Disable 11b") and AVM FRITZ!Box ("802.11n+g") the notable consumer exceptions.

There is no host-side workaround. The frames arrive perfectly and are radio-invisible.

## Contribute a row

The vendor survey is documentation-based; this table is **measurement**-based, and that
difference matters — vendors document controls that do not always do what they claim.
One run adds one row. Please include the *measured* beacon rate **after** applying any
control, not just that the control exists.

| AP vendor / model | firmware | default rate code | CSI yield | control applied | rate after | harvestable? |
|---|---|---|---|---|---|---|
| *(consumer mesh, 2 radios, model withheld)* | stock | `0x00` (1 Mbps DSSS) | **0 / 3442 beacons** | none available | — | **no** |
| *(add yours)* | | | | | | |

**Do not include BSSIDs or MACs** — WiFi-positioning databases map BSSID to street address,
so publishing them publishes a location (README honesty rule 5). Vendor and model only, and
withhold even those if the deployment is someone's home.
