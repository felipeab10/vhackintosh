# SMBIOS identity edit for an existing VM

Demanda 005. Rules live in `core/smbios.py`; the identity is written into the
existing `config.plist` by `core/disk.py::inject_opencore_config`, and the job
that drives it belongs to the Control backend (`core/web_agent.py`).

## 1. Where the EFI identity actually lives

The identity edit no longer rebuilds the EFI. The chosen path edits only
`config.plist` inside the existing image:

```
core/disk.py:310  inject_opencore_config
  qemu-img check                      # core/disk.py:327-334, refuse a damaged image
  copy image -> staged                # core/disk.py:340-347, original untouched until replace
  guestfish download /EFI/OC/config.plist
  GenSMBIOS.inject_into_config_plist  # core/smbios.py:191, identity = smbios or vm.smbios
  guestfish upload   /EFI/OC/config.plist
  read back + compare all five fields # core/disk.py:423-446
  qemu-img check, then atomic replace # core/disk.py:350-365, restore on failure
```

`GenSMBIOS.inject_into_config_plist` is the only writer of the five fields:

- `core/smbios.py:219` forces `PlatformInfo.Automatic = true` and
  `UpdateDataHub/UpdateNVRAM/UpdateSMBIOS = true` with
  `UpdateSMBIOSMode = Create`.
- `core/smbios.py:227` writes the five fields into `PlatformInfo/Generic`:
  `SystemProductName`, `SystemSerialNumber`, `MLB`, `SystemUUID` (string) and
  `ROM` (6-byte `data`). `Generic` is created when the config only had the
  legacy sections; a `PlatformInfo` that is absent, or a `Generic` that is not
  a dictionary, is refused instead of overwritten.
- `core/smbios.py:234` removes the same keys from `PlatformInfo/DataHub`,
  `PlatformInfo/PlatformNVRAM` and `PlatformInfo/SMBIOS`, when those exist.

That is the clean injection point. Per Dortania, OpenCore Reference Manual 1.0.9,
§10.1 (`PlatformInfo > Properties`), with `Automatic = true` the `DataHub`,
`PlatformNVRAM` and `SMBIOS` dictionaries are **ignored**, and `UpdateNVRAM`
rewrites the platform NVRAM variables from `Generic`. So:

- the identity must be edited in `Generic` only — writing `PlatformNVRAM.MLB` or
  `DataHub.SystemSerialNumber` has no effect while `Automatic` is true, and would
  come back to life if anyone ever flipped it to `false`;
- the stale identity that may exist in `OVMF_VARS.fd` is rewritten by OpenCore at
  boot (§10.1, `UpdateNVRAM`), so neither `OVMF_VARS.fd` nor the rest of the EFI
  has to change;
- `Board-ID` and `FirmwareFeatures` are derived by OpenCore from
  `SystemProductName`, which is why the template's `PlatformNVRAM.BID`
  (`Mac-CFF7D910A743CAAF`, `templates/config.plist:1507`) is inert and must not be
  copied by the editor.

`build_opencore.py:34-37` burns the neutral identity into the ISO EFI
(`C02VHA000001`, `C02VHA00000000001`, nil UUID, zero ROM); every VM starts there
and the edit replaces it. The neutral EFI itself is still built by
`reims-util opencore --model iMac20,1` (`appliance/build_opencore.py:209`); that
is the only place the EFI builder's model list still matters.

## 2. Rules (`GenSMBIOS.validate_for_edit`, `core/smbios.py:375`)

Pure function (no I/O, no environment) returning
`SMBIOSEditValidation(identity, warnings, changed_fields)` with the normalized
identity — uppercase serial/MLB/ROM, canonical uppercase UUID, `:`/`-` stripped
from ROM.

Accepted only when all of these hold:

| Field | Rule |
|---|---|
| `model` | must be in the allowlist table (`core/smbios.py:39`); must still be supported by the VM's `macos_version` |
| `serial_number` | exactly 12 uppercase alphanumerics; 3-char factory prefix in `C02/C07/D25/F5K/G08/C17`; not the ISO neutral serial |
| `board_serial` (MLB) | exactly 17 uppercase alphanumerics; same 3-char factory prefix as the serial; not the neutral MLB; not one repeated character; not equal to the serial |
| `smuuid` | canonical UUID, not the nil UUID |
| `rom` | 12 uppercase hex digits; locally administered unicast (`bit1 = 1`, `bit0 = 0`); not all-zero and not broadcast; a prefix other than `02` is accepted with a warning |

Plus two cross-field rules:

- changing `model` while keeping the old serial is refused: the serial carries
  the model code, so a new model needs a freshly generated serial/MLB pair;
- no field may collide with another VM's serial, MLB, UUID or ROM (case
  insensitive) — the same rule the store already applies
  (`core/config.py:507`, `_has_identity_conflict`).

`changed_fields` exists so the job can audit *which* field changed without
logging the values.

`GenSMBIOS.verify_with_macserial` (`core/smbios.py:487`) is the non-pure
companion for the post-generation gate: it decodes the serial with
`macserial --info` and requires the decoded model to equal the requested model,
then requires a valid MLB checksum. Measured on the host:

```
$ ./resources/macserial/macserial --info C02D38RCPN5T
         Model: PN5T - iMac20,1
   SystemModel: iMac (Retina 5K, 27-inch, 2020)

$ ./resources/macserial/macserial --verify C02204300GUPHC1A9
Valid MLB length: modern
WARN: Invalid MLB checksum!      # rc=0 !
```

`macserial` exits 0 even when it flags an invalid checksum, so the verdict is read
from stdout, never from the return code. Both helpers fail closed when the binary
is missing.

## 3. Model x macOS coherence

The allowlist (`core/smbios.py:39`) is curated, not copied from `macserial`.
Newest macOS per model, from Apple's compatibility lists
([Tahoe 26](https://support.apple.com/en-us/122867),
[Sequoia 15](https://support.apple.com/en-us/120282),
[Sonoma 14](https://support.apple.com/en-us/105113) and
[Ventura 13](https://support.apple.com/en-us/102861), whose list still includes the 2017
iMac):

| Model | Newest macOS | Why it stops there |
|---|---|---|
| iMac18,3 | Ventura 13 | Sonoma dropped the 2017 iMac |
| iMac19,1 | Sequoia 15 | Tahoe dropped the 2019 iMac |
| iMac20,1 | Tahoe 26 | 2020 27" iMac, appliance default |
| iMac20,2 | Tahoe 26 | 2020 27" iMac with 5700 XT |
| iMacPro1,1 | Sequoia 15 | Tahoe dropped the iMac Pro |
| MacBookPro15,1 | Sequoia 15 | Tahoe dropped 2018/2019 15" |
| MacBookPro16,1 | Tahoe 26 | 16" 2019 is on the Tahoe list |
| Macmini8,1 | Sequoia 15 | Tahoe dropped the 2018 mini |
| MacPro7,1 | Tahoe 26 | 2019 Mac Pro, ISO payload default |

The appliance floor is Ventura 13, so every listed model runs every OS the
appliance offers, except that `iMac18,3`, `iMacPro1,1`, `MacBookPro15,1` and
`Macmini8,1` cannot run Tahoe. Apple Silicon models (`Mac13,x` and newer) are
absent on purpose: `macserial` does not emit them and a VM cannot impersonate
them.

### The allowlist is capacity, not the EFI builder

Before the in-place edit was chosen, the identity went through
`reims-util opencore`, and every installed build accepted only two models:

```
$ reims-util opencore --model MacPro7,1 ...
error: unknown model "MacPro7,1": iMac20,1 and iMacPro1,1 are known
```

That constraint is gone: the EFI builder is not in the identity path any more,
so the only limits are what `macserial` can generate and what macOS installs.
All nine models above were re-measured on 2026-11-18 with the pinned binary:
each one generates a 12-character serial through
`macserial -m <model> -g -n 1` and `macserial --info` decodes that serial back
to the same model (e.g. `F5KKG0KAP7QM` → `P7QM - MacPro7,1`). A test enforces
this for every table entry (`tests/test_smbios_edit_rules.py`,
`SmbiosAllowlistIsGeneratableTests`), so a model that macserial cannot produce
fails CI instead of reaching a VM.

`MacPro7,1` is in the table and was the appliance's original default before the
macserial work: the current default is `iMac20,1`
(`core/config.py:49` and `RECOMMENDED_MODELS`, `core/smbios.py:92`), and the
`MacPro7,1` you find in `appliance/archiso/.../core/` is a stale payload copy
that `appliance/build-iso.sh:212` deletes and rebuilds from this tree. So no
model needs to be reintroduced for the in-place edit; `MacPro7,1` is simply
allowed, as the earlier example expects.

### Does the QEMU graphics profile constrain the SMBIOS model? No

`apple-gfx-pci` and `reims-graphics-pci` are selected by the QEMU device and by
their kext bundles (`core/graphics_profiles.py`): `ReimsVgpu` plus the
`AppleParavirt*` bridges, or the `Rgfx*` family. Nothing in either bundle, in
`templates/config.plist` or in the injection path keys on `SystemProductName`.
`DeviceProperties > Add` is empty in the generated EFI and no `board-id` appears
anywhere in the VM config; the `board_id` values in `core/downloader.py:96-115`
are the fixed identifiers used to fetch a recovery image, independent of the
VM's identity.

So the model and the graphics profile are independent, with two caveats that are
about macOS, not about QEMU:

- the model has to be able to run the chosen macOS at all (the table above);
- `MacPro7,1` on KVM is the classic `AppleIntelMCEReporter` panic case, and the
  template already ships `MCEReporterDisabler.kext` enabled
  (`templates/config.plist:485`), so no per-model patch is needed.

### `macserial -a` is a generator table, not an allowlist

Measured: `macserial -a` prints **1210 lines but only 121 distinct models**,
starting at `MacBook1,1` (2006). It happily generates a pair for a 2006 MacBook,
which no current macOS installs on, and it is the same table that
`macserial -l` uses to list the valid 4-char model codes per model
(`iMac20,1` has 9 codes, `MacBookPro15,1` has 58). So:

- usable as the source of serial/MLB pairs and to decode a serial back to its
  model (`--info`);
- not usable as the allowlist of selectable models — that is the table above,
  which must be extended only with both measurements (`macserial -l` lists the
  model, AND Apple's current list still supports it).

## 4. SMUUID and ROM stay Control-generated

`macserial` emits only the serial and the MLB; it has no SMUUID or ROM output.
The Control keeps minting them (`core/smbios.py:143-150`):

- **SMUUID**: random UUID4, uppercase, never nil, unique across the catalog.
  OpenCore writes it to SMBIOS `SystemUUID` and to the
  `4D1EDE05-38C7-4A6A-9CC6-4BCCA8B38C14:system-id` NVRAM variable (§10.5,
  `SystemUUID`).
- **ROM**: `02` + 5 random bytes = a locally administered unicast MAC. The format
  matters only in that it must be 6 bytes, locally administered, and unique per
  VM; Apple does not derive it from the serial or validate it as a checksum.
  Deriving it from the VM's QEMU NIC (`52:54:00:...`) would tie the identity to
  the network config and force a re-edit after any NIC change, so the independent
  `02` value is preferable. A real vendor OUI must never be used.

## 5. Consequences in the guest

- The Apple services layer keys registration to serial + MLB + UUID + ROM, so a new
  identity makes the guest a **new Mac** to iCloud, iMessage and FaceTime.
- Sign out of iCloud/iMessage/FaceTime **before** the change; after it, sign in
  again and remove the old machine from the Apple ID device list, otherwise the
  account keeps a ghost device.
- FaceTime/iMessage registration can fail if the serial/MLB pair does not pass
  Apple's checks — the serial's model code has to belong to the model being
  claimed. `GenSMBIOS.verify_with_macserial` is the gate for that and the job
  must call it (see §7); with the pure rules alone, a serial that decodes to
  `Unknown` is accepted and the guest then fails registration.
- FileVault keeps unlocking with the user password or the recorded recovery key;
  an identity change does not re-encrypt the volume, but iCloud escrow of the key
  should be re-checked after signing in again.
- Activation Lock / Find My is tied to the serial presented at enrolment: the new
  serial starts unenrolled, and the old serial stays attached to the old Apple ID
  entry until it is removed.
- "About This Mac" and System Report show the new model and serial immediately
  after the next boot; no macOS reinstall is needed for an identity-only change.
- Anything signed with the old identity (App Store device list, some DRM
  authorizations) may need to be authorized again.
- Repeated identity changes in a short window are a risk: Apple treats a serial
  that keeps changing on the same hardware as suspicious and can refuse
  iMessage/FaceTime registration for a while. Edit once, test, and only then
  touch it again.

## 5.1 What the in-place edit touches on an already-installed VM

Nothing except `PlatformInfo`. The identity is written into the existing
`config.plist`, so for an installed VM this is a serial swap, not an EFI reset:

- `NVRAM > Add > 7C436110-… > csr-active-config` (SIP) keeps whatever the image
  already had: a guest running with SIP disabled stays that way.
- The `Kernel > Patch` entries (including the two that hide the VM,
  `kern.hv_vmm_present`), `Kernel > Add` and the kext set keep their `Enabled`
  flags and contents — no kext can be dropped by an identity change.
- `Misc > Security > Vault`, `SecureBootModel`, `prev-lang:kbd`, `boot-args`,
  `Misc > Boot > ShowPicker`, `DeviceProperties` and `ACPI` are untouched.
  `inject_into_config_plist` accepts a `show_picker` argument only for call-site
  compatibility and deliberately does not apply it.
- The disk and `OVMF_VARS.fd` are untouched; OpenCore rewrites the platform NVRAM
  variables from `Generic` at boot (Dortania §10.1, `UpdateNVRAM`).

So the guest boots exactly as before, with a new Apple identity. What remains to
tell the operator is the Apple-account side (§5): sign out first, sign in again
after, and clean the device list.

Rollback is one step back, twice over: `inject_opencore_config` keeps a staged
copy and a temporary backup and only replaces the original after `qemu-img check`
and the read-back of all five fields both pass (`core/disk.py:340-371`); the
apply job also keeps `.opencore-before-smbios.qcow2` and
`smbios-backup-before-regeneration/OpenCore.qcow2` (the directory name predates
this change; it is now the backup the catalog guard requires,
`core/config.py:482-485`). All of these are overwritten by the next edit, so
only the immediately previous EFI can be restored.

## 6. Clone: identity is regenerated, and guarded

`core/vm_clone.py` does **not** copy the source identity:

- `core/vm_clone.py:457` mints a new identity through `smbios_factory`
  (`_generate_smbios`, same model, fresh serial/MLB/UUID/ROM);
- `:458-460` validates it and refuses the clone if it collides with any VM;
- `:501` assigns it to the clone, and `:599` rewrites `OpenCore.qcow2` with it;
- `:509` seeds NVRAM from a clean template, not from the source's `OVMF_VARS.fd`.

Residual risk: a custom `smbios_factory`, or a manual copy of `OpenCore.qcow2`
outside this service, would duplicate serial/MLB. Two VMs sharing one Apple
identity fight over the same iCloud/iMessage registration and can get the serial
flagged. The conflict rule is the detector; there is nothing to fix in the clone
flow itself.

## 7. State after the in-place decision (updated 2026-11-18)

The apply job no longer regenerates the EFI. Its identity path is
`core/disk.py::inject_opencore_config`, which is already in the tree and already
guarantees, without any new code:

1. `qemu-img check` on the image before and after the write, with the original
   restored if either fails (`core/disk.py:327-334` and `:350-365`).
2. the five fields read back from the image and compared before the staged copy
   replaces the original (`core/disk.py:423-446`);
3. `PlatformInfo/Generic` as the single source of truth, with
   `Automatic/Update*/UpdateSMBIOSMode` set and the legacy sections cleaned
   (`core/smbios.py:219-238`), so the whole `Kernel`/`Misc`/`NVRAM` tree is
   preserved byte for byte.

Closed by this change: the allowlist/EFI-builder mismatch, and the previous
postcondition that only compared the fields it had requested — the read-back is
now against the identity actually written to the image.

Still open, for the job that wires it:

1. **`core/disk.py` is mid-change and does not run yet.** The identity is
   resolved at `core/disk.py:326` (`identity = smbios if smbios is not None else
   vm.smbios`) but the in-place helper never sees it: `_inject_opencore_config_in_place`
   takes `smbios=` and does not accept `identity`, and `core/disk.py:408` calls
   `GenSMBIOS.inject_into_config_plist(tmp_plist, identity, ...)` with no binding
   for `identity` in scope — `NameError` on the first real run. The `smbios`
   parameter is used nowhere in that function.
2. **The read-back still compares against the old identity.**
   `core/disk.py:439-446` checks `generic.get(...) == vm.smbios....`, so once
   item 1 is fixed, applying a *new* identity to an existing VM fails with
   "OpenCore EFI identity verification failed". Both places must use the same
   resolved `identity`.
3. `verify_with_macserial` still has no production caller. With the pure rules
   and the read-back as the only gates, `model=iMac20,2` + `serial=C02ABCDEFGHJ`
   (which decodes to `FGHJ - Unknown`) is accepted end to end, and the guest then
   fails iMessage/FaceTime. Call it in the prepare step, after
   `validate_for_edit` and before the backup.
4. Ordering and the catalog: validate, then copy the backups, then call
   `inject_opencore_config(smbios=<new identity>)`, then update the catalog through
   `update_vm`, which refuses an identity change with no verified backup
   (`core/config.py:482-485`).
5. `inject_into_config_plist` writes the template's `PlatformInfo` keys
   untouched (`SystemMemoryStatus`, `ProcessorType`, `SpoofVendor`,
   `AdviseFeatures`), which is what the fleet already runs; if a model needs a
   different `ProcessorType`, that is a separate change, not part of the edit.
6. The `show_picker` argument is now dead weight: it is accepted and ignored on
   purpose. Do not start applying it from the identity job.

