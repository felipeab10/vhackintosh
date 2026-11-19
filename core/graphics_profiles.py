"""Shared graphics-profile kext sets and vendored bundle locations.

The profile name is the QEMU device name. Lilu is embedded by reims-util and is
therefore intentionally absent from each profile's externally injected bundles.
"""
from __future__ import annotations

from pathlib import Path

KEXT_MAX_KERNEL = "25.99.99"
LEGACY_PROFILE = "apple-gfx-pci"
EMBEDDED_KEXTS = frozenset({"Lilu.kext"})

# Kexts de suporte que o usuário adiciona ao `Kernel > Add` e que não pertencem a
# nenhuma família gráfica. Eles não mudam o perfil do QEMU nem indicam
# ambiguidade, então o detector os aceita e apenas os nomeia (warning). O
# `MCEReporterDisabler` evita o panic do AppleIntelMCEReporter em board-ids como
# Mac-27AD2F918AE68F61 (MacPro7,1); VirtualSMC/SMC* são o conjunto de sensores.
SUPPORT_KEXTS = EMBEDDED_KEXTS | frozenset({
    "VirtualSMC.kext",
    "SMCProcessor.kext",
    "SMCSuperIO.kext",
    "MCEReporterDisabler.kext",
})

# The support bundles that are actually vendored under `support/` and injected in
# every generated image, with their OpenCore MinKernel. A support kext added by
# the user (VirtualSMC, SMC*) is accepted by the classifier above but not copied
# by Control: only these are part of the build.
SUPPORT_PROFILE = "support"
SUPPORT_KEXT_BUNDLES: tuple[tuple[str, str], ...] = (
    ("MCEReporterDisabler.kext", "19.0.0"),
)

GRAPHICS_PROFILES: dict[str, tuple[tuple[str, str], ...]] = {
    "apple-gfx-pci": (
        ("ReimsVgpu.kext", "25.0.0"),
        ("AppleGraphicsDeviceControlBridge.kext", "25.4.0"),
        ("AppleParavirtGPULoader.kext", "25.4.0"),
        ("AppleParavirtIOSurface.kext", "25.4.0"),
        ("IOGPUFamilyBridge.kext", "25.4.0"),
        ("AppleParavirtGPUIOGPUFamily.kext", "25.4.0"),
        ("IOGraphicsFamilyBridge.kext", "25.4.0"),
        ("APVDisplayPort.kext", "25.4.0"),
    ),
    "reims-graphics-pci": (
        ("ReimsGraphicsLoader.kext", "25.4.0"),
        ("RgfxIOGraphicsFamilyBridge.kext", "25.4.0"),
        ("RgfxAppleGraphicsDeviceControlBridge.kext", "25.4.0"),
        ("ReimsGraphics.kext", "25.4.0"),
        ("RgfxGate.kext", "25.4.0"),
    ),
}

VENDORED_KEXTS_DIR = Path(__file__).resolve().parent.parent / "resources" / "reims-kexts"
VENDORED_KEXTS = GRAPHICS_PROFILES[LEGACY_PROFILE]

def support_kext_dir(base: Path | None = None) -> Path:
    """Directory holding the vendored support bundles (`support/`)."""
    root = VENDORED_KEXTS_DIR if base is None else base
    return root / SUPPORT_PROFILE


def profile_kext_dir(profile: str, base: Path | None = None) -> Path:
    """Directory holding ``profile``'s bundles, resolved at call time.

    ``base`` defaults to the current ``VENDORED_KEXTS_DIR``. Callers that re-export the
    path (e.g. ``core.reims_util``) pass their own global so patching it is honored
    instead of silently using a frozen path.
    """
    root = VENDORED_KEXTS_DIR if base is None else base
    if profile == LEGACY_PROFILE:
        return root
    return root / profile

MANIFEST_NAME = "set.toml"
# Only the historical flat legacy layout has no canonical set.toml. New profiles must
# declare a manifest; otherwise deleting it would disable executable-hash validation.
MANIFESTLESS_PROFILES = frozenset({LEGACY_PROFILE})

def verify_kext_manifest(profile: str, base: Path | None = None) -> None:
    """Verify each bundle's executable against ``set.toml`` (the canonical manifest).

    Contract agreed with Nova: the manifest beside the bundles is the source of truth.
      - The named legacy flat profile is manifest-less; every other profile requires set.toml.
      - For each ``[[kext]]`` whose ``sha256`` is set, require ``ExecutablePath`` and
        compare the SHA-256 of ``<bundle>/<ExecutablePath>`` case-insensitively.
      - Hash only the executable file, never the whole bundle.
      - A plist-only bundle (no executable) pins ``plist_sha256`` instead: require
        ``PlistPath`` and compare the SHA-256 of ``<bundle>/<PlistPath>``. This is
        additive; executable hashing keeps the contract agreed with Nova.

    Raises ``RuntimeError`` naming bundle, path, expected and actual on mismatch, so a
    truncated or swapped kext fails at build time instead of surfacing inside the VM.
    """
    import hashlib
    import tomllib

    directory = profile_kext_dir(profile, base)
    manifest = directory / MANIFEST_NAME
    if not manifest.is_file():
        if profile in MANIFESTLESS_PROFILES:
            return
        raise RuntimeError(
            f"manifesto obrigatório ausente: {manifest} (perfil {profile!r})"
        )
    with manifest.open("rb") as stream:
        data = tomllib.load(stream)
    for entry in data.get("kext", ()):
        bundle = entry.get("BundlePath")
        expected = entry.get("sha256")
        expected_plist = entry.get("plist_sha256")
        if not expected and not expected_plist:
            # Both hashes are optional: an entry without them is accepted as-is.
            continue
        # Executable first (the historical contract), then the plist for bundles
        # that carry no executable at all.
        for key, relative_key, label in (
            (expected, "ExecutablePath", "executável"),
            (expected_plist, "PlistPath", "Info.plist"),
        ):
            if not key:
                continue
            relative = entry.get(relative_key)
            if not relative or not bundle:
                raise RuntimeError(
                    f"manifesto {manifest}: entrada {bundle!r} traz hash de {label} "
                    f"sem {relative_key}"
                )
            target = directory / bundle / relative
            if not target.is_file():
                raise RuntimeError(
                    f"manifesto {manifest}: {label} ausente {target} "
                    f"(bundle {bundle!r}, esperado sha256 {key})"
                )
            actual = hashlib.sha256(target.read_bytes()).hexdigest()
            if actual.lower() != str(key).lower():
                raise RuntimeError(
                    f"manifesto {manifest}: sha256 divergente no bundle {bundle!r} "
                    f"({relative}): esperado {key}, obtido {actual}"
                )

# Layout is resolved by `profile_kext_dir(profile, base)` at call time. A frozen
# `PROFILE_KEXT_DIRS` snapshot used to live here; it was removed because patching it was a
# silent no-op once generation switched to call-time resolution.
