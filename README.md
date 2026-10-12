# vHackintosh 🍏⚡

> **Dedicated Linux Appliance & macOS VM Manager powered by KVM and Reims vGPU**

O **vHackintosh** é uma solução completa em formato *Appliance / Kiosk OS* para transformar qualquer computador compatível em uma estação de trabalho macOS de alta performance, utilizando virtualização KVM e aceleração gráfica paravirtualizada por hardware via **Reims vGPU (Metal over Vulkan)** ou **VFIO Passthrough**.

---

## ✨ Principais Recursos

- 🖥 **Kiosk Mode Nativo (Console Experience):**
  - Execução direta em **100% Fullscreen** com captura exclusiva de teclado e mouse.
  - **Sincronização de Energia Bidirecional:** Desligar ou reiniciar o macOS desliga ou reinicia a máquina física.
- ⚡ **Auto-Start com Interceptador de 5 Segundos:**
  - Inicialização automática da sua VM principal no boot.
  - Pressione `[ BARRA DE ESPAÇO ]` durante a contagem para abrir o Gerenciador de VMs.
- 🎯 **Gerenciador Multi-VMs Exclusivo:**
  - Cadastre e gerencie múltiplas versões do macOS (Tahoe, Sequoia, Sonoma, Ventura).
  - Execução estritamente exclusiva (apenas 1 VM ativa por vez).
- 🧠 **Hardware Advisor & Auto-Tuning:**
  - Detecção de topologia de CPU (P-Cores vs E-Cores e threads) e cálculo de memória RAM segura para o host.
- 🎮 **GPU Compatibility Checker & VFIO Helper:**
  - Verificação de compatibilidade Vulkan 1.2+ para Reims vGPU.
  - Mapeamento automático de grupos IOMMU e isolamento `vfio-pci`.
- 🔑 **GenSMBIOS Integrado:**
  - Geração automática de números de série válidos da Apple (Serial, MLB, SmUUID, ROM).
  - Injeção in-place no arquivo `config.plist` do OpenCore.
- 🔄 **Máquina de Estados de Instalação (Multi-Reboot Orchestrator):**
  - Rastreamento dos reboots intermediários da Apple via QMP socket sem desligar a VM.
  - Seleção automática da partição no OpenCore.
- 🛠 **Compilação Oculta com Barra de Progresso Animada:**
  - Builds de `reims-vgpu`, ROMs UEFI GOP e QEMU com saída limpa e barras animadas via **Rich**.

---

## 🏗 Arquitetura do Projeto

```text
vhackintosh/
├── bin/
│   └── vhackintosh             # Wrapper executável do CLI
├── cli/
│   └── main.py                 # Ponto de entrada do CLI e orquestrador
├── core/
│   ├── config.py               # Modelo de dados JSON e Lock exclusivo
│   ├── hardware.py             # Analisador de CPU e memória RAM
│   ├── gpu.py                  # Checador de GPU e helper IOMMU/VFIO
│   ├── smbios.py               # Gerador GenSMBIOS e injetor de plist
│   ├── builder.py              # Compilador silencioso com Rich Progress
│   ├── state_machine.py        # Máquina de estados de instalação e QMP
│   └── power.py                # Sincronização de energia Host <-> VM
├── ui/
│   ├── banner.py               # Logo ASCII e branding
│   ├── boot_delay.py           # Contagem de 5s com listener de tecla ESPAÇO
│   └── tui.py                  # Interface rica e interativa de terminal
├── themes/
│   └── grub/                   # Tema personalizado do GRUB2
├── appliance/
│   └── autologin-setup.sh      # Script de auto-login no console
└── docs/
    ├── soul.md                 # Memória viva do projeto e decisões
    └── tasks/                  # Quadro de tarefas e progresso
```

---

## 🚀 Como Usar

### 1. Iniciar o Gerenciador Interativo:
```bash
./bin/vhackintosh
```

### 2. Comandos Diretos da Linha de Comando:
```bash
# Listar VMs cadastradas
./bin/vhackintosh list

# Executar diagnóstico de hardware e GPUs
./bin/vhackintosh doctor

# Iniciar uma VM diretamente
./bin/vhackintosh start "macOS Sequoia"

# Atualizar e compilar componentes de aceleração gráfica
./bin/vhackintosh update
```

---

## 🌐 Recursos do Appliance (pt-BR)

- **Idioma**: a primeira tela do appliance escolhe entre **Português (Brasil)**
  e **English (US)**; a escolha vale para a instalação e para a TUI inteira e
  persiste nos reboots. Para trocar depois de instalado:
  ```bash
  ./bin/vhackintosh-lang pt   # ou: en
  ```
  Idioma inválido é recusado e a lista de suportados é exibida.
- **Hotkeys de notebook**: teclas XF86 de brilho e volume/mute funcionam na
  sessão do appliance (listener evdev próprio, independente de compositor).
  Em desktop sem brilho, as teclas são inofensivas.
- **Dispositivos USB na VM**: com a VM ligada, a opção `d` (Dispositivos USB)
  da tela da VM conecta/desconecta HDs externos, pen drives e câmeras por
  hotplug QMP (`usb-host`) — sem reiniciar a VM. "Anexar sempre" (★) persiste
  e o dispositivo é ligado sozinho no boot seguinte.
- **Áudio do convidado**: o PipeWire do host é o backend do QEMU (a sessão do
  appliance o sobe sozinha); o dispositivo no guest é `usb-audio` (macOS 26
  não tem mais Intel HDA). A prontidão avisa — sem impedir o uso — quando o
  host não tem serviço de áudio.
- **Virtualização aninhada**: a ISO traz `nested=1` (kvm_intel/kvm_amd) e a CPU
  da VM recebe `+vmx`/`+svm` quando o host confirma nested — Docker/UTM rodam
  dentro do macOS. Dentro do guest: `sysctl machdep.cpu.features` deve citar
  `VMX`. Sem nested no host, a prontidão explica a limitação sem bloquear.

## 🌐 Appliance Features (English)

- **Language**: the first screen picks **English (US)** or **Português
  (Brasil)**; it applies to installation and the whole TUI and survives
  reboots. To switch after install: `./bin/vhackintosh-lang en` (or `pt`).
  Invalid input is rejected with the supported list.
- **Laptop hotkeys**: XF86 brightness and volume/mute keys work appliance-wide
  (dedicated evdev listener, compositor-independent; harmless on desktops).
- **USB devices in the VM**: with the VM running, the VM screen's `d` option
  attaches/detaches external drives, pen drives and cameras via QMP
  hotplug (`usb-host`) — no VM restart. "Always attach" (★) persists and the
  device is plugged automatically on the next boot.
- **Guest audio**: the host PipeWire is the QEMU backend (the appliance session
  starts it automatically); the guest device is `usb-audio` (macOS 26 dropped
  Intel HDA). Readiness warns — without blocking — when the host has no audio
  service.
- **Nested virtualization**: the ISO ships `nested=1` (kvm_intel/kvm_amd) and
  the VM CPU gets `+vmx`/`+svm` when the host confirms nested — Docker/UTM run
  inside macOS. Inside the guest: `sysctl machdep.cpu.features` should list
  `VMX`. Without host nested support, readiness explains the limitation.

---

## 📖 Documentação Viva
Consulte [`docs/soul.md`](docs/soul.md) para detalhes de arquitetura, princípios de design e decisões do projeto.

---

## 📄 Licença
Distribuído sob a licença MIT / GPLv3.
