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

## `reims-graphics-pci` profile bundles

The five bundles under `reims-graphics-pci/` are copied verbatim from
`steelbrain/reims-vgpu` commit `eef1c88da0b3de5cdb161b44c24087e367ee23b9`,
`guest/prebuilt/`. Source metadata is the adjacent `set.toml` and `sources.toml`;
they were copied from the same commit. `set.toml` declares each bundle's
executable SHA-256 and the OpenCore gates `MinKernel=25.4.0`,
`MaxKernel=25.99.99`. Each executable was checked both against `set.toml` and
the source blob at that commit. The LICENSE at that commit offers
GPL-2.0-or-later OR LGPL-3.0-or-later; see the upstream `LICENSE` and the full
texts already included in `LICENSES/` above. These five bundles use
`com.steelbrain.*` identifiers (not `com.apple.*`).

| Bundle | CFBundleIdentifier | Executable SHA-256 | Bundle tree SHA-256 |
| --- | --- | --- | --- |
| `ReimsGraphicsLoader.kext` | `com.steelbrain.ReimsGraphicsLoader` | `b4ba581f334b060d4b42d0c7d61353242197b5cfbe1ea36cd895d229ac7981d0` | `466bb3475e28970f93e37af19c9b827b19fe83be1a2a8605d19d96b534a32b23` |
| `RgfxIOGraphicsFamilyBridge.kext` | `com.steelbrain.RgfxIOGraphicsFamilyBridge` | `347b0d81deeb1aba5a6bb2640c9f69bce68607b88b3d7fc5b7f43687ce365253` | `cd77e6b679164aaabf7cfad7c31ca3589f296796d0c2bf00e5b43c92a0d57d21` |
| `RgfxAppleGraphicsDeviceControlBridge.kext` | `com.steelbrain.RgfxAppleGraphicsDeviceControlBridge` | `12bded4c6d70f1177662195121927ca9ce0af450a80f3fcf79ba1b555887f95d` | `bdcb3ee46ccb6297e63bf10ee5168e4b16825184c7fec923500d307e06e5a084` |
| `ReimsGraphics.kext` | `com.steelbrain.ReimsGraphics` | `b091b773681dc6771d7cd807f877cd5c682e6cca3a21cd1c6ae61e8342473c89` | `cd5baf48aeb932117c728b76c407fbfbc6026e97f3522657cc344bad4946f249` |
| `RgfxGate.kext` | `com.steelbrain.RgfxGate` | `44d238921f7b5e20afd64649c464ad2a8748d7e35b89ea0c2e08a66b4c4af0b7` | `1114dc5b5e7d54ca931a2ccd627e30921309d6abb02f7dcec6c8410acce5a224` |

The `set.toml`/`sources.toml` files are retained alongside the bundles for
upstream build-input provenance. The project does not modify `reims-vgpu` or
its generated kexts.
