# Tarefa T08: GRUB Theme & Appliance ISO Builder

## Objetivo
Criar um tema customizado do GRUB com a identidade visual do vHackintosh e scripts para empacotar a imagem do Linux Appliance bootável com auto-login configurado.

## Itens a Desenvolver
- [ ] Tema do GRUB 2 com logo vHackintosh, fontes elegantes e fundo escuro minimalista.
- [ ] Configuração de Auto-Login no Linux:
  - Serviço systemd / getty para iniciar diretamente a CLI/TUI do `vhackintosh` no login do console.
- [ ] Script de geração de ISO do Appliance:
  - Criação de imagem ISO instalável com base mínima (Archiso / Debian live build) pré-configurada com `reims-vgpu`, QEMU e o manager.
