import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.runtime_inventory import _catalog_names, _device_categories, _device_names, _device_property_inventory, _device_property_names, _identity, _option_names, _probe, _reims_schema_features, _selected_binary_paths, _simple_names, inspect_runtime


class RuntimeInventoryTests(unittest.TestCase):
    def _script(self, root: Path, name: str, body: str) -> Path:
        path = root / name
        path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
        path.chmod(0o700)
        return path

    def test_inventory_reports_selected_versions_and_safe_qemu_options(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            reims = self._script(root, "reims-ctl", '''case "$1 $2" in
  "--version ") echo "reims-ctl 8.1" ;;
  "capabilities --json") echo '{"schema_version":1,"schema_features":["additional_disks"]}' ;;
  "set --help") printf '%s\\n' '      --cpus <CPUS>' '      --audio [<AUDIO>]' '      --audio-backend <BACKEND>' '      --network [<NETWORK>]' '      --mac <MAC>' ;;
  "create --help") printf '%s\\n' '      --disk-size <SIZE>' '      --cpus <CPUS>' ;;
  "update-opencore --help") printf '%s\\n' 'Usage: reims-ctl update-opencore [OPTIONS] <NAME>' '      --boot-selection [<BOOT_SELECTION>]' ;;
esac
''')
            qemu = self._script(root, "qemu", '''case "$1" in
  --version) echo "QEMU emulator version 11.1" ;;
  -help) printf "%s\\n" "-rtc [base=utc]" "-msg [timestamp=on]" "-device driver" ;;
  -device) printf '%s\\n' 'name "q35-pcihost", bus PCI' 'name "virtio-blk-pci", bus PCI' ;;
  -machine) printf '%s\\n' 'Supported machines are:' 'q35  Q35 machine' 'pc-i440fx-9.2  Standard PC' ;;
  -cpu) printf '%s\\n' 'Available CPUs:' 'Skylake-Client-noTSX-IBRS  alias' 'host  host processor' ;;
  -audiodev) printf '%s\\n' 'Available audio drivers:' 'pipewire' 'pa' 'alsa' 'none' ;;
esac
''')

            with patch("core.reims_release.current_release", return_value="v1.4.0"):
                result = inspect_runtime(reims_binary=reims, qemu_binary=qemu, use_cache=False)

        self.assertEqual(result["status"], "available")
        self.assertEqual(result["reims_version"], "reims-ctl 8.1")
        self.assertEqual(result["reims_schema_version"], 1)
        self.assertEqual(result["reims_schema_features"], ["additional_disks"])
        self.assertEqual(result["reims_release_tag"], "v1.4.0")
        self.assertEqual(result["qemu_version"], "QEMU emulator version 11.1")
        self.assertEqual(result["qemu_options"], ["-rtc", "-msg"])
        self.assertEqual(result["qemu_command_options"], ["-rtc", "-msg", "-device"])
        self.assertEqual(result["reims_set_options"], ["--cpus", "--audio", "--audio-backend", "--network", "--mac"])
        self.assertEqual(result["reims_create_options"], ["--disk-size", "--cpus"])
        self.assertTrue(result["reims_update_opencore_picker"])
        self.assertEqual(result["qemu_devices"], ["q35-pcihost", "virtio-blk-pci"])
        self.assertEqual(result["qemu_machine_types"], ["q35", "pc-i440fx-9.2"])
        self.assertEqual(result["qemu_cpu_models"], ["Skylake-Client-noTSX-IBRS", "host"])
        self.assertEqual(result["qemu_audio_backends"], ["pipewire", "pa", "alsa", "none"])
        self.assertEqual(result["qemu_device_categories"]["storage"], ["virtio-blk-pci"])
        self.assertEqual(result["qemu_device_properties"]["virtio-blk-pci"]["status"], "unknown")
        self.assertEqual(len(result["binary_fingerprint"]), 64)
        inventory = result["capability_inventory"]
        self.assertEqual(inventory["schema_version"], 2)
        self.assertIn("not runtime validation", inventory["evidence_basis"])
        self.assertEqual(inventory["reims"]["set_options"]["status"], "reported")
        self.assertEqual(inventory["reims"]["set_options"]["source"], "reims-ctl set --help")
        self.assertEqual(inventory["qemu"]["audio_backends"]["values"], ["pipewire", "pa", "alsa", "none"])
        self.assertEqual(inventory["qemu"]["device_categories"]["storage"]["values"], ["virtio-blk-pci"])
        self.assertEqual(inventory["qemu"]["device_properties"]["virtio-blk-pci"]["status"], "unknown")
        self.assertEqual(inventory["qemu"]["machine_types"]["status"], "reported")
        self.assertIn("does not prove runtime compatibility", inventory["qemu"]["machine_types"]["meaning"])

    def test_fresh_appliance_inventories_the_tagged_iso_runtime_before_first_vm_start(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "bin").mkdir()
            (root / "vendor" / "qemu" / "build" / "bin").mkdir(parents=True)
            reims = self._script(root / "bin", "reims-ctl", '''case "$1 $2" in
  "--version ") echo "reims-ctl bundled 1.4.0" ;;
  "capabilities --json") echo '{"schema_version":1,"schema_features":["additional_disks"]}' ;;
  "set --help") echo '      --audio [<AUDIO>]' ;;
  "create --help") echo '      --disk-size <SIZE>' ;;
  "update-opencore --help") echo 'Usage: reims-ctl update-opencore [OPTIONS] <NAME>' ;;
esac
''')
            qemu = self._script(root / "vendor" / "qemu" / "build" / "bin", "qemu-system-x86_64", '''case "$1" in
  --version) echo "QEMU emulator version 11.1" ;;
  -help) printf "%s\\n" "-rtc [base=utc]" "-msg [timestamp=on]" "-device driver" ;;
  -device) printf '%s\\n' 'name "virtio-net-pci", bus PCI' ;;
  -machine) printf '%s\\n' 'Supported machines are:' 'q35  Q35 machine' ;;
  -cpu) printf '%s\\n' 'Available CPUs:' 'host  host processor' ;;
  -audiodev) printf '%s\\n' 'Available audio drivers:' 'pipewire' ;;
esac
''')
            (root / ".release").write_text("v1.4.0\n", encoding="utf-8")
            installed_reims = root / "not-installed" / "reims-ctl"
            installed_qemu = root / "not-installed" / "qemu-system-x86_64"
            with patch("core.runtime_inventory._cache", None), \
                 patch("core.runtime_inventory._qemu_path", return_value=installed_qemu), \
                 patch("core.reims_ctl.reims_ctl_bin", return_value=str(installed_reims)), \
                 patch("core.reims_build.find_reims_dir", return_value=root), \
                 patch("core.reims_release.current_release", return_value="v1.4.0"):
                selected_reims, selected_qemu = _selected_binary_paths()
                result = inspect_runtime(use_cache=False)
            self.assertEqual(selected_reims, reims)
            self.assertEqual(selected_qemu, qemu)
            self.assertEqual(result["status"], "available")
            self.assertEqual(result["reims_version"], "reims-ctl bundled 1.4.0")
            self.assertEqual(result["qemu_version"], "QEMU emulator version 11.1")

            # A tagged ISO should normally contain .release, but losing that
            # metadata must not make already packaged executable binaries invisible
            # until the user re-prepares the same release from the dashboard.
            (root / ".release").unlink()
            with patch("core.runtime_inventory._qemu_path", return_value=installed_qemu), \
                 patch("core.reims_ctl.reims_ctl_bin", return_value=str(installed_reims)), \
                 patch("core.reims_build.find_reims_dir", return_value=None), \
                 patch("core.reims_build.REIMS_SEARCH_PATHS", (str(root),)):
                selected_reims, selected_qemu = _selected_binary_paths()
            self.assertEqual(selected_reims, reims)
            self.assertEqual(selected_qemu, qemu)

    def test_installed_runtime_pair_takes_precedence_over_the_iso_bundle(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            installed_reims = self._script(root, "installed-reims-ctl", "exit 0\n")
            installed_qemu = self._script(root, "installed-qemu", "exit 0\n")
            with patch("core.runtime_inventory._qemu_path", return_value=installed_qemu), \
                 patch("core.reims_ctl.reims_ctl_bin", return_value=str(installed_reims)), \
                 patch("core.reims_build.find_reims_dir") as find_bundle:
                selected_reims, selected_qemu = _selected_binary_paths()

        find_bundle.assert_not_called()
        self.assertEqual(selected_reims, installed_reims)
        self.assertEqual(selected_qemu, installed_qemu)

    def test_reims_schema_feature_parser_rejects_malformed_or_duplicate_contract(self):
        self.assertEqual(_reims_schema_features('{"schema_version":1,"schema_features":["additional_disks"]}'), (1, ["additional_disks"]))
        for value in (
            "not json",
            '{"schema_version":0,"schema_features":["additional_disks"]}',
            '{"schema_version":1,"schema_features":["bad feature"]}',
            '{"schema_version":1,"schema_features":["additional_disks","additional_disks"]}',
        ):
            with self.subTest(value=value):
                self.assertEqual(_reims_schema_features(value), (None, []))

    def test_inventory_fails_closed_when_qemu_lacks_a_required_option(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            reims = self._script(root, "reims-ctl", 'if [ "$1" = "--version" ]; then echo "reims-ctl 8.1"; fi\n')
            qemu = self._script(root, "qemu", 'if [ "$1" = "--version" ]; then echo "QEMU emulator version 11.1"; else echo "-rtc [base=utc]"; fi\n')
            result = inspect_runtime(reims_binary=reims, qemu_binary=qemu, use_cache=False)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["qemu_options"], ["-rtc"])
        self.assertFalse(result["reims_update_opencore_picker"])
        self.assertEqual(result["capability_inventory"]["qemu"]["safe_options"]["values"], ["-rtc"])
        self.assertEqual(result["capability_inventory"]["qemu"]["machine_types"]["status"], "unknown")

    def test_binary_identity_tracks_missing_paths_and_metadata_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            missing_one = root / "one" / "qemu"
            missing_two = root / "two" / "qemu"
            self.assertNotEqual(_identity(missing_one), _identity(missing_two))
            missing_one.parent.mkdir()
            before_install = _identity(missing_one)
            missing_one.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            missing_one.chmod(0o700)
            after_install = _identity(missing_one)
            self.assertNotEqual(before_install, after_install)
            before_update = _identity(missing_one)
            with missing_one.open("a", encoding="utf-8") as binary:
                binary.write("# updated\n")
            self.assertNotEqual(before_update, _identity(missing_one))

    def test_cached_inventory_is_defensively_copied(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            reims = self._script(root, "reims-ctl", "exit 0\n")
            qemu = self._script(root, "qemu", "exit 0\n")

            def probe(_binary, *arguments, **kwargs):
                if arguments == ("-device", "help"):
                    return 'name "virtio-net-pci", bus PCI\n'
                if arguments == ("-help",):
                    return "-rtc [base=utc]\n-msg [timestamp=on]\n"
                return ""

            with patch("core.runtime_inventory._cache", None), \
                 patch("core.runtime_inventory._probe", side_effect=probe), \
                 patch("core.reims_release.current_release", return_value="v-test"):
                first = inspect_runtime(reims_binary=reims, qemu_binary=qemu)
                first["qemu_device_categories"]["network"].clear()
                first["capability_inventory"]["qemu"]["devices"]["values"].clear()
                second = inspect_runtime(reims_binary=reims, qemu_binary=qemu)

        self.assertEqual(second["qemu_device_categories"]["network"], ["virtio-net-pci"])
        self.assertEqual(second["capability_inventory"]["qemu"]["devices"]["values"], ["virtio-net-pci"])

    def test_cache_fingerprint_tracks_binary_replacement_and_release_tag(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            reims = self._script(root, "reims-ctl", "exit 0\n")
            qemu = self._script(root, "qemu", "exit 0\n")
            with patch("core.runtime_inventory._cache", None), \
                 patch("core.runtime_inventory._probe", return_value="" ) as probe, \
                 patch("core.reims_release.current_release", side_effect=["v1", "v1", "v1", "v2"]):
                first = inspect_runtime(reims_binary=reims, qemu_binary=qemu)
                calls_after_first = probe.call_count
                cached = inspect_runtime(reims_binary=reims, qemu_binary=qemu)
                self.assertEqual(probe.call_count, calls_after_first)
                self.assertEqual(first["binary_fingerprint"], cached["binary_fingerprint"])

                qemu.write_text("#!/bin/sh\n# replacement\nexit 0\n", encoding="utf-8")
                qemu.chmod(0o700)
                replaced = inspect_runtime(reims_binary=reims, qemu_binary=qemu)
                self.assertGreater(probe.call_count, calls_after_first)
                self.assertNotEqual(first["binary_fingerprint"], replaced["binary_fingerprint"])
                replaced_calls = probe.call_count

                new_release = inspect_runtime(reims_binary=reims, qemu_binary=qemu)
                self.assertGreater(probe.call_count, replaced_calls)
                self.assertNotEqual(replaced["binary_fingerprint"], new_release["binary_fingerprint"])

    def test_probe_rejects_unbounded_output_and_times_out(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            noisy = self._script(root, "noisy", 'yes "012345678901234567890123456789"\n')
            slow = self._script(root, "slow", 'sleep 30\n')
            self.assertIsNone(_probe(noisy, "--version"))
            self.assertIsNone(_probe(slow, "--version"))

    def test_probe_does_not_start_process_after_shared_deadline(self):
        with patch("core.runtime_inventory.time.monotonic", return_value=10.0), \
             patch("core.runtime_inventory.subprocess.Popen") as popen:
            self.assertIsNone(_probe(Path("/bin/true"), "--help", deadline=9.0))
        popen.assert_not_called()

    def test_multiple_probes_cannot_extend_the_aggregate_deadline(self):
        with tempfile.TemporaryDirectory() as temp:
            binary = self._script(Path(temp), "qemu", "exit 0\n")
            clock = [0.0]

            class QuietSelector:
                def get_map(self):
                    return {1: object()}

                def register(self, *_args):
                    pass

                def select(self, timeout):
                    clock[0] += timeout
                    return []

                def close(self):
                    pass

            class FakeOutput:
                def fileno(self):
                    return 1

                def close(self):
                    pass

            process = type("FakeProcess", (), {
                "stdout": FakeOutput(), "pid": 123,
                "kill": lambda self: None,
                "wait": lambda self, *args, **kwargs: 0,
            })()
            with patch("core.runtime_inventory.time.monotonic", side_effect=lambda: clock[0]), \
                 patch("core.runtime_inventory.selectors.DefaultSelector", side_effect=QuietSelector), \
                 patch("core.runtime_inventory.os.set_blocking"), \
                 patch("core.runtime_inventory.os.killpg", side_effect=OSError), \
                 patch("core.runtime_inventory.subprocess.Popen", return_value=process) as popen:
                deadline = 0.5
                self.assertIsNone(_probe(binary, "-help", deadline=deadline))
                self.assertEqual(clock[0], deadline)
                self.assertIsNone(_probe(binary, "-device", "help", deadline=deadline))
                self.assertEqual(clock[0], deadline)
            self.assertEqual(popen.call_count, 1)

    def test_catalog_parsers_ignore_headers_and_deduplicate(self):
        self.assertEqual(_device_names('name "virtio-blk-pci", bus PCI\nname "virtio-blk-pci", bus PCI\n'), ["virtio-blk-pci"])
        self.assertEqual(_catalog_names("Supported machines are:\nq35  Q35\n\n", "Supported machines are:"), ["q35"])
        self.assertEqual(_catalog_names("unrecognized output", "Available CPUs:"), [])

    def test_audio_backends_and_device_categories_are_exact_and_bounded(self):
        self.assertEqual(_simple_names("Available audio drivers:\npipewire\npa\nnone\nnot a name with spaces\n"), ["pipewire", "pa", "none"])
        categories = _device_categories(["usb-audio", "virtio-sound", "ide-hd", "virtio-net-pci", "unknown"])
        self.assertEqual(categories["audio"], ["usb-audio", "virtio-sound"])
        self.assertEqual(categories["storage"], ["ide-hd"])
        self.assertEqual(categories["network"], ["virtio-net-pci"])

    def test_device_property_parser_is_bounded_and_does_not_expose_values(self):
        output = """virtio-net-pci options:
  bootindex=<int32>
  mac=<MACADDR>
  hostfwd=tcp::2222-:22
  This is a description, not a property
"""
        self.assertEqual(_device_property_names(output), ["bootindex", "mac", "hostfwd"])
        self.assertEqual(len(_device_property_names("".join(f"  prop{i}=<str>\n" for i in range(300)))), 128)
        self.assertEqual(_device_property_names(None), [])

    def test_property_inventory_queries_only_listed_allowlisted_models_and_reports_unknown(self):
        with tempfile.TemporaryDirectory() as temp:
            binary = Path(temp) / "qemu"
            binary.write_text("", encoding="utf-8")
            binary.chmod(0o700)
            deadline = 123.0
            def probe(_binary, *arguments, **kwargs):
                self.assertEqual(kwargs["deadline"], deadline)
                self.assertEqual(kwargs["max_output"], 32 * 1024)
                if arguments == ("-device", "virtio-net-pci,help"):
                    return "virtio-net-pci options:\n  mac=<MACADDR>\n  mq=<bool>\n"
                return None
            with patch("core.runtime_inventory._probe", side_effect=probe) as mocked:
                result = _device_property_inventory(
                    binary,
                    ["virtio-net-pci", "usb-host", "virtio-sound-pci"],
                    deadline=deadline,
                )
        self.assertEqual(list(result), ["virtio-net-pci", "virtio-sound-pci"])
        self.assertEqual(result["virtio-net-pci"]["status"], "reported")
        self.assertEqual(result["virtio-net-pci"]["properties"], ["mac", "mq"])
        self.assertEqual(result["virtio-sound-pci"]["status"], "unknown")
        self.assertEqual(mocked.call_count, 2)
        self.assertTrue(all(call.args[1] == "-device" for call in mocked.call_args_list))

    def test_option_parser_requires_option_tokens_and_deduplicates(self):
        self.assertEqual(_option_names("  --audio [<AUDIO>]\n  --audio-backend <BACKEND>\n  not-an-option\n  --audio [again]", prefix="--"), ["--audio", "--audio-backend"])


if __name__ == "__main__":
    unittest.main()
