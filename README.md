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
- **Testar a ISO em VM (sem trocar o SSD do notebook)**: o `verify-iso.sh` sobe
  a ISO num QEMU local. Para pular a checagem de Vulkan 1.2+ — que exige GPU
  física e por isso nunca passa num QEMU com vídeo emulado — use
  `VHACK_SKIP_VULKAN_CHECK=1`:
  ```bash
  sudo VHACK_SKIP_VULKAN_CHECK=1 ./appliance/verify-iso.sh \
      --headless --build-test appliance/out/vhackintosh-os-*.iso
  ```
  **É temporário e não verifica nada:** a prontidão passa a dizer
  explicitamente que a checagem foi *pulada*, para a tela nunca afirmar
  "Suportado & Pronto" sem ter olhado uma GPU. Sem a variável, o padrão
  continua estrito. `--build-test` roda o build do `reims-vgpu` dentro do
  convidado e imprime `VHACK_BUILD_TEST_RESULT=PASS|FAIL`.
- **Build offline do `reims-vgpu`**: a ISO leva os caches do Cargo
  (`git/db` do `metal2vulkan` + `registry` do crates.io) para o primeiro boot
  compilar sem rede. `REIMS_CARGO_OFFLINE=1` força offline; `=0` libera a rede
  (útil em desenvolvimento). Sem cache completo o build usa rede e avisa, em
  vez de falhar com `you are in the offline mode`.
- **reims-vgpu pré-compilado (padrão)**: compilar o QEMU do fork dentro da VM
  levava horas (e a ISO é live, sem garantia de rede), então o padrão voltou a
  ser **empacotar o binário já compilado**, vindo de uma release do GitHub. A
  ISO nasce pronta para lançar a VM; nada é compilado no primeiro boot.
  ```bash
  # padrão: baixa a última release do reims-vgpu e empacota na ISO
  sudo ./appliance/build-iso.sh -o appliance/out --release latest

  # fixar uma tag em vez da última
  sudo ./appliance/build-iso.sh -o appliance/out --release v1.0.1
  ```
  O modo fontes continua existindo e é **opt-in**:
  ```bash
  sudo ./appliance/build-iso.sh -o appliance/out --from-source
  ```
  Em ambos os casos a ISO leva o `vendor/qemu/pc-bios` (ROMs: pxe, vgabios,
  edk2) — sem elas o QEMU pré-compilado não sobe.
- **Atualizar o reims-vgpu de dentro do appliance**: com a árvore pré-compilada,
  o lançamento da VM oferece procurar uma release nova e trocá-la sem
  recompilar. Fora da TUI, o mesmo caminho pelo script:
  ```bash
  sudo ./appliance/build-iso.sh --update-sources --release latest   # última
  sudo ./appliance/build-iso.sh --update-sources --release v1.1.0   # fixa
  ```
  Ele baixa o `reims-vgpu-linux-x86_64.tar.gz`, extrai sobre
  `/opt/reims-vgpu` e grava `.release` com a tag instalada. Nada é apagado: o
  que já existe é preservado, então o modo fontes continua funcionando depois.
  O arquivo só é trocado **depois** de validado — release sem o binário do QEMU
  é recusada com a árvore intacta.
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

## 📦 Publicando uma release do reims-vgpu

O binário que a ISO consome é publicado por um script que vive no **fork**
[`felipeab10/reims-vgpu`](https://github.com/felipeab10/reims-vgpu), em
`scripts/release.sh`. Ele calcula a próxima versão, empacota o build da sua
máquina e abre a release no GitHub — sem passar por CI (compilar o QEMU do fork
no GitHub Actions levava ~2 h; localmente leva minutos).

```bash
cd ~/reims-vgpu

./scripts/release.sh --dry-run    # mostra a versão e o que seria empacotado
./scripts/release.sh              # empacota e publica (pede confirmação)
```

**A versão é inferida dos commits** desde a última tag `v*`, seguindo os
prefixos usados no fork (gitmoji): `:new:` → *minor*, `:bug:` → *patch*,
`BREAKING CHANGE`/`!` → *major*. A versão sugerida é mostrada na tela antes de
qualquer coisa ser criada, e você confirma. Para forçar:

```bash
./scripts/release.sh --minor        # força minor
./scripts/release.sh --major        # força major
./scripts/release.sh --patch        # força patch
./scripts/release.sh --tag v2.0.0   # usa esta tag exata (recusa se já existir)
./scripts/release.sh --yes          # sem perguntas (para script/automação)
./scripts/release.sh --skip-push    # empacota sem publicar
```

**Antes de publicar, os artefatos precisam existir na árvore** — o script
aborta com instruções se faltarem, em vez de publicar uma release quebrada.
O construtor deste checkout é o `install-ctl.sh`:

```bash
./scripts/install-ctl.sh --yes   # compila; deixa o QEMU em ~/.cache/reims-ctl/
```

Depois copie os artefatos para os caminhos que o `release.sh` empacota — o QEMU
com o binário e as ROMs como **irmãos**, que é a única disposição em que ele
acha o `romfile` em runtime:

```
vendor/qemu/build/bin/qemu-system-x86_64
vendor/qemu/build/share/qemu/ReimsVgpu.rom   ← obrigatória
vendor/qemu/build/share/qemu/*               ← pc-bios (bios-256k.bin e cia.)
vendor/qemu/pc-bios/
```

O `romfile "ReimsVgpu.rom"` é resolvido relativo ao **próprio executável**: o
QEMU sobe um nível a partir de `bin/` e procura em `<dir do binário>/../share/qemu/`.
Medido com o binário real: com a ROM nesse lugar ele carrega; um nível mais fundo
(ele responde `could not load PC BIOS 'bios-256k.bin'`) ou ausente
(`failed to find romfile`), não.

> **Atenção:** o commit `98fe2a3b` (no superprojeto, não no submódulo `vendor/qemu`)
> substituiu a árvore do fork (`:new: Replace the tree with libreims_vgpu and its
> C API`), então `crates/reims-vgpu-efi/` e `scripts/qemu-build/` **não existem
> mais** no `master`. O `install-ctl.sh` é o construtor que funciona.
>
> O workflow `.github/workflows/release.yml` foi renomeado para
> `release.yml.disabled` justamente por isso: ele referenciava os caminhos
> removidos, e — pior — ficaria **competindo com o `release.sh`** pela mesma tag,
> já que o script empurra a tag `v*` antes de criar o Release. O publicador é um
> só: o `release.sh`.

O pacote publicado é o `reims-vgpu-linux-x86_64.tar.gz` (+ `.sha256`), com o
layout de prefixo acima mais `vm/ovmf/` e `vm/boot-x86.sh` quando presentes na
árvore — o mesmo que o appliance espera. Se a publicação falhar, a tag local é
removida e o tarball apagado, para não sobrar sujeira no próximo `git status`.

> **As duas ROMs são de gerações diferentes — e nunca podem ser trocadas entre
> si.** Não é "a certa contra a errada": é `reims-vgpu-gop.rom` (26112 bytes,
> sha256 `02cf046f…`) para o device `reims-vgpu-pci` das v1.0.0/v1.0.1, e
> `ReimsVgpu.rom` (5632 bytes, sha256 `2165aa4c…`) para o `apple-gfx-pci` atual.
> O QEMU aceita as duas como `romfile` sem reclamar, então trocar uma pela outra
> publica uma release que sobe a VM com o firmware errado e **nenhum erro na
> tela** — o sintoma é tela preta, longe da causa.
>
> Qual delas serve **não depende de uma convenção fixa: depende do binário que
> veio no pacote**. O `romfile` é uma string que o compilador deixa na tabela de
> propriedades do device (`pci->romfile = "ReimsVgpu.rom"` em
> `hw/display/apple-gfx-pci.c`), então a verificação lê os bytes do binário para
> decidir. Medido: o binário atual (QEMU 11.1.50) traz a string; o da v1.0.1
> (11.0.50) traz zero ocorrências e tem outro device (`reims-vgpu-pci`, que só
> recebe ROM pelo `romfile=` passado à mão no `vm/boot-x86.sh`).
>
> A **versão do QEMU não serve para essa decisão** — ela fala da base upstream,
> não da geração do device. O rename no fork trouxe o `romfile` novo sobre uma
> base `VERSION=11.0.50`, e o 11.1.0 só entrou depois; usar a versão rejeitaria
> builds legítimos dessa faixa. A verificação pergunta nos **dois** sentidos ("o
> binário carrega a ROM que veio?"), porque a inversão também é silenciosa:
> `ReimsVgpu.rom` numa árvore de binário antigo é tão quebrada quanto o
> contrário.
>
> O `release.sh` recusa publicar sem a ROM correta e explica se só achou a antiga.

> O binário é **x86-64 genérico** (sem `-march=native`) e carrega o Vulkan em
> runtime via `dlopen` (a `libvulkan` não aparece no `ldd`), então não é preso à
> máquina que o compilou.
>
> **O piso real de glibc é o do host que compilou.** Medido nos binários deste
> host: **GLIBC_2.43** (host com glibc 2.44). Para conferir antes de publicar:
>
> ```bash
> objdump -T vendor/qemu/build/bin/qemu-system-x86_64 \
>   | grep -o 'GLIBC_[0-9.]*' | sort -uV | tail -1
> ```
>
> Se o valor for alto demais para as distros que você quer atender, compile num
> container com glibc mais antiga — não há como baixar esse piso depois.

---

## 📖 Documentação Viva
Consulte [`docs/soul.md`](docs/soul.md) para detalhes de arquitetura, princípios de design e decisões do projeto.

---

## 📄 Licença
Distribuído sob a licença MIT / GPLv3.
