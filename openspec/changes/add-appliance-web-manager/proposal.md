## Why

O vHackintosh precisa oferecer administração remota do appliance Linux sem depender de um terminal diante do computador. Um painel inspirado na organização do Proxmox, com a identidade visual do próprio projeto, permitirá configurar, provisionar e acompanhar VMs macOS e o host pelo celular ou desktop, preservando a integração existente com QEMU, OpenCore e Reims vGPU.

## What Changes

- Adicionar painel web autenticado, executado automaticamente no appliance e acessível pela LAN, com endereço e estado exibidos no script/TUI.
- Oferecer visão geral do host, inventário de VMs, assistente de criação, edição de hardware, downloads, tarefas, logs, armazenamento e diagnósticos.
- Expor todas as opções atuais do vHackintosh em formulários, complementados por configuração QEMU avançada com argumentos estruturados, descoberta de capacidades do binário instalado e prévia da configuração efetiva. Conforme escolha do usuário, não será necessário criar um campo visual para cada flag rara do QEMU.
- Permitir várias VMs simultâneas quando memória, dispositivos, rede e sessão gráfica comportarem a execução; manter reserva de recursos e impedir partidas duplicadas entre painel e terminal.
- **BREAKING**: no modo gerenciado, desligar/reiniciar o macOS ou usar os controles da VM afetará somente aquela VM por padrão. Sincronizar energia do host será uma preferência explícita e restrita ao modo kiosk exclusivo, sempre desativada durante instalação e quando outra VM estiver ativa.
- Selecionar e trocar a GPU física utilizada pelo Reims com identificação por dispositivo, distinguindo aceleração Vulkan compartilhável de passthrough VFIO exclusivo. Mudanças exigirão VM desligada; não haverá fallback silencioso para outra placa.
- Mostrar CPU, memória, armazenamento, rede e sensores disponíveis do host, além de consumo e estado das VMs, em tempo real, com fontes, unidades, indisponibilidades e histórico limitado identificados.
- Acrescentar operações de proteção de armazenamento: backup e restauração offline, clonagem com nova identidade e snapshots de disco somente nas combinações suportadas.
- Definir tema futurista/HUD inspirado em https://vhackintosh.proclin.com.br, com fundo quase preto, azul/ciano, brilho discreto, tabelas legíveis e navegação responsiva e acessível.

## Capabilities

### New Capabilities

- `web-access-security`: acesso HTTPS, cadastro inicial local, sessões administrativas, autorização, proteção das operações e separação entre API e agente do host.
- `web-vm-management`: catálogo compartilhado, criação, edição, importação, ciclo de vida, concorrência, tarefas, logs e política de energia por VM.
- `qemu-configuration`: formulários, capacidades versionadas, argumentos avançados e validação da configuração efetiva sem perder opções entre reinícios.
- `reims-gpu-assignment`: inventário gráfico, seleção exata, troca offline, diagnóstico Vulkan/VFIO e verificação da GPU efetiva.
- `realtime-host-vm-telemetry`: coleta única, séries limitadas e transmissão de telemetria do host e das VMs com semântica verificável.
- `vm-storage-protection`: imagens, discos, backups, restauração, clonagem e snapshots offline compatíveis.
- `appliance-web-service`: serviços persistentes, integração ISO/instalação/atualização, descoberta do endereço e convivência com console e kiosk.
- `responsive-manager-interface`: arquitetura das páginas, design system futurista do projeto, acessibilidade e comportamento mobile/desktop.

### Modified Capabilities

Nenhuma spec principal existe em `openspec/specs/` neste checkout. Há uma proposta ativa em `appliance-i18n-and-device-integration/specs/guest-power-controls/spec.md`: sua escolha explícita de sincronização será preservada, mas subordinada à proteção de outras VMs e ao modo kiosk exclusivo. Essa reconciliação deverá ocorrer antes de arquivar as mudanças; esta proposta não altera os artefatos daquela mudança.

## Impact

- Reaproveitar e separar da UI a lógica em `core/config.py`, `core/reims_ctl.py`, `core/disk.py`, `core/install_phase.py`, `core/power.py`, `core/passthrough.py`, `core/gpu.py`, `core/smbios.py`, `core/readiness.py`, `ui/tui.py` e `cli/main.py`.
- Adicionar API/agent Python e frontend React/TypeScript compilado estaticamente; FastAPI/Uvicorn, Pydantic, psutil, SQLite, Vite, Tailwind, componentes acessíveis e gráficos leves são as escolhas propostas em `design.md`.
- Atualizar posteriormente o empacotamento Archiso, serviços systemd, launcher Wayland, instalador em disco, validação da ISO e documentação. O appliance não precisará de Node.js em execução.
- A seleção exata entre GPUs idênticas e opções que o perfil atual do `reims-ctl` não representa podem exigir uma extensão versionada do runtime Reims, entregue em seu repositório e empacotada como dependência. Esse trabalho é um marco explícito, não uma capacidade presumida.
- Escopo: um host Linux e convidados macOS; cluster, HA, migração ao vivo, instalação bare-metal pelo navegador e desktop macOS transmitido pelo Reims no navegador ficam fora desta mudança.
- Esta entrega contém apenas planejamento. Não modifica código, VMs existentes, serviços, discos ou a alteração local em `ui/banner.py`.
