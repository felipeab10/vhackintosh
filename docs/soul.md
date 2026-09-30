# vHackintosh — Soul & Living Memory

> **"A experiência de um Mac nativo, com aceleração gráfica paravirtualizada por hardware, rodando sobre um Appliance Linux minimalista."**

---

## 1. Visão e Propósito

O **vHackintosh** é uma distribuição e gerenciador de máquinas virtuais (*Linux Appliance & VM Manager*) projetado para transformar qualquer computador compatível em uma estação de trabalho macOS de alta performance através de virtualização KVM e aceleração gráfica paravirtualizada via **Reims vGPU (Metal over Vulkan)** ou **VFIO Passthrough**.

O objetivo final é entregar a experiência de um console (*Kiosk Mode* tipo SteamOS/Proxmox), onde o sistema operacional subjacente (Linux mínimo) é transparente para o usuário:
- **Zero Atrito:** Auto-login no boot, menu elegante com contagem regressiva de 5 segundos (com tecla `ESPAÇO` para abrir o gerenciador).
- **Tela Cheia Kiosk:** macOS inicia direto em Fullscreen 100%, com captura exclusiva de teclado e mouse.
- **Sincronização Total de Energia:** Desligar ou reiniciar o macOS desliga ou reinicia a máquina física automaticamente.
- **Auto-Configuração Inteligente:** Sugestão automática de vCPUs e RAM com base na topologia da CPU (P-Cores vs E-Cores) e checagem de compatibilidade de GPU.
- **OpenCore Automatizado:** Injeção automática de seriais gerados pelo GenSMBIOS no `config.plist` e detecção automática da partição de boot.

---

## 2. Decisões Arquiteturais e Pilares Técnicos

### 2.1. Gráficos & Aceleração
- **Reims vGPU como Padrão:** Toda a pilha de renderização (desde o instalador OpenCore até a interface gráfica do macOS) utiliza o `reims-vgpu-pci` com a ROM UEFI GOP (`reims-vgpu-gop.rom`) e backend Vulkan.
- **Backend X11/Xwayland:** Por padrão, o ambiente gráfico do host utiliza modo X11/Xwayland para evitar engasgos de *frame pacing* no driver NVIDIA/Wayland.
- **Opção de Passthrough VFIO:** Para GPUs AMD dedicadas suportadas nativamente pelo macOS, o sistema oferece isolamento de grupo IOMMU e stub do `vfio-pci`.

### 2.2. Gestão de VMs & Exclusividade
- **Múltiplas VMs, Uma Ativa:** O usuário pode criar e armazenar várias VMs (ex: macOS Tahoe, macOS Sequoia, Sonoma), mas apenas uma pode ser executada por vez para garantir 100% dos recursos dedicados.
- **Auto-Start Interceptável:** Uma VM pode ser marcada como padrão de inicialização. No boot do host, um contador de 5 segundos aguarda a tecla `ESPAÇO`. Se pressionada, o gerenciador abre; se expirar, a VM sobe imediatamente.

### 2.3. Compilação Silenciosa com UX Rica
- Qualquer build (`reims-vgpu`, submódulos QEMU, ROMs EFI) oculta saídas brutas do terminal e exibe barras de progresso modernas e animadas via **Rich**.

### 2.4. Ciclo de Vida da Instalação (State Machine)
A instalação do macOS realiza múltiplos reboots que o gerenciador orquestra via socket QMP e porta serial:
1. `INICIANDO` (Boot OpenCore)
2. `EM_INSTALACAO` (BaseSystem / Recovery da Apple)
3. `REINICIANDO_POS_INSTALACAO` (Estágio 2 / Cópia de arquivos do sistema)
4. `INSTALACAO_CONCLUIDA` (Finalização de provisionamento)
5. `INICIANDO_MACOS` (Desktop pronto para uso diário)
6. `ERRO_AO_INICIAR` (Captura de kernel panic com diagnóstico)

---

## 3. Repositórios e Módulos Integrados

| Módulo | Repositório / Fonte | Função |
|---|---|---|
| **vHackintosh** | `felipeab10/vhackintosh` | Appliance, VM Manager, TUI, Auto-tuner e Orquestrador |
| **Reims vGPU** | `felipeab10/reims-vgpu` (fork de `steelbrain/reims-vgpu`) | Driver de vGPU Vulkan para macOS KVM |
| **OpenCore EFI Base** | OSX-KVM / Custom EFI | Base do bootloader com Kexts e ACPI |

---

## 4. Registro de Decisões & Evolução

- **2026-09-30:** Criação da estrutura do projeto `felipeab10/vhackintosh`.
- **2026-09-30:** Padronização de `reims-vgpu` desde a fase de instalação com injeção automática de GenSMBIOS.
- **2026-09-30:** Definição do Kiosk Mode com sincronização bidirecional de energia (Host Power Sync).
