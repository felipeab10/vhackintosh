"""Shared graphics-profile kext sets and vendored bundle locations.

The profile name is the QEMU device name. Lilu is embedded by reims-util and is
therefore intentionally absent from each profile's externally injected bundles.
"""
from __future__ import annotations

from pathlib import Path

KEXT_MAX_KERNEL = "25.99.99"
LEGACY_PROFILE = "apple-gfx-pci"
EMBEDDED_KEXTS = frozenset({"Lilu.kext"})

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
        expected = entry.get("sha256")
        if not expected:
            # sha256 is optional: an entry without it is accepted as-is.
            continue
        relative = entry.get("ExecutablePath")
        bundle = entry.get("BundlePath")
        if not relative or not bundle:
            raise RuntimeError(
                f"manifesto {manifest}: entrada {bundle!r} traz sha256 sem ExecutablePath"
            )
        executable = directory / bundle / relative
        if not executable.is_file():
            raise RuntimeError(
                f"manifesto {manifest}: executável ausente {executable} "
                f"(bundle {bundle!r}, esperado sha256 {expected})"
            )
        actual = hashlib.sha256(executable.read_bytes()).hexdigest()
        if actual.lower() != str(expected).lower():
            raise RuntimeError(
                f"manifesto {manifest}: sha256 divergente no bundle {bundle!r} "
                f"({relative}): esperado {expected}, obtido {actual}"
            )

# Layout is resolved by `profile_kext_dir(profile, base)` at call time. A frozen
# `PROFILE_KEXT_DIRS` snapshot used to live here; it was removed because patching it was a
# silent no-op once generation switched to call-time resolution.
