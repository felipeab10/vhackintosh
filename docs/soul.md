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

### 2.5. Arquitetura de Áudio (Padrão ultimate-macOS-KVM + Dortania)
- **Problema do OSX-KVM Padrão:** O OSX-KVM utiliza `usb-audio` no controlador `qemu-xhci`. No Linux KVM, o áudio isócrono USB sofre com forte jitter de tempo, estouro de buffer e descarte de pacotes (`streambuf_put`), causando estalos contínuos (*crackles*), som robotizado ou ausência total de som.
- **Solução Validada (ultimate-macOS-KVM):** Utilização de controlador nativo PCI Intel High Definition Audio (`ich9-intel-hda`) com codec duplex (`hda-duplex`), mapeado para o servidor de áudio do host (PipeWire / PulseAudio via `/run/user/$UID/pulse/native`).
- **OpenCore / Dortania:** No OpenCore, o `AppleALC` (ou layout nativo AppleHDA) reconhece o barramento HDA PCI sem necessidade de gambiarras USB, entregando áudio limpo, estéreo sem latência e volume de sistema funcional.

### 2.6. Auto-Downloader de Imagens do macOS (Apple SUS Recovery)
- **Download Oficial Apple:** O vHackintosh integra cliente de comunicação direta com os servidores de atualização e recuperação da Apple (`osrecovery.apple.com` e `oscdn.apple.com`).
- **Escopo Inicial de Versões Suportadas:**
  - 🍏 **macOS Tahoe (26):** Próxima geração do macOS com aceleração via Reims vGPU experimental.
  - 🍏 **macOS Sequoia (15):** Versão atual estável e recomendada para produção.
  - 🍏 **macOS Ventura (13):** Versão de referência de alta estabilidade e baixo consumo.
- **Pipeline Automatizado:** Download de `BaseSystem.dmg` com barra de progresso visual em tempo real (velocidade, ETA e tamanho via Rich) e conversão transparente para disco virtual `BaseSystem.img` via `dmg2img`. Cache local em `~/.config/vhackintosh/images/` para evitar downloads repetidos.

### 2.7. Provisionamento Automático de Armazenamento e OpenCore EFI
- **Discos Virtuais Dinâmicos:** Cada VM criada recebe seu próprio armazenamento em `~/.config/vhackintosh/vms/{vm_id}/hdd.qcow2`, com alocação dinâmica (*thin provisioning* e cluster size de 2M otimizado).
- **Injeção Transparente na Partição EFI:** Utilização do `guestfish` para abrir diretamente a partição EFI (`/dev/sda1`) da imagem `OpenCore.qcow2` da VM e gravar o `config.plist` modificado com os seriais do GenSMBIOS e boot-args de áudio sem necessidade de privilégios de superusuário (`sudo`).

### 2.8. VMs Registradas no Host (Sequoia e Tahoe)
- **macOS Sequoia 15 (`macos-sequoia`):**
  - Armazenamento: `vm/disks/rails/sequoia/persistent/macos.qcow2` (36 GB gravados).
  - Alocação: 12 vCPUs, 8 GB RAM, backend Vulkan, tela cheia sem bordas (`REIMS_VGPU_FULLSCREEN=1`).
  - Padrão de Auto-Start: Definido como VM padrão para inicialização com interceptador de 5 segundos.
- **macOS Tahoe 26 (`macos-tahoe`):**
  - Armazenamento: `vm/disks/rails/tahoe/persistent/macos.qcow2` (42 GB gravados).
  - Alocação: 12 vCPUs, 8 GB RAM, backend Vulkan experimental.

### 2.9. Motor de Teste A/B de Instalação do Zero (`core/installer_runner.py`)
- **Validação Isolada:** Permite testar o fluxo completo de instalação oficial da Apple do zero, sem afetar ou alterar as VMs de produção já instaladas no disco.
- **Comparações A/B de Áudio:** Alternância em tempo real entre o padrão moderno `ich9-intel-hda` + `hda-duplex` e o legado `usb-audio`.
- **Modos de Exibição:** Suporte a execução em Janela (para depuração e observação de logs) ou Fullscreen Kiosk.

### 2.10. Seletor Inteligente Dual-GPU e Suporte Multi-Vendor (NVIDIA, AMD e Intel)
- **Detecção de Gráficos Híbridos:** Identificação automática de todas as GPUs presentes no barramento PCI do host (Intel Iris Xe / UHD, NVIDIA GeForce GTX/RTX Mobile/Desktop e AMD Radeon RX).
- **PRIME Render Offload Dinâmico:**
  - **NVIDIA:** Ativação automática de `__NV_PRIME_RENDER_OFFLOAD=1`, `__GLX_VENDOR_LIBRARY_NAME=nvidia` e injeção do arquivo ICD proprietário `nvidia_icd.json` para renderização Vulkan na GPU dedicada.
  - **AMD:** Ativação de `DRI_PRIME=1` e apontamento do driver Vulkan Mesa RADV (`radeon_icd.x86_64.json`) ou AMDVLK.
  - **Intel:** Utilização do driver Mesa ANV com aceleração vGPU Vulkan.
- **VFIO Passthrough Nativo para AMD:** Para placas desktop AMD Polaris (RX 400/500), Vega e Navi (RX 5000/6000), o sistema oferece vinculação direta via `vfio-pci` com 100% de suporte nativo Apple Metal 3, DisplayPort áudio e aceleração bare-metal.

### 2.11. Resolução do Kernel Handoff em `#[EB|LOG:EXITBS:START]`
- **Causa Raiz Identificada:** A parada no milissegundo de transição `ExitBootServices: START` entre o firmware UEFI e o Kernel XNU decorre da checagem obrigatória do chip de segurança AppleSMC e da sincronização de timers de CPU.
- **Parâmetros Mandatórios do QEMU:**
  - Emulação do chip com chave OSK: `-device isa-applesmc,osk="ourhardworkbythesewordsguardedpleasedontsteal(c)AppleComputerInc"`
  - Bloqueio de colapso de suspensão ACPI S3/S4: `-global ICH9-LPC.disable_s3=1` e `-global ICH9-LPC.disable_s4=1`
  - Sincronização de timer de CPU: `vmware-cpuid-freq=on` com perfil AVX2 e exclusão de instruções problemáticas (`-hle,-rtm`)
  - Identificação de placa Apple: `-smbios type=2`

### 2.12. Instalador Bare-Metal da Appliance Linux (`vhackintosh-install`)
- **Particionamento Automatizado & Manual:** Particionamento limpo de SSDs NVMe/SATA em GPT com tabela EFI de 1 GB (`boot,esp`) e partição raiz ext4.
- **Cópia de Sistema com Barra Rich:** Substituição de logs rsync por barra animada em tempo real com taxa de transferência (MB/s), porcentagem e cálculo de tempo restante (`vhack-copy-system`).
- **Bootloader de Resiliência:** Instalação do GRUB no caminho padrão de contingência UEFI (`/boot/efi/EFI/BOOT/BOOTX64.EFI`) com tema escuro Apple Dark integrado.
- **Gerador de Imagem ISO (`appliance/build-iso.sh`):** Empacotamento com diretório de trabalho em `/var/tmp` para evitar exaustão de memória em discos voláteis `tmpfs`.

### 2.13. Portal de Distribuição & Landing Page (`vhackintosh-lp`)
- **Design System UI/UX Pro Max:** Interface moderna em tema Dark Sci-Fi HUD / Apple Minimalist construída em Next.js 16 (App Router), React 19, Tailwind CSS v4 e Lucide Icons.
- **Terminal Interativo:** Demonstração interativa de hardware com Intel 12ª geração, Dual-GPU e boot KVM na landing page.
- **Distribuição de Imagens ISO de até 4 GB:**
  - Motor de upload particionado (*Chunked Upload*) em blocos de 4 MB com barra de progresso visual, imune a limites de timeout e proxy reverso.
  - Limpeza automática de versões anteriores no storage após novo upload.
  - Download público via streaming com suporte a Range headers e contador global de downloads persistente.
  - Painel de administração autenticado (`/admin`) com criptografia bcrypt para senhas e suporte nativo a deploy no Coolify via Docker standalone.

### 2.14. Sincronização em Tempo Real de Energia (Host Power Sync)
- **QMP Socket Daemon (`core/power.py`):** Monitora eventos QMP em segundo plano (`/tmp/vhackintosh-qmp.sock`).
- **Desligamento Unificado:** Ao selecionar "Desligar..." no macOS, o QEMU emite o evento `SHUTDOWN` com reason `guest-shutdown`. O host Linux executa `systemctl poweroff` e desliga a máquina física em 2 segundos.
- **Reinicialização Unificada:** Ao selecionar "Reiniciar..." no macOS, o QEMU aciona `-no-reboot` e emite `guest-reset`. O host Linux executa `systemctl reboot` e reinicia o computador físico, retornando pelo Kiosk auto-boot do vHackintosh.

### 2.15. Auto-Boot Direto do OpenCore com Revelação por Tecla de Atalho (Spacebar Hotkey)
- **Configuração Silenciosa do Bootloader:** `ShowPicker = False`, `Timeout = 0`, `PollAppleHotKeys = True`, `AllowSetDefault = True`.
- **Comportamento Apple Nativo:** O macOS inicia diretamente no volume padrão (`OSX`) sem telas intermediárias. Segurar ou pressionar a **barra de espaço** ou **Option/Alt** durante a inicialização abre o menu gráfico completo do OpenCore.
- **Injeção Dinâmica via Guestfish:** O vHackintosh altera essa configuração in-place dentro de `OpenCore.qcow2` através do menu de gerenciamento da VM (opção 3).

### 2.16. Pipeline de Distribuição Contínua por GitHub Releases & Auto-Updater
- **GitHub Actions CI (`felipeab10/reims-vgpu`):** Workflow `.github/workflows/release.yml` compila na nuvem a ROM UEFI GOP (Rust) e o QEMU com backend Vulkan, empacotando em `reims-vgpu-linux-x86_64.tar.gz` com checksum SHA-256 em cada tag/release.
- **Auto-Updater Inteligente (`core/updater.py`):** O comando `vhackintosh update` consulta a API de Releases do GitHub, baixa e descompacta os binários pré-compilados em `/opt/reims-vgpu/` com barra de progresso visual no Rich e executa `git pull` no vHackintosh, atualizando o sistema inteiro em menos de 10 segundos sem compilação local.

---

## 3. Wikis e Documentação de Referência

O projeto segue as melhores práticas e especificações técnicas documentadas nas wikis e guias oficiais:
- 📚 **ultimate-macOS-KVM Wiki:** [https://github.com/Coopydood/ultimate-macOS-KVM/wiki](https://github.com/Coopydood/ultimate-macOS-KVM/wiki)
- 📚 **Dortania OpenCore Install & Post-Install Guide:** [https://dortania.github.io/docs/](https://dortania.github.io/docs/)
  - *OpenCore Post-Install Universal Audio:* [https://dortania.github.io/OpenCore-Post-Install/universal/audio.html](https://dortania.github.io/OpenCore-Post-Install/universal/audio.html)
  - *OpenCore Configuration Guide:* [https://dortania.github.io/OpenCore-Install-Guide/](https://dortania.github.io/OpenCore-Install-Guide/)

---

## 4. Repositórios e Módulos Integrados

| Módulo | Repositório / Fonte | Função |
|---|---|---|
| **vHackintosh** | `felipeab10/vhackintosh` | Appliance, VM Manager, TUI, Auto-tuner e Orquestrador |
| **Reims vGPU** | `felipeab10/reims-vgpu` (fork de `steelbrain/reims-vgpu`) | Driver de vGPU Vulkan para macOS KVM |
| **OpenCore EFI Base** | OSX-KVM / Custom EFI | Base do bootloader com Kexts e ACPI |

---

## 5. Registro de Decisões & Evolução

- **2026-09-30:** Criação da estrutura do projeto `felipeab10/vhackintosh`.
- **2026-09-30:** Padronização de `reims-vgpu` desde a fase de instalação com injeção automática de GenSMBIOS.
- **2026-09-30:** Definição do Kiosk Mode com sincronização bidirecional de energia (Host Power Sync).
- **2026-09-30:** Adoção oficial das wikis **ultimate-macOS-KVM** e **Dortania** como referências vivas.
- **2026-09-30:** Substituição do áudio `usb-audio` bugado do OSX-KVM pelo `ich9-intel-hda` + `hda-duplex` do ultimate-macOS-KVM.
- **2026-09-30:** Implementação do Auto-Downloader oficial Apple Recovery (SUS) com conversão `dmg2img` e cache local.
- **2026-09-30:** Implementação do Provisionador de Armazenamento QCOW2 e injeção EFI in-place via guestfish (`core/disk.py`).
- **2026-09-30:** Integração e cadastro no catálogo das VMs existentes no host (macOS Sequoia 15 e macOS Tahoe 26) com suporte a `REIMS_VGPU_FULLSCREEN=1`.
- **2026-09-30:** Implementação do Motor de Teste A/B de Instalação do Zero (`core/installer_runner.py`) com alternância entre `ich9-intel-hda` e `usb-audio`.
- **2026-09-30:** Implementação do Seletor Inteligente Dual-GPU para notebooks com Intel Iris Xe e NVIDIA Optimus PRIME / AMD RADV Vulkan offloading.
- **2026-09-30:** Suporte de primeira classe para placas de vídeo AMD Radeon (Mesa RADV Vulkan e VFIO PCI Passthrough bare-metal 100% nativo).
- **2026-09-30:** Diagnóstico e resolução da trava de kernel handoff em `#[EB|LOG:EXITBS:START]` via injeção de dispositivo `isa-applesmc` com chave OSK da Apple, `smbios type=2`, `vmware-cpuid-freq=on` e bloqueio de suspensão ACPI.
- **2026-09-30:** Criação do instalador bare-metal `vhackintosh-install` com particionador GPT automático/manual e barra de progresso visual animada via Rich (`vhack-copy-system`).
- **2026-09-30:** Criação da imagem de distribuição ISO em `/var/tmp` para prevenção de estouro de memória em discos `tmpfs`.
- **2026-09-30:** Criação do repositório privado `felipeab10/vhackintosh-lp` contendo a Landing Page oficial e Portal de Distribuição em Next.js 16 com sistema de design Dark Cyber HUD / Apple Minimalist.
- **2026-09-30:** Implementação do sistema de upload particionado (*Chunked Upload*) para ISOs de até 4 GB com limpeza automática de versões anteriores, contador global de downloads e painel administrativo `/admin` autenticado.
- **2026-09-30:** Configuração de deploy no Coolify via Dockerfile standalone, entrypoint seguro de permissões de volume persistente e liberação de builds pnpm.
