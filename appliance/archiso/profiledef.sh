#!/usr/bin/env bash
# shellcheck disable=SC2034

iso_name="vhackintosh-os"
iso_label="VHACK_$(date --date="@${SOURCE_DATE_EPOCH:-$(date +%s)}" +%Y%m)"
iso_publisher="vHackintosh Project <https://github.com/felipeab10/vhackintosh>"
iso_application="vHackintosh Appliance - Dedicated Linux for macOS VMs"
iso_version="1.1.1"
install_dir="arch"
buildmodes=('iso')
bootmodes=('uefi.grub')
arch="x86_64"
pacman_conf="pacman.conf"
airootfs_image_type="squashfs"
airootfs_image_tool_options=('-comp' 'zstd' '-Xcompression-level' '19')
file_permissions=(
  ["/etc/shadow"]="0:0:400"
  ["/root"]="0:0:750"
  ["/usr/local/bin/vhackintosh"]="0:0:755"
  ["/usr/local/bin/setup-harness-tools.sh"]="0:0:755"
  ["/usr/local/bin/vhackintosh-install"]="0:0:755"
  ["/usr/local/bin/vhackintosh-mode"]="0:0:755"
  ["/usr/local/bin/dmg2img"]="0:0:755"
  ["/usr/local/bin/vhack-copy-system"]="0:0:755"
  ["/usr/local/bin/vhackintosh-waysession"]="0:0:755"
  # O reims-util é instalado em tempo de build de ISO
  # (appliance/build-iso.sh) e sem esta linha chega na ISO como 0644 — a
  # validação falha com "bundled reims-util is not executable in ISO rootfs",
  # que foi exatamente o que a 1.1.0 mostrou em 16/10. Toda entrada nova em
  # /usr/local/bin precisa da sua linha aqui.
  ["/usr/local/bin/reims-util"]="0:0:755"
  # Atalho para o QEMU pré-compilado da release, instalado por build-iso.sh.
  # Sem esta linha o `install` de
  # 0755 é desfeito pelo `cp -af --no-preserve=...mode` do mkarchiso e o atalho
  # chega na ISO como 0644, sem poder ser executado.
  ["/usr/local/bin/reims-vgpu-qemu"]="0:0:755"
  ["/etc/sysctl.d/90-reims-vgpu.conf"]="0:0:644"
  ["/etc/vhackintosh/sway.conf"]="0:0:644"
  ["/etc/systemd/system/getty@.service.d/autologin.conf"]="0:0:644"
  ["/opt/vhackintosh/bin/"]="0:0:755"
  ["/opt/vhackintosh/bin/vhackintosh"]="0:0:755"
  ["/opt/vhackintosh/resources/macserial/macserial"]="0:0:755"
  ["/opt/vhackintosh/themes/grub/install.sh"]="0:0:755"
)
