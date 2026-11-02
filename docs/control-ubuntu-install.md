# vHackintosh Control on Ubuntu

The first standalone Control package targets **Ubuntu 24.04 LTS, amd64**. It
installs the HTTPS frontend/API, the local privileged agent, and display
integration for VM windows. The web and agent services start automatically at
boot. The Reims vGPU/QEMU runtime is intentionally downloaded separately from
the configured `felipeab10/reims-vgpu` GitHub fork after the administrator
changes the initial password.

## Install

Download `vhackintosh-control_<version>_amd64.deb` from the GitHub release and
install it with:

```bash
sudo apt install ./vhackintosh-control_<version>_amd64.deb
```

Open `https://localhost:8443` on the host or `https://<host-address>:8443` from
another device on the same network. The browser will warn that the appliance
uses a locally generated TLS certificate; compare the fingerprint shown by
the service before trusting it.

The first account is `admin` with temporary password `vhackintosh`. The
dashboard requires a new password before granting access to the rest of the
Control API. After changing it, the dashboard opens the Reims runtime settings
so the user can select and prepare a published release from the fork. Once the
runtime is ready, create and install the macOS VM from the dashboard.

The package includes the pinned official `reims-util` needed to generate
APV-enabled OpenCore images. The Reims vGPU/QEMU runtime is still downloaded
separately, only after the signed-in administrator selects a published release
in Control.

## Host requirements

- Ubuntu 24.04 LTS on amd64 with systemd.
- Hardware virtualization enabled and `/dev/kvm` available.
- A Vulkan 1.2-capable GPU and the matching Ubuntu Vulkan driver for Reims vGPU.
- Network access for the first Reims release download and macOS recovery.
- Sufficient free disk, memory, and CPU resources for the selected guest.

When a local Wayland or X11 desktop session is active, VM windows open in that
session. The package discovers only the active session's display socket and
passes the required display variables to the VM service. On a console-only
system, it falls back to the package's dedicated shared Wayland compositor.
The appliance ISO continues to use its dedicated console session. The web
dashboard remains available over HTTPS while VMs are running.

## Services and diagnostics

```bash
systemctl status vhackintosh-web vhackintosh-agent vhackintosh-display
journalctl -u vhackintosh-web -u vhackintosh-agent -b
```

The administrator database, TLS identity, and downloaded VM data are stored
under `/var/lib/vhackintosh` and `/root/.config/vhackintosh`. Removing the
package does not delete VM disks or administrator data.
