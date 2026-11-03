"""Validate and atomically activate a compatible web-manager bundle.

Bundles are immutable directories containing ``core``, ``web/api``, the SPA,
and vendored Python dependencies. A single ``current`` symlink selects the
complete set, so API code, agent code, dependencies, and frontend assets cannot
be mixed during promotion. Existing SQLite state and the appliance's Reims
release are compatibility inputs, not files that the updater mutates.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from core.web_jobs import _SCHEMA_VERSION
from core.web_rpc import PROTOCOL_VERSION


BUNDLE_FORMAT_VERSION = 1
API_CONTRACT_VERSION = 1
JOBS_SCHEMA_MIN = 0  # Schema 0 is the supported first-run migration state.
JOBS_SCHEMA_MAX = _SCHEMA_VERSION
_BUNDLE_ID = re.compile(r"^[a-f0-9]{64}$")
_HASH = re.compile(r"^[a-f0-9]{64}$")


class WebBundleError(RuntimeError):
    """Bundle is incomplete, incompatible, or unsafe to activate."""


class BundleIntegrityError(WebBundleError):
    """The bundle manifest or payload hashes do not match."""


class BundleCompatibilityError(WebBundleError):
    """The bundle does not support this appliance's current state."""


@dataclass(frozen=True)
class BundleMetadata:
    bundle_id: str
    api_contract_version: int
    rpc_protocol_version: int
    jobs_schema_min: int
    jobs_schema_max: int
    runtime_release: str


def _regular_payload_files(root: Path) -> list[Path]:
    if root.is_symlink() or not root.is_dir():
        raise BundleIntegrityError("bundle candidate must be a real directory")
    files: list[Path] = []
    for path in root.rglob("*"):
        if path.is_symlink():
            raise BundleIntegrityError(f"bundle contains a symbolic link: {path.relative_to(root)}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise BundleIntegrityError(f"bundle contains a non-regular entry: {path.relative_to(root)}")
        files.append(path)
    return sorted(files, key=lambda item: item.relative_to(root).as_posix().encode("utf-8"))


def _payload_id(root: Path, files: list[Path], runtime_release: str) -> str:
    digest = hashlib.sha256()
    for path in files:
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        digest.update(b"\0")
    digest.update(runtime_release.encode("utf-8"))
    return digest.hexdigest()


def write_bundle_metadata(root: str | os.PathLike[str], *, runtime_release: str) -> BundleMetadata:
    """Write deterministic compatibility metadata and SHA-256 file inventory."""
    directory = Path(root)
    if not isinstance(runtime_release, str) or not runtime_release.strip() or len(runtime_release) > 128:
        raise BundleCompatibilityError("runtime release must be a non-empty bounded string")
    files = _regular_payload_files(directory)
    required = {
        "core/web_jobs.py", "core/web_rpc.py", "web/api/server.py",
        "web/api/app.py", "web/frontend/dist/index.html",
        "vendor/fastapi/__init__.py", "vendor/uvicorn/__init__.py",
    }
    present = {path.relative_to(directory).as_posix() for path in files}
    missing = sorted(required - present)
    if missing:
        raise BundleIntegrityError("bundle is missing required files: " + ", ".join(missing))

    bundle_id = _payload_id(directory, files, runtime_release.strip())
    metadata = BundleMetadata(
        bundle_id=bundle_id,
        api_contract_version=API_CONTRACT_VERSION,
        rpc_protocol_version=PROTOCOL_VERSION,
        jobs_schema_min=JOBS_SCHEMA_MIN,
        jobs_schema_max=JOBS_SCHEMA_MAX,
        runtime_release=runtime_release.strip(),
    )
    (directory / "BUNDLE.json").write_text(
        json.dumps({
            "format_version": BUNDLE_FORMAT_VERSION,
            "bundle_id": metadata.bundle_id,
            "api_contract_version": metadata.api_contract_version,
            "rpc_protocol_version": metadata.rpc_protocol_version,
            "jobs_schema_min": metadata.jobs_schema_min,
            "jobs_schema_max": metadata.jobs_schema_max,
            "runtime_release": metadata.runtime_release,
        }, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    _write_sha256_manifest(directory)
    return metadata


def _write_sha256_manifest(root: Path) -> None:
    manifest = root / "MANIFEST.sha256"
    files = [path for path in _regular_payload_files(root) if path != manifest]
    temporary = root / ".MANIFEST.sha256.tmp"
    with temporary.open("w", encoding="ascii", newline="\n") as output:
        for path in files:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            output.write(f"{digest}  {path.relative_to(root).as_posix()}\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, manifest)


def validate_bundle(
    root: str | os.PathLike[str],
    *,
    jobs_schema: int,
    runtime_release: str,
) -> BundleMetadata:
    """Verify manifest coverage and reject API/schema/runtime incompatibility."""
    directory = Path(root)
    files = _regular_payload_files(directory)
    metadata_path = directory / "BUNDLE.json"
    manifest_path = directory / "MANIFEST.sha256"
    if metadata_path not in files or manifest_path not in files:
        raise BundleIntegrityError("bundle metadata or SHA-256 manifest is missing")
    try:
        raw = json.loads(metadata_path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
        metadata = BundleMetadata(
            bundle_id=raw["bundle_id"],
            api_contract_version=raw["api_contract_version"],
            rpc_protocol_version=raw["rpc_protocol_version"],
            jobs_schema_min=raw["jobs_schema_min"],
            jobs_schema_max=raw["jobs_schema_max"],
            runtime_release=raw["runtime_release"],
        )
    except (OSError, UnicodeError, ValueError, TypeError, KeyError) as exc:
        raise BundleIntegrityError("bundle metadata is malformed") from exc
    if (not isinstance(raw, dict) or raw.get("format_version") != BUNDLE_FORMAT_VERSION
            or not isinstance(metadata.bundle_id, str) or not _BUNDLE_ID.fullmatch(metadata.bundle_id)
            or any(isinstance(value, bool) or not isinstance(value, int) for value in (
                metadata.api_contract_version, metadata.rpc_protocol_version,
                metadata.jobs_schema_min, metadata.jobs_schema_max,
            ))
            or metadata.jobs_schema_min < 0 or metadata.jobs_schema_max < metadata.jobs_schema_min
            or not isinstance(metadata.runtime_release, str) or not metadata.runtime_release):
        raise BundleIntegrityError("bundle metadata has invalid fields")

    declared: dict[str, str] = {}
    try:
        for line in manifest_path.read_text(encoding="ascii").splitlines():
            digest, separator, relative = line.partition("  ")
            if not separator or not _HASH.fullmatch(digest) or not relative or relative in declared:
                raise BundleIntegrityError("bundle SHA-256 manifest is malformed")
            rel = Path(relative)
            if rel.is_absolute() or ".." in rel.parts or relative == "MANIFEST.sha256":
                raise BundleIntegrityError("bundle manifest contains an unsafe path")
            target = directory / rel
            if target.is_symlink() or not target.is_file():
                raise BundleIntegrityError(f"bundle manifest path is not a regular file: {relative}")
            if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                raise BundleIntegrityError(f"bundle payload hash mismatch: {relative}")
            declared[relative] = digest
    except (OSError, UnicodeError) as exc:
        raise BundleIntegrityError("bundle SHA-256 manifest could not be read") from exc
    actual = {path.relative_to(directory).as_posix() for path in files if path != manifest_path}
    if set(declared) != actual:
        raise BundleIntegrityError("bundle SHA-256 manifest does not cover every payload file")
    if _payload_id(directory, [path for path in files if path not in {metadata_path, manifest_path}],
                   metadata.runtime_release) != metadata.bundle_id:
        raise BundleIntegrityError("bundle ID does not match its payload and runtime metadata")

    if metadata.api_contract_version != API_CONTRACT_VERSION:
        raise BundleCompatibilityError("API contract version is incompatible")
    if metadata.rpc_protocol_version != PROTOCOL_VERSION:
        raise BundleCompatibilityError("local agent protocol version is incompatible")
    if isinstance(jobs_schema, bool) or not isinstance(jobs_schema, int):
        raise BundleCompatibilityError("jobs database schema is unknown")
    if not metadata.jobs_schema_min <= jobs_schema <= metadata.jobs_schema_max:
        raise BundleCompatibilityError(
            f"jobs schema {jobs_schema} is outside supported range "
            f"{metadata.jobs_schema_min}..{metadata.jobs_schema_max}"
        )
    if metadata.runtime_release not in {runtime_release, "control-independent"}:
        raise BundleCompatibilityError(
            f"bundle expects Reims runtime {metadata.runtime_release}, found {runtime_release}"
        )
    return metadata


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate metadata key")
        result[key] = value
    return result


def promote_bundle(
    candidate: str | os.PathLike[str],
    *,
    bundles_dir: str | os.PathLike[str],
    current_link: str | os.PathLike[str],
    jobs_schema: int,
    runtime_release: str,
) -> BundleMetadata:
    """Copy, validate, then switch one relative symlink as the commit point.

    The candidate can live on another filesystem. It is first copied to a
    private staging directory beside immutable versioned bundles. A failure
    before the final ``os.replace`` leaves the previous ``current`` pointer
    untouched; a completed but not activated version may safely remain for
    cleanup/retry.
    """
    source = Path(candidate)
    metadata = validate_bundle(source, jobs_schema=jobs_schema, runtime_release=runtime_release)
    parent = Path(bundles_dir)
    link = Path(current_link)
    parent.mkdir(parents=True, exist_ok=True)
    link.parent.mkdir(parents=True, exist_ok=True)
    if parent.parent.resolve() != link.parent.resolve():
        raise WebBundleError("current pointer must be beside the bundle versions")
    target = parent / metadata.bundle_id
    if target.exists() or target.is_symlink():
        existing = validate_bundle(target, jobs_schema=jobs_schema, runtime_release=runtime_release)
        if existing != metadata:
            raise WebBundleError("an immutable bundle ID already exists with different metadata")
    else:
        stage = Path(tempfile.mkdtemp(prefix=".bundle-stage-", dir=parent))
        shutil.rmtree(stage)
        try:
            shutil.copytree(source, stage, symlinks=False)
            validate_bundle(stage, jobs_schema=jobs_schema, runtime_release=runtime_release)
            os.replace(stage, target)
        except Exception:
            shutil.rmtree(stage, ignore_errors=True)
            raise

    temporary_link = link.parent / f".{link.name}.{os.getpid()}.tmp"
    temporary_link.unlink(missing_ok=True)
    try:
        temporary_link.symlink_to(f"{parent.name}/{target.name}")
        os.replace(temporary_link, link)
    except Exception:
        temporary_link.unlink(missing_ok=True)
        raise
    return metadata


def current_bundle_id(
    *,
    bundles_dir: str | os.PathLike[str],
    current_link: str | os.PathLike[str],
) -> str | None:
    """Return the validated ID behind the pointer, or None if not initialized."""
    parent = Path(bundles_dir)
    link = Path(current_link)
    if not link.is_symlink():
        if link.exists():
            raise WebBundleError("current bundle pointer exists but is not a symlink")
        return None
    raw = Path(os.readlink(link))
    if raw.is_absolute() or len(raw.parts) != 2 or raw.parts[0] != parent.name or not _BUNDLE_ID.fullmatch(raw.parts[1]):
        raise WebBundleError("current bundle pointer is malformed or escapes its versions directory")
    bundle_id = raw.parts[1]
    directory = parent / bundle_id
    try:
        raw_metadata = json.loads((directory / "BUNDLE.json").read_text(encoding="utf-8"))
        schema = raw_metadata.get("jobs_schema_min")
        runtime = raw_metadata.get("runtime_release")
        if isinstance(schema, bool) or not isinstance(schema, int) or not isinstance(runtime, str):
            raise ValueError("invalid compatibility metadata")
        validate_bundle(directory, jobs_schema=schema, runtime_release=runtime)
    except (OSError, ValueError, TypeError) as exc:
        raise WebBundleError("current bundle is incomplete or corrupt") from exc
    return bundle_id


def point_to_bundle(
    bundle_id: str | None,
    *,
    bundles_dir: str | os.PathLike[str],
    current_link: str | os.PathLike[str],
) -> None:
    """Atomically restore a known immutable bundle pointer (or clear it)."""
    parent = Path(bundles_dir)
    link = Path(current_link)
    if parent.parent.resolve() != link.parent.resolve():
        raise WebBundleError("current pointer must be beside the bundle versions")
    if bundle_id is not None:
        if not _BUNDLE_ID.fullmatch(bundle_id):
            raise WebBundleError("bundle ID is invalid")
        directory = parent / bundle_id
        if not directory.is_dir() or directory.is_symlink():
            raise WebBundleError("requested rollback bundle is missing or unsafe")
        if not (directory / "BUNDLE.json").is_file():
            raise WebBundleError("requested rollback bundle has no metadata")
        raw = json.loads((directory / "BUNDLE.json").read_text(encoding="utf-8"))
        validate_bundle(directory, jobs_schema=raw["jobs_schema_min"], runtime_release=raw["runtime_release"])
        replacement = link.parent / f".{link.name}.{os.getpid()}.rollback"
        replacement.unlink(missing_ok=True)
        replacement.symlink_to(f"{parent.name}/{bundle_id}")
        os.replace(replacement, link)
    else:
        link.unlink(missing_ok=True)
