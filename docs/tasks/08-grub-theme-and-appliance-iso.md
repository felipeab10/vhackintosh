# Tarefa T08: GRUB Theme & Appliance ISO Builder

## Objetivo
Criar um tema customizado do GRUB com a identidade visual do vHackintosh e scripts para empacotar a imagem do Linux Appliance bootável com auto-login configurado, aceleração Vulkan para o reims-vgpu e Harnesses CLI de IA integrados.

## Itens Desenvolvidos
- [x] Tema do GRUB 2 com identidade visual minimalista vHackintosh (Dark Apple Silicon aesthetic):
  - Fundo sutil obsidian com gradiente e logo (`background.png`).
  - Barra de seleção com bordas em ciano neon (`select_c.png`).
  - `theme.txt` e `grub.cfg` configurados com suporte a resolução 1080p, modo diagnóstico e opções UEFI.
- [x] Configuração de Auto-Login no Linux Appliance:
  - Drop-in systemd `getty@tty1.service.d/autologin.conf` para auto-login sem senha no console.
  - Script `/etc/profile.d/vhackintosh.sh` para acionar automaticamente o Kiosk vHackintosh no tty1.
  - Otimizações de Kernel e KVM em `/etc/modprobe.d/kvm.conf` (`ignore_msrs=1`, `nested=1`).
  - Limites de memória (`limits.d/99-vhackintosh.conf`) com memlock ilimitado para performance máxima do QEMU.
- [x] Integração de AI Coding Harnesses CLI:
  - Script dedicado `setup-harness-tools.sh` para instalação e atualização automática de ferramentas:
    - **Claude Code CLI** (`@anthropic-ai/claude-code`)
    - **OpenAI Codex CLI** (`@openai/codex`)
    - **OpenCode CLI** (`opencode-cli`)
  - Pacotes Node.js, npm, pnpm, Python, ripgrep, fd, bat, fzf e jq incluídos em `packages.x86_64`.
- [x] Scripts de Geração e Verificação de ISO:
  - `appliance/build-iso.sh`: Script automatizado com checagem de root, sincronização de código, empacotamento do reims-vgpu e geração de squashfs ZSTD-19 via Archiso.
  - `appliance/verify-iso.sh`: Utilitário para teste instantâneo de inicialização da ISO gerada dentro do QEMU local com firmware OVMF UEFI.
- [x] Interface TUI vHackintosh:
  - Opção `7 - Gerador de Imagem ISO Bootável (Appliance Live USB com AI Harnesses)` integrada ao menu principal.
