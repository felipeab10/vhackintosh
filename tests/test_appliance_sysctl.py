from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ApplianceSysctlTests(unittest.TestCase):
    def test_reims_vgpu_mapping_limit_is_baked_into_live_and_installed_system(self):
        config = ROOT / "appliance/archiso/airootfs/etc/sysctl.d/90-reims-vgpu.conf"
        self.assertEqual(
            config.read_text(encoding="utf-8").splitlines()[-1],
            "vm.max_map_count=2097152",
        )
        profile = ROOT / "appliance/archiso/profiledef.sh"
        self.assertIn(
            '["/etc/sysctl.d/90-reims-vgpu.conf"]="0:0:644"',
            profile.read_text(encoding="utf-8"),
        )

        # The installer copies the live root filesystem to the target, so this
        # sysctl.d setting is retained by installed appliances as well.
        installer = ROOT / "appliance/archiso/airootfs/usr/local/bin/vhackintosh-install"
        self.assertIn(
            'vhack-copy-system "/" "/mnt/"',
            installer.read_text(encoding="utf-8"),
        )


if __name__ == "__main__":
    unittest.main()
