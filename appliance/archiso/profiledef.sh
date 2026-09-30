#!/usr/bin/env bash
# shellcheck disable=SC2034

iso_name="vhackintosh-os"
iso_label="VHACK_$(date --date="@${SOURCE_DATE_EPOCH:-$(date +%s)}" +%Y%m)"
iso_publisher="vHackintosh Project <https://github.com/felipeab10/vhackintosh>"
iso_application="vHackintosh Appliance - Dedicated Linux for macOS VMs"
iso_version="1.0.0"
install_dir="arch"
buildmodes=('iso')
bootmodes=('bios.syslinux.mbr' 'bios.syslinux.eltorito'
           'uefi-ia32.systemd-boot.esp' 'uefi-x86_64.systemd-boot.esp'
           'uefi-ia32.systemd-boot.eltorito' 'uefi-x86_64.systemd-boot.eltorito')
arch="x86_64"
pacman_conf="pacman.conf"
airootfs_image_type="squashfs"
airootfs_image_tool_options=('-comp' 'zstd' '-Xcompression-level' '19')
file_permissions=(
  ["/etc/shadow"]="0:0:400"
  ["/etc/gshadow"]="0:0:400"
  ["/root"]="0:0:750"
  ["/usr/local/bin/vhackintosh"]="0:0:755"
  ["/usr/local/bin/setup-harness-tools.sh"]="0:0:755"
)
