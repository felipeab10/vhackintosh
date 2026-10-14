# Bundled MacSerial

The Linux x86-64 executable in this directory is copied from the official
Acidanthera OpenCorePkg release archive `OpenCore-1.0.8-RELEASE.zip`.

- Upstream: https://github.com/acidanthera/OpenCorePkg
- Release/tag: `1.0.8`
- Archive URL: https://github.com/acidanthera/OpenCorePkg/releases/download/1.0.8/OpenCore-1.0.8-RELEASE.zip
- Archive SHA-256: `2011e8b7216ecb2645d97ea710df965947edd406846e92050b9bc4190be6d27b`
- Archive member: `Utilities/macserial/macserial.linux`
- Executable SHA-256: `b0a4a3581c0f06b3ddce915165eb3a1726acfa876192fe9ee5ef9ea08a469ad8`
- Version reported by executable: `2.1.8`
- License: BSD 3-Clause; the complete upstream `LICENSE.txt` is included beside the binary.

The release archive checksum was verified before extraction. The tool produces
serial/MLB pairs; this application generates a fresh UUID and locally
administered ROM per VM, validates the complete tuple, and persists it with
that VM. Generated identifiers are synthetic and are not guaranteed to be
accepted by Apple services. This package makes no representation of Apple
approval or activation.
