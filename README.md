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

## 📖 Documentação Viva
Consulte [`docs/soul.md`](docs/soul.md) para detalhes de arquitetura, princípios de design e decisões do projeto.

---

## 📄 Licença
Distribuído sob a licença MIT / GPLv3.
