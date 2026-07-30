"""wisent — WiFi sensing toolkit for ESP32 swarms.

Live-path modules: csi_io, sanitize, features, vrti, breathing, linkbvp,
ratios, observability.
Test-only module: sim (physics-based synthetic CSI). Live-path modules MUST NOT
import sim — this separation is checked by scripts/validate_synthetic.py.
"""

__version__ = "0.1.0"

C = 299792458.0          # m/s
F0 = 2.437e9             # Hz, WiFi channel 6 center
LAMBDA0 = C / F0         # ~0.1231 m
