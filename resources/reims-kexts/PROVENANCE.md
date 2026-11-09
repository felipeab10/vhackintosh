# Bundled reims-vgpu guest kexts

The eight `.kext` bundles in this directory are copied verbatim from the
`steelbrain/reims-util` repository at commit
`c717e69f0a74ba050973dcca294ba35499145c56` (directory `kext/`).

- Upstream: https://github.com/steelbrain/reims-util
- Commit: `c717e69f0a74ba050973dcca294ba35499145c56`
- Upstream path: `kext/`
- License: GPL-2.0-or-later OR LGPL-3.0-or-later (the reims-util `LICENSE`,
  included here as `LICENSE.txt`, with the two full texts under `LICENSES/`).
- `upstream-sources.sha256` is the upstream `kext/sources.sha256`, kept so the
  built kext provenance can be traced back to its sources.

Why these are vendored here: `reims-util` used to embed these kexts in the
OpenCore image it generates. Upstream commit
`fe9bfe1` ("Add kexts to the OpenCore image with --add-kext, drop the ported
ones") stopped embedding them; the binary now ships a Lilu alone and takes
extra kexts through `--add-kext`. Both `core/reims_util.py` and
`appliance/build_opencore.py` therefore pass the eight bundles below with
`--add-kext` and the kernel-version bounds from `REQUIRED_KEXTS`. A pinned
binary predating `fe9bfe1` still embeds them, and in that case nothing is
passed.

Three of the bundles carry an Apple `CFBundleIdentifier` (`com.apple.*`) and
originate from Apple; upstream distributed them publicly in the same `kext/`
directory under the repository license above. They are noted here so a later
reader knows the identifier is third-party.

## Bundles

SHA-256 of the bundle executable and of the whole bundle tree
(`find <bundle> -type f | sort | xargs sha256sum | sha256sum`), both verified
against the files at commit `c717e69f0a74ba050973dcca294ba35499145c56`:

| Bundle | CFBundleIdentifier | Executable | Executable SHA-256 | Tree SHA-256 |
| --- | --- | --- | --- | --- |
| `ReimsVgpu.kext` | `com.steelbrain.ReimsVgpu` | `ReimsVgpu` | `d028c738a707af0de69a78b455bbca8ea39e4d8b71a56c51f432bbf1c29915c3` | `dc17fab4b13934be44cc8f8ff066f699b0d750f3541290530cf8c1dd7b12016e` |
| `AppleGraphicsDeviceControlBridge.kext` | `com.steelbrain.paravirt.AppleGraphicsDeviceControlBridge` | `AppleGraphicsDeviceControlBridge` | `88cb0b74e808f3c69b9669b532236018eb2940423e407ca2e4da38717e9e369c` | `121c898216605a440ea6375ff3e349dcb837048cc2968482ddda1d802c92e428` |
| `AppleParavirtGPULoader.kext` | `com.steelbrain.paravirt.AppleParavirtGPULoader` | `AppleParavirtGPULoader` | `9b73442b9a85f1236569ac7354ecd56bcfef37479a9c84f7cd9e0af11b3c95ab` | `e1aab751dec8be1acf97073967d5b8d5a406f612de19ad44d0205ebf6275e19a` |
| `AppleParavirtIOSurface.kext` | `com.apple.iokit.AppleParavirtIOSurface` | `AppleParavirtIOSurface` | `9fd57239aa4e997691d4453703e5fe0dbe96207e467d058da9138eab8de12750` | `b057dce83b2b55346cd1b810616053a7f7d10238fac6b22ac6f3a0699ac65557` |
| `IOGPUFamilyBridge.kext` | `com.steelbrain.paravirt.IOGPUFamilyBridge` | `IOGPUFamilyBridge` | `a3f9af6c2d19146ff0839f206874c274fadef64841709215522cf2d4c1db1cca` | `27f302d069471e9c1b54f35d2e28de37587ecdc3006d7944af3c02b8ecc815aa` |
| `AppleParavirtGPUIOGPUFamily.kext` | `com.apple.driver.AppleParavirtGPUIOGPUFamily` | `AppleParavirtGPUIOGPUFamily` | `2841ab3f3536a01b7b737c1d39976034ecad3f5adb84e6d96c069555fbf9ea44` | `83b099ad8edc39e44c2f6936ce13893c829c168734a6a7c55bdffd688b9fe294` |
| `IOGraphicsFamilyBridge.kext` | `com.steelbrain.paravirt.IOGraphicsFamilyBridge` | `IOGraphicsFamilyBridge` | `3cc3be05f3504699a46f099fe526cd88653b8e9c5a790c802b971115f42c3730` | `4d0e8886bebe6741781002f8fe86635de68f03b508edce608a30121228433003` |
| `APVDisplayPort.kext` | `com.apple.driver.APVDisplayPort` | `APVDisplayPort` | `02f2065b7ff64e8da80ea64603ee85f9667da994515e29a22eebc9524cf97f8f` | `fdf2df8b48b55f4538799493a6395e4b80e7608d4af1c9e69f7e5df61e079598` |

All eight executables are Mach-O 64-bit x86_64 kext bundles.

## Which set is correct today

These eight are the kexts for the `apple-gfx-pci` device, the default of the
installed fleet `reims-ctl`. The fork's `origin/master` has since moved to
`reims-graphics-pci` (`2cc59c93e`), whose own kexts live under
`guest/prebuilt/` (`ReimsGraphics`, `ReimsGraphicsLoader`, `RgfxGate`,
`RgfxIOGraphicsFamilyBridge`, `RgfxAppleGraphicsDeviceControlBridge`). When the
fleet `reims-ctl` migrates, the correct set becomes those five and this one
becomes wrong; the choice follows the installed `reims-ctl` binary, not a
permanent constant.
