# Recording manifest

SHA-256 (first 16 hex) and size for every archived capture, so a
checkout can be verified as complete. These files are **large (~180 MB total) and are
not suitable for git**; if this repo is published, ship them via release assets or an
archive and treat a checkout without them as **claims-not-reproducible**.

| file | size | sha256[:16] |
|---|---|---|
| `first_light.npz` | 0.0 MB | `1c29d234ee7a2a83` |
| `labelled_presence.npz` | 38.5 MB | `ea50e52d3b47aab2` |
| `seqlock.npz` | 2.1 MB | `9e3f56bcb653ed3a` |
| `soak10min.npz` | 0.3 MB | `cacfc90b103a7b89` |
| `spread1m.npz` | 6.3 MB | `49526c6a9e5a1752` |
| `tdm.npz` | 8.0 MB | `58bd20f7785d57e3` |
| `tdm4.npz` | 21.0 MB | `8216d9879b7719ff` |
| `threelink.npz` | 14.3 MB | `adec84b44dacd85b` |
| `twonode.npz` | 4.1 MB | `9a714c69cafb2042` |
| `twonode10min.npz` | 47.6 MB | `d2292764b7b5aa66` |
| `walk_null_fail1.npz` | 0.9 MB | `b6d63051d680a949` |
| `walk_null_pass.npz` | 0.9 MB | `37b62f83fb0e8a62` |
| `walk_vrti.npz` | 36.2 MB | `9de8f974bfa57eff` |
| empty_ch11.npz | 117.8 MB | sha256:dfec09f7c391ac73... | 2026-07-30 | EMPTY-ROOM baseline, ch11, 20 links; proved+fixed the LTF-block scale toggle |
| station2.npz | 26.9 MB | sha256:3439c039db947a1b... | 2026-07-30 | station test #2, clean pipeline, 5 dwells |
