"""Exercise the installed updater from a ProtectSystem=full systemd service."""

import subprocess
import sys
from pathlib import Path

from core.control_release import ControlRelease, install_release


package = Path(sys.argv[1]).resolve(strict=True)
version = subprocess.check_output(["dpkg-deb", "-f", str(package), "Version"], text=True).strip()
release = ControlRelease(
    tag=f"control-v{version}", name="CI self-update", published_at="", prerelease=True,
    asset_name=package.name, asset_url=package.as_uri(), asset_size=package.stat().st_size,
    checksum_url=(package.parent / "SHA256SUMS").as_uri(),
)
result = install_release(release, kind="deb", log=print, schedule_restart=True)
assert result.tag == release.tag
