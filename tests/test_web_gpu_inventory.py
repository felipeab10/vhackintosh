from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.web_gpu import (
    CachedGpuInventory,
    ReimsGpu,
    ReimsProbe,
    VulkanProbe,
    _MAX_VULKAN_OUTPUT,
    inventory_gpus,
    probe_reims_gpus,
    probe_vulkan,
)


class GpuInventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.sysfs = self.root / "sys"
        self.dev = self.root / "dev/dri"
        self.pci = self.sysfs / "bus/pci/devices"
        self.pci.mkdir(parents=True)
        self.dev.mkdir(parents=True)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def add_gpu(
        self,
        bdf: str,
        *,
        vendor: str = "0x1002",
        device: str = "0x744c",
        render_node: str | None = "renderD128",
        driver: str | None = "amdgpu",
    ) -> Path:
        entry = self.pci / bdf
        entry.mkdir()
        (entry / "class").write_text("0x030000\n")
        (entry / "vendor").write_text(vendor + "\n")
        (entry / "device").write_text(device + "\n")
        (entry / "drm").mkdir()
        if render_node:
            (entry / "drm" / render_node).mkdir()
            (self.dev / render_node).touch()
        if driver:
            drivers = self.sysfs / "bus/pci/drivers" / driver
            drivers.mkdir(parents=True, exist_ok=True)
            (entry / "driver").symlink_to(drivers)
        return entry

    def test_lists_identical_cards_separately_by_bdf(self) -> None:
        one = self.add_gpu("0000:03:00.0", render_node="renderD128")
        self.add_gpu("0000:04:00.0", render_node="renderD129")
        gpus = inventory_gpus(
            sysfs_root=self.sysfs,
            dev_root=self.dev,
            vulkan_probe_result=VulkanProbe(False, {}, "vulkaninfo não instalado"),
            reims_probe_result=ReimsProbe(False, (), "Inventário Reims não instalado"),
        )
        self.assertEqual([gpu.pci_bdf for gpu in gpus], ["0000:03:00.0", "0000:04:00.0"])
        self.assertEqual(gpus[0].vendor_id, "1002")
        self.assertEqual(gpus[0].device_id, "744c")
        self.assertEqual(gpus[0].render_node, str(self.dev / "renderD128"))
        self.assertIsNone(gpus[0].driver_file)
        self.assertFalse(gpus[0].vulkan_available)
        self.assertIn("Vulkan indisponível", gpus[0].limitation or "")
        self.assertFalse(gpus[0].selection_supported)
        self.assertIn("Inventário Reims", gpus[0].limitation or "")
        self.assertEqual(gpus[0].name, "AMD [1002:744c]")

    def test_filters_non_display_devices_and_missing_identifiers(self) -> None:
        self.add_gpu("0000:01:00.0", vendor="0x10de", device="0x2684", render_node=None)
        network = self.pci / "0000:02:00.0"
        network.mkdir()
        (network / "class").write_text("0x020000")
        (network / "vendor").write_text("0x8086")
        (network / "device").write_text("0x1234")
        unknown = self.pci / "0000:03:00.0"
        unknown.mkdir()
        (unknown / "class").write_text("0x030000")
        (unknown / "vendor").write_text("unknown")
        self.assertEqual(
            [gpu.pci_bdf for gpu in inventory_gpus(
                sysfs_root=self.sysfs,
                dev_root=self.dev,
                vulkan_probe_result=VulkanProbe(False, {}, "sem sonda"),
                reims_probe_result=ReimsProbe(False, (), "sem Reims"),
            )],
            ["0000:01:00.0"],
        )

    def test_marks_host_display_only_on_explicit_framebuffer_link(self) -> None:
        gpu_path = self.add_gpu("0000:05:00.0")
        fb = self.sysfs / "class/graphics/fb0"
        fb.mkdir(parents=True)
        (fb / "device").symlink_to(gpu_path)
        gpus = inventory_gpus(
            sysfs_root=self.sysfs,
            dev_root=self.dev,
            vulkan_probe_result=VulkanProbe(False, {}, "not installed"),
            reims_probe_result=ReimsProbe(False, (), "sem Reims"),
        )
        self.assertIs(gpus[0].used_by_host, True)

    def test_probe_maps_uuid_only_with_explicit_pci_identity_and_safe_argv(self) -> None:
        calls: list[tuple[list[str], dict[str, object]]] = []

        def fake_runner(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append((argv, kwargs))
            document = {
                "devices": [{
                    "deviceUUID": "gpu-uuid-1",
                    "pciBusInfo": {"pciDomain": 0, "pciBus": 3, "pciDevice": 0, "pciFunction": 0},
                }]
            }
            return subprocess.CompletedProcess(argv, 0, json.dumps(document), "")

        probe = probe_vulkan(fake_runner)
        self.assertTrue(probe.available)
        self.assertEqual(probe.devices_by_bdf, {"0000:03:00.0": "gpu-uuid-1"})
        self.assertEqual(calls[0][0], ["vulkaninfo", "--json"])
        self.assertEqual(calls[0][1]["timeout"], 2.0)
        self.assertTrue(calls[0][1]["capture_output"])
        self.assertTrue(calls[0][1]["check"] is False)

    def test_probe_refuses_name_or_id_only_and_unavailable_tool(self) -> None:
        result = probe_vulkan(lambda argv, **_: subprocess.CompletedProcess(
            argv, 0, json.dumps({"deviceName": "AMD GPU", "deviceID": 123}), ""
        ))
        self.assertFalse(result.available)
        self.assertIn("não forneceu UUID associado", result.limitation or "")

        def missing_tool(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            raise FileNotFoundError("vulkaninfo")

        missing = probe_vulkan(missing_tool)
        self.assertFalse(missing.available)
        self.assertIn("indisponível", missing.limitation or "")

    def test_probe_reads_json_file_created_by_vulkaninfo_without_touching_cwd(self) -> None:
        document = {
            "devices": [{
                "deviceUUID": "gpu-uuid-file",
                "pciBusInfo": {"pciDomain": 0, "pciBus": 3, "pciDevice": 0, "pciFunction": 0},
                "vendorID": 0x1002,
                "deviceID": 0x744C,
            }]
        }

        def fake_bounded(
            argv: list[str], *, timeout: float, cwd: str | Path,
            env_overrides: dict[str, str] | None = None,
        ):
            self.assertEqual(argv[-1], "--json")
            self.assertEqual(timeout, 2.0)
            self.assertEqual(env_overrides, {})
            Path(cwd, "VP_VULKANINFO_gpu.json").write_text(json.dumps(document))
            return subprocess.CompletedProcess(argv, 0, "", "")

        with patch("core.web_gpu.shutil.which", return_value="/usr/bin/vulkaninfo"), patch(
            "core.web_gpu._run_vulkan_bounded", side_effect=fake_bounded
        ):
            probe = probe_vulkan(sysfs_root=self.sysfs)

        self.assertTrue(probe.available)
        self.assertEqual(probe.devices_by_bdf, {"0000:03:00.0": "gpu-uuid-file"})
        self.assertEqual(probe.pci_ids_by_bdf, {"0000:03:00.0": ("1002", "744c")})

    def test_probe_merges_nvidia_prime_devices_with_default_gpu_inventory(self) -> None:
        self.add_gpu("0000:00:02.0", vendor="0x8086", device="0x46a6")
        self.add_gpu("0000:01:00.0", vendor="0x10de", device="0x25e2", render_node="renderD129", driver="nvidia")
        calls: list[dict[str, str] | None] = []
        documents = {
            None: {
                "devices": [{
                    "deviceUUID": "intel-uuid",
                    "pciBusInfo": {"pciDomain": 0, "pciBus": 0, "pciDevice": 2, "pciFunction": 0},
                    "vendorID": 0x8086,
                    "deviceID": 0x46A6,
                }]
            },
            "nvidia": {
                "devices": [{
                    "deviceUUID": "nvidia-uuid",
                    "pciBusInfo": {"pciDomain": 0, "pciBus": 1, "pciDevice": 0, "pciFunction": 0},
                    "vendorID": 0x10DE,
                    "deviceID": 0x25E2,
                }]
            },
        }

        def fake_bounded(
            argv: list[str], *, timeout: float, cwd: str | Path,
            env_overrides: dict[str, str] | None = None,
        ):
            self.assertEqual(argv, ["/usr/bin/vulkaninfo", "--json"])
            self.assertEqual(timeout, 2.0)
            calls.append(env_overrides)
            key = "nvidia" if env_overrides else None
            Path(cwd, "VP_VULKANINFO_gpu.json").write_text(json.dumps(documents[key]))
            return subprocess.CompletedProcess(argv, 0, "", "")

        with patch("core.web_gpu.shutil.which", return_value="/usr/bin/vulkaninfo"), patch(
            "core.web_gpu._run_vulkan_bounded", side_effect=fake_bounded
        ):
            probe = probe_vulkan(sysfs_root=self.sysfs)

        self.assertTrue(probe.available)
        self.assertEqual(calls, [
            {},
            {"__NV_PRIME_RENDER_OFFLOAD": "1", "__VK_LAYER_NV_optimus": "NVIDIA_only"},
        ])
        self.assertEqual(probe.devices_by_bdf, {
            "0000:00:02.0": "intel-uuid",
            "0000:01:00.0": "nvidia-uuid",
        })
        self.assertEqual(probe.pci_ids_by_bdf, {
            "0000:00:02.0": ("8086", "46a6"),
            "0000:01:00.0": ("10de", "25e2"),
        })

        reims = ReimsProbe(True, (
            ReimsGpu("8086", "46a6", "/usr/share/vulkan/icd.d/intel_icd.json"),
            ReimsGpu("10de", "25e2", "/usr/share/vulkan/icd.d/nvidia_icd.json"),
        ))
        gpus = inventory_gpus(
            sysfs_root=self.sysfs,
            dev_root=self.dev,
            vulkan_probe_result=probe,
            reims_probe_result=reims,
        )
        self.assertEqual([gpu.selection_supported for gpu in gpus], [True, True])
        self.assertEqual([gpu.driver_file for gpu in gpus], [
            "/usr/share/vulkan/icd.d/intel_icd.json",
            "/usr/share/vulkan/icd.d/nvidia_icd.json",
        ])

    def test_probe_rejects_ambiguous_pci_uuid_mapping_and_oversized_output(self) -> None:
        one_gpu = {
            "deviceUUID": "uuid-a",
            "pciBusInfo": {"pciDomain": 0, "pciBus": 3, "pciDevice": 0, "pciFunction": 0},
        }
        other_gpu = {**one_gpu, "deviceUUID": "uuid-b"}
        ambiguous = probe_vulkan(lambda argv, **_: subprocess.CompletedProcess(
            argv, 0, json.dumps({"devices": [one_gpu, other_gpu]}), ""
        ))
        self.assertFalse(ambiguous.available)
        self.assertFalse(ambiguous.devices_by_bdf)

        oversized = probe_vulkan(lambda argv, **_: subprocess.CompletedProcess(
            argv, 0, " " * (_MAX_VULKAN_OUTPUT + 1), ""
        ))
        self.assertFalse(oversized.available)
        self.assertIn("limite seguro", oversized.limitation or "")

    def test_reims_probe_parses_manifest_and_device_ids_with_bounds(self) -> None:
        calls: list[tuple[list[str], dict[str, object]]] = []

        def fake_runner(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append((argv, kwargs))
            return subprocess.CompletedProcess(
                argv,
                0,
                "By default the library takes the first discrete GPU.\n"
                " 1. Radeon RX 7900 XTX  [1002:744c]  /usr/share/vulkan/icd.d/radeon_icd.x86_64.json\n",
                "",
            )

        probe = probe_reims_gpus(fake_runner)
        self.assertTrue(probe.available)
        self.assertEqual(len(probe.devices), 1)
        self.assertEqual(probe.devices[0].vendor_id, "1002")
        self.assertEqual(probe.devices[0].device_id, "744c")
        self.assertEqual(probe.devices[0].driver_file, "/usr/share/vulkan/icd.d/radeon_icd.x86_64.json")
        self.assertEqual(probe.devices[0].name, "Radeon RX 7900 XTX")
        self.assertEqual(calls[0][0], ["reims-ctl", "gpus"])
        self.assertEqual(calls[0][1]["timeout"], 2.0)

        oversized = probe_reims_gpus(lambda argv, **_: subprocess.CompletedProcess(
            argv, 0, "x" * (64 * 1024 + 1), ""
        ))
        self.assertFalse(oversized.available)
        self.assertIn("limite seguro", oversized.limitation or "")

        timeout = probe_reims_gpus(lambda *args, **kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired("reims-ctl gpus", 2.0)
        ))
        self.assertFalse(timeout.available)
        self.assertIn("tempo limite", timeout.limitation or "")

    def test_selection_maps_exact_bdf_to_reims_manifest_when_device_is_unique(self) -> None:
        self.add_gpu("0000:03:00.0")
        vulkan = VulkanProbe(
            True,
            {"0000:03:00.0": "gpu-uuid-1"},
            pci_ids_by_bdf={"0000:03:00.0": ("1002", "744c")},
        )
        reims = ReimsProbe(True, (
            # Device IDs are the runtime's Vulkan VK_LOADER_DEVICE_ID_FILTER values.
            ReimsGpu(
                "1002", "744c", "/usr/share/vulkan/icd.d/radeon_icd.x86_64.json"
            ),
        ))
        gpu = inventory_gpus(
            sysfs_root=self.sysfs,
            dev_root=self.dev,
            vulkan_probe_result=vulkan,
            reims_probe_result=reims,
        )[0]
        self.assertTrue(gpu.selection_supported)
        self.assertEqual(gpu.driver_file, "/usr/share/vulkan/icd.d/radeon_icd.x86_64.json")
        self.assertEqual(gpu.device_id, "744c")
        self.assertEqual(gpu.vulkan_uuid, "gpu-uuid-1")
        self.assertIsNone(gpu.limitation)

    def test_identical_physical_devices_remain_unselectable_even_with_exact_bdfs(self) -> None:
        self.add_gpu("0000:03:00.0", render_node="renderD128")
        self.add_gpu("0000:04:00.0", render_node="renderD129")
        vulkan = VulkanProbe(
            True,
            {"0000:03:00.0": "gpu-uuid-1", "0000:04:00.0": "gpu-uuid-2"},
            pci_ids_by_bdf={
                "0000:03:00.0": ("1002", "744c"),
                "0000:04:00.0": ("1002", "744c"),
            },
        )
        reims = ReimsProbe(True, (ReimsGpu(
            "1002", "744c", "/usr/share/vulkan/icd.d/radeon_icd.x86_64.json"
        ),))
        gpus = inventory_gpus(
            sysfs_root=self.sysfs,
            dev_root=self.dev,
            vulkan_probe_result=vulkan,
            reims_probe_result=reims,
        )
        self.assertEqual(len(gpus), 2)
        self.assertTrue(all(not gpu.selection_supported for gpu in gpus))
        self.assertTrue(all("homônimas" in (gpu.limitation or "") for gpu in gpus))
        self.assertTrue(all(gpu.vulkan_uuid for gpu in gpus))

    def test_inventory_serialization_matches_api_gpu_contract_fields(self) -> None:
        self.add_gpu("0000:03:00.0")
        data = inventory_gpus(
            sysfs_root=self.sysfs,
            dev_root=self.dev,
            vulkan_probe_result=VulkanProbe(False, {}, "not installed"),
            reims_probe_result=ReimsProbe(False, (), "sem Reims"),
        )[0].as_dict()
        contract_keys = {
            "pci_bdf", "vendor_id", "device_id", "name", "kind", "render_node",
            "vulkan_uuid", "driver_file", "vulkan_available", "used_by_host",
            "assigned_vm_id", "selection_supported", "limitation",
            "reims_device_uuid", "reims_pci_address", "reims_shares_driver",
        }
        self.assertEqual(set(data), contract_keys)

    def test_gpu_inventory_json_is_used_only_when_runtime_advertises_feature(self) -> None:
        payload = [{
            "name": "GPU", "vendor_id": "1002", "device_id": "744c",
            "device_uuid": "reims-uuid-1", "pci_address": "0000:03:00.0",
            "kind": "discrete", "driver_file": "/icd/amd.json", "shares_driver": True,
        }]
        calls: list[list[str]] = []

        def runner(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

        result = probe_reims_gpus(runner, schema_features={"gpu_inventory_json"})
        self.assertTrue(result.available)
        self.assertEqual(calls, [["reims-ctl", "gpus", "--json"]])
        self.assertEqual(result.devices[0].pci_address, "0000:03:00.0")
        self.assertEqual(result.devices[0].device_uuid, "reims-uuid-1")
        self.assertTrue(result.devices[0].shares_driver)

    def test_gpu_inventory_json_rejects_duplicate_or_incomplete_identity(self) -> None:
        from core.web_gpu import _parse_reims_gpu_json

        item = {
            "name": "GPU", "vendor_id": "1002", "device_id": "744c",
            "device_uuid": "uuid", "pci_address": "0000:03:00.0",
            "kind": "discrete", "driver_file": "/icd/amd.json", "shares_driver": False,
        }
        self.assertFalse(_parse_reims_gpu_json(json.dumps([item, item])).available)
        incomplete = dict(item)
        incomplete.pop("device_uuid")
        self.assertFalse(_parse_reims_gpu_json(json.dumps([incomplete])).available)

    def test_exact_reims_identity_supports_identical_pci_models_without_ambiguity(self) -> None:
        self.add_gpu("0000:03:00.0")
        self.add_gpu("0000:04:00.0")
        vulkan = VulkanProbe(
            True,
            {"0000:03:00.0": "vk-1", "0000:04:00.0": "vk-2"},
            pci_ids_by_bdf={"0000:03:00.0": ("1002", "744c"), "0000:04:00.0": ("1002", "744c")},
        )
        reims = ReimsProbe(True, (
            ReimsGpu("1002", "744c", "/icd/amd.json", "GPU A", "reims-a", "0000:03:00.0", True),
            ReimsGpu("1002", "744c", "/icd/amd.json", "GPU B", "reims-b", "0000:04:00.0", True),
        ))
        gpus = inventory_gpus(
            sysfs_root=self.sysfs, dev_root=self.dev,
            vulkan_probe_result=vulkan, reims_probe_result=reims,
        )
        self.assertEqual([gpu.reims_device_uuid for gpu in gpus], ["reims-a", "reims-b"])
        self.assertTrue(all(gpu.selection_supported for gpu in gpus))
        self.assertEqual([gpu.reims_pci_address for gpu in gpus], ["0000:03:00.0", "0000:04:00.0"])

    def test_inventory_provider_is_cached_and_refresh_is_single_flight(self) -> None:
        now = [0.0]
        calls: list[int] = []

        def provider():
            calls.append(1)
            return ["gpu-1"]

        cache = CachedGpuInventory(provider, ttl=30, monotonic=lambda: now[0])
        self.assertEqual(cache(), ["gpu-1"])
        self.assertEqual(cache(), ["gpu-1"])
        self.assertEqual(len(calls), 1)
        now[0] = 29.9
        cache()
        self.assertEqual(len(calls), 1)
        now[0] = 30.0
        cache()
        self.assertEqual(len(calls), 2)

    def test_inventory_cache_keeps_stale_display_snapshot_but_reports_refresh_failure(self) -> None:
        now = [0.0]
        failing = [False]
        calls = []

        def provider():
            calls.append(1)
            if failing[0]:
                raise OSError("probe failed")
            return ["pci:0000:03:00.0"]

        cache = CachedGpuInventory(provider, ttl=1, monotonic=lambda: now[0])
        self.assertEqual(cache(), ["pci:0000:03:00.0"])
        failing[0] = True
        now[0] = 2
        self.assertEqual(cache(), ["pci:0000:03:00.0"])
        self.assertEqual(cache(), ["pci:0000:03:00.0"])
        self.assertEqual(cache.last_error, "Sonda de GPU indisponível (OSError).")
        self.assertEqual(len(calls), 2)

    def test_inventory_cache_reports_initial_failure_without_inventing_devices(self) -> None:
        cache = CachedGpuInventory(lambda: (_ for _ in ()).throw(TimeoutError("probe failed")))
        self.assertEqual(cache(), [])
        self.assertEqual(cache.last_error, "Sonda de GPU indisponível (TimeoutError).")


if __name__ == "__main__":
    unittest.main()
