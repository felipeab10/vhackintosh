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
  # Verificação headless do build do reims-vgpu, acionada por `vhack_build_test=1`
  # no cmdline (appliance/verify-iso.sh --build-test). Sem esta linha o script
  # chega 0644 e o hook do profile.d o rejeita no teste `[ -x ]`.
  ["/usr/local/bin/verify-build-in-guest.sh"]="0:0:755"
  # O reims-util é instalado em tempo de build de ISO
  # (appliance/build-iso.sh) e sem esta linha chega na ISO como 0644 — a
  # validação falha com "bundled reims-util is not executable in ISO rootfs",
  # que foi exatamente o que a 1.1.0 mostrou em 16/10. Toda entrada nova em
  # /usr/local/bin precisa da sua linha aqui.
  ["/usr/local/bin/reims-util"]="0:0:755"
  ["/usr/local/bin/vhackintosh-update-reims-util"]="0:0:755"
  # Atalho para o QEMU pré-compilado da release (instalado por build-iso.sh no
  # modo padrão). Mesmo problema do reims-util: sem esta linha o `install` de
  # 0755 é desfeito pelo `cp -af --no-preserve=...mode` do mkarchiso e o atalho
  # chega na ISO como 0644, sem poder ser executado.
  ["/usr/local/bin/reims-vgpu-qemu"]="0:0:755"
  ["/etc/sysctl.d/90-reims-vgpu.conf"]="0:0:644"
  ["/etc/vhackintosh/sway.conf"]="0:0:644"
  ["/etc/systemd/system/getty@.service.d/autologin.conf"]="0:0:644"
  ["/opt/vhackintosh/bin/"]="0:0:755"
  ["/opt/vhackintosh/bin/vhackintosh"]="0:0:755"
  ["/opt/vhackintosh/appliance/update-reims-util.sh"]="0:0:755"
  ["/opt/vhackintosh/resources/macserial/macserial"]="0:0:755"
  ["/opt/vhackintosh/themes/grub/install.sh"]="0:0:755"
  # O mkarchiso copia o airootfs com `cp -af --no-preserve=ownership,mode` e só
  # reaplica os modos listados aqui — e esta lista **não aceita glob**. Sem as
  # duas linhas abaixo, todo /opt/reims-vgpu chega na ISO como 0644, e o
  # `./configure` do QEMU (invocado pelo qemu-build.sh) morre com
  # "Permission denied". Uma barra final faz o mkarchiso aplicar `chmod -R`.
  # Efeito colateral aceito: arquivos de dados dessas árvores também ficam 755,
  # o que é inofensivo. O `core/reims_build.py` restaura os bits exatos em
  # tempo de execução, então ISOs já gravadas também funcionam.
  ["/opt/reims-vgpu/vendor/qemu/"]="0:0:755"
  ["/opt/reims-vgpu/scripts/"]="0:0:755"
  # `crates/` e `vm/` faltavam. O `vm/boot-x86.sh` executa diretamente
  # `crates/reims-vgpu-efi/scripts/.../reims-vgpu-efi-rom.sh` (com `[ -x ]`
  # antes, em require_shader_toolchain: "EFI ROM builder not executable") — e na
  # ISO anterior ele chegou 0644.
  ["/opt/reims-vgpu/crates/"]="0:0:755"
  ["/opt/reims-vgpu/vm/"]="0:0:755"
)
