from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.web_bundle import (
    BundleCompatibilityError,
    BundleIntegrityError,
    _write_sha256_manifest,
    bind_current_bundle_runtime,
    promote_bundle,
    validate_bundle,
    write_bundle_metadata,
)


def make_bundle(root: Path, *, content: str, runtime: str = "v1.0.0") -> Path:
    for relative in (
        "core/web_jobs.py", "core/web_rpc.py", "web/api/server.py", "web/api/app.py",
        "web/frontend/dist/index.html", "vendor/fastapi/__init__.py", "vendor/uvicorn/__init__.py",
    ):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    write_bundle_metadata(root, runtime_release=runtime)
    return root


class WebBundleTests(unittest.TestCase):
    def test_runtime_binding_rehashes_bundle_and_updates_current_pointer(self):
        with tempfile.TemporaryDirectory() as temporary:
            web_root = Path(temporary) / "opt/vhackintosh/web"
            versions = web_root / "bundles"
            versions.mkdir(parents=True)
            candidate = make_bundle(versions / ".control-staging", content="control",
                                    runtime="control-independent")
            initial_metadata = json.loads((candidate / "BUNDLE.json").read_text(encoding="utf-8"))
            source = versions / initial_metadata["bundle_id"]
            candidate.rename(source)
            current = web_root / "current"
            current.symlink_to(f"bundles/{source.name}")

            metadata = bind_current_bundle_runtime(web_root, runtime_release="v1.5.0")

            self.assertNotEqual(metadata.bundle_id, initial_metadata["bundle_id"])
            self.assertEqual(os.readlink(current), f"bundles/{metadata.bundle_id}")
            self.assertFalse(source.exists())
            self.assertEqual(current.resolve().name, metadata.bundle_id)
            self.assertEqual(validate_bundle(current.resolve(), jobs_schema=1,
                                             runtime_release="v1.5.0"), metadata)

    def test_runtime_binding_rejects_current_pointer_outside_bundle_versions(self):
        with tempfile.TemporaryDirectory() as temporary:
            web_root = Path(temporary) / "opt/vhackintosh/web"
            (web_root / "bundles").mkdir(parents=True)
            (web_root / "current").symlink_to("../../outside")
            with self.assertRaisesRegex(BundleIntegrityError, "malformed or escapes"):
                bind_current_bundle_runtime(web_root, runtime_release="v1.5.0")

    def test_valid_bundle_promotes_through_one_current_pointer(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old = make_bundle(root / "candidate-old", content="old")
            new = make_bundle(root / "candidate-new", content="new")
            versions = root / "opt/vhackintosh/web/bundles"
            current = root / "opt/vhackintosh/web/current"
            first = promote_bundle(old, bundles_dir=versions, current_link=current,
                                  jobs_schema=1, runtime_release="v1.0.0")
            self.assertEqual(current.resolve().name, first.bundle_id)
            second = promote_bundle(new, bundles_dir=versions, current_link=current,
                                    jobs_schema=1, runtime_release="v1.0.0")
            self.assertEqual(current.resolve().name, second.bundle_id)
            self.assertEqual((current / "web/frontend/dist/index.html").read_text(), "new")
            self.assertTrue((versions / first.bundle_id / "web/frontend/dist/index.html").is_file())
            self.assertEqual(sorted(path.name for path in versions.iterdir()), sorted((first.bundle_id, second.bundle_id)))

    def test_schema_and_runtime_mismatches_are_refused_before_activation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate = make_bundle(root / "candidate", content="new")
            with self.assertRaises(BundleCompatibilityError):
                validate_bundle(candidate, jobs_schema=2, runtime_release="v1.0.0")
            with self.assertRaises(BundleCompatibilityError):
                validate_bundle(candidate, jobs_schema=1, runtime_release="v1.0.1")
            with self.assertRaises(BundleCompatibilityError):
                promote_bundle(candidate, bundles_dir=root / "versions", current_link=root / "current",
                               jobs_schema=2, runtime_release="v1.0.0")
            self.assertFalse((root / "current").exists())

    def test_api_and_rpc_mismatch_are_refused_even_with_valid_hashes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate = make_bundle(root / "candidate", content="new")
            metadata_path = candidate / "BUNDLE.json"
            metadata = json.loads(metadata_path.read_text())
            metadata["api_contract_version"] += 1
            metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
            _write_sha256_manifest(candidate)
            with self.assertRaisesRegex(BundleCompatibilityError, "API contract"):
                validate_bundle(candidate, jobs_schema=1, runtime_release="v1.0.0")
            metadata["api_contract_version"] -= 1
            metadata["rpc_protocol_version"] += 1
            metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
            _write_sha256_manifest(candidate)
            with self.assertRaisesRegex(BundleCompatibilityError, "agent protocol"):
                validate_bundle(candidate, jobs_schema=1, runtime_release="v1.0.0")

    def test_corruption_or_missing_payload_never_changes_current(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old = make_bundle(root / "old", content="old")
            candidate = make_bundle(root / "new", content="new")
            versions = root / "versions"
            current = root / "current"
            previous = promote_bundle(old, bundles_dir=versions, current_link=current,
                                      jobs_schema=1, runtime_release="v1.0.0")
            (candidate / "web/frontend/dist/index.html").write_text("tampered", encoding="utf-8")
            with self.assertRaises(BundleIntegrityError):
                promote_bundle(candidate, bundles_dir=versions, current_link=current,
                               jobs_schema=1, runtime_release="v1.0.0")
            self.assertEqual(current.resolve().name, previous.bundle_id)

    def test_interruption_before_pointer_replace_keeps_old_bundle_active(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old = make_bundle(root / "old", content="old")
            new = make_bundle(root / "new", content="new")
            versions = root / "versions"
            current = root / "current"
            previous = promote_bundle(old, bundles_dir=versions, current_link=current,
                                      jobs_schema=1, runtime_release="v1.0.0")
            replace = os.replace

            def interrupt(source, destination):
                if Path(destination) == current:
                    raise OSError("simulated power loss before activation")
                return replace(source, destination)

            with patch("core.web_bundle.os.replace", side_effect=interrupt):
                with self.assertRaisesRegex(OSError, "power loss"):
                    promote_bundle(new, bundles_dir=versions, current_link=current,
                                   jobs_schema=1, runtime_release="v1.0.0")
            self.assertEqual(current.resolve().name, previous.bundle_id)
            complete = validate_bundle(versions / json.loads((new / "BUNDLE.json").read_text())["bundle_id"],
                                       jobs_schema=1, runtime_release="v1.0.0")
            self.assertEqual((versions / complete.bundle_id / "web/frontend/dist/index.html").read_text(), "new")
            self.assertFalse(list(versions.glob(".bundle-stage-*")))

    def test_copy_interruption_discards_stage_and_keeps_current(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old = make_bundle(root / "old", content="old")
            new = make_bundle(root / "new", content="new")
            versions = root / "versions"
            current = root / "current"
            previous = promote_bundle(old, bundles_dir=versions, current_link=current,
                                      jobs_schema=1, runtime_release="v1.0.0")

            def fail_copy(source, destination, **kwargs):
                Path(destination).mkdir(parents=True)
                (Path(destination) / "partial").write_text("incomplete")
                raise OSError("simulated interruption while copying")

            with patch("core.web_bundle.shutil.copytree", side_effect=fail_copy):
                with self.assertRaisesRegex(OSError, "while copying"):
                    promote_bundle(new, bundles_dir=versions, current_link=current,
                                   jobs_schema=1, runtime_release="v1.0.0")
            self.assertEqual(current.resolve().name, previous.bundle_id)
            self.assertFalse(list(versions.glob(".bundle-stage-*")))

    def test_bundle_refuses_symbolic_links(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate = make_bundle(root / "candidate", content="new")
            (candidate / "escape").symlink_to(candidate / "BUNDLE.json")
            with self.assertRaises(BundleIntegrityError):
                validate_bundle(candidate, jobs_schema=1, runtime_release="v1.0.0")


if __name__ == "__main__":
    unittest.main()
