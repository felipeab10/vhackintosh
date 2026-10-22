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

### Iniciar o gerenciador

No appliance inicializado (ou no Linux de desenvolvimento com os requisitos
abaixo):

```bash
./bin/vhackintosh
```

Atalhos de linha de comando:

```bash
./bin/vhackintosh list
./bin/vhackintosh doctor
./bin/vhackintosh start "macOS Sequoia"
./bin/vhackintosh update
```

`update` atualiza as fontes locais do vHackintosh. A atualização do motor
`reims-vgpu` é uma ação separada no menu do gerenciador.

### Clonar o projeto e executar no Linux

O appliance é construído para Linux x86-64. Para clonar o repositório:

```bash
git clone https://github.com/felipeab10/vhackintosh.git ~/vhackintosh
cd ~/vhackintosh
```

Em uma distro Arch (incluindo CachyOS), instale as dependências para executar a
TUI e provisionar VMs:

```bash
sudo pacman -S --needed python python-rich python-evdev \
  qemu-system-x86 qemu-img libguestfs edk2-ovmf
```

Confirme que a virtualização está habilitada na BIOS/UEFI e que seu usuário tem
acesso a `/dev/kvm`. Depois, rode os comandos acima sem `sudo`, para manter a
configuração das VMs no seu próprio diretório `~/.config`.

> Executar o gerenciador a partir do clone não instala o sistema operacional e
> não compila o motor gráfico. Para iniciar uma VM macOS, instale também o
> `reims-ctl` e o QEMU do fork `felipeab10/reims-vgpu`, conforme a seção
> [Preparar o motor reims-vgpu](#preparar-o-motor-reims-vgpu).

### Requisitos para executar VMs macOS

| Requisito | Mínimo/observação |
| --- | --- |
| Arquitetura | Linux x86-64; para desempenho, executar diretamente no hardware. |
| CPU e firmware | Intel VT-x ou AMD-V habilitado na BIOS/UEFI e `/dev/kvm` acessível. |
| Memória | 8 GB de RAM é o mínimo detectado; 16 GB ou mais é recomendado para alocar 8–12 GB à VM. |
| GPU para Reims vGPU | GPU física com Vulkan 1.2+ e driver Vulkan funcional. GPUs virtuais/llvmpipe não contam. |
| Armazenamento | O disco virtual começa em 64 GB e cresce conforme o uso; 70 GB ou mais é recomendado para macOS e atualizações. Reserve pelo menos 50 GB livres para o disco e o instalador. |
| Rede | Necessária para baixar o instalador oficial do macOS e para baixar releases durante o build da ISO. |
| Virtualização aninhada | Opcional; necessária apenas se quiser executar Docker/UTM ou outras VMs dentro do macOS. |

O verificador identifica como referências de GPU Intel Gen9+, NVIDIA GTX 900+
ou AMD GCN 4+; o que vale é o Vulkan enumerar uma GPU física compatível. O
modo de teste VMware SVGA abaixo permite testar a tela e o fluxo da VM aninhada,
mas não substitui esse requisito para usar Reims vGPU.

### Preparar o motor reims-vgpu

O vHackintosh usa o motor e o `reims-ctl` do fork em um repositório separado.
Clone-o com os submódulos e compile/instale os componentes uma vez:

```bash
git clone --recurse-submodules https://github.com/felipeab10/reims-vgpu.git ~/reims-vgpu
cd ~/reims-vgpu
./scripts/install-ctl.sh --yes
```

Esse passo exige as dependências de compilação documentadas no repositório do
`reims-vgpu`. O script instala o `reims-ctl`, o QEMU com Reims vGPU e o
`reims-util`. O último também é necessário no host que constrói a ISO, pois o
builder o inclui no appliance.

Para atualizar o `reims-util` oficial pelo script do vHackintosh, rode antes
do build da ISO:

```bash
./appliance/build-iso.sh --update-reims-util
```

O script resolve o commit atual da branch `master`, compila em uma pasta
temporária, valida o binário e só então substitui a instalação local. Para
fixar outra branch ou tag, use `--reims-util-ref <REF>`. O build da ISO usa
esse binário para gerar o OpenCore com Lilu e os kexts oficiais de aceleração.
Dentro do appliance, `vhackintosh-update-reims-util` atualiza o utilitário e
regenera a EFI padrão usada por novas VMs.

### Construir a ISO

A construção da ISO usa o **Archiso** e deve ser feita em uma distro Arch. O
builder precisa de `mkarchiso`, `rsync`, `git`, `curl` (ou `wget`),
`xorriso`, `unsquashfs`, `qemu-img`, `guestfish`, acesso à rede para baixar a
release selecionada e do `reims-util` instalado acima. Ele gera a imagem EFI
com esse utilitário durante o build, sem baixar kexts no primeiro boot da VM.
O OVMF continua vindo de `templates/OVMF_CODE_4M.fd`, preservando a logo
vHackintosh embutida no firmware.

```bash
cd ~/vhackintosh
sudo pacman -S --needed archiso rsync git curl libisoburn squashfs-tools \
  qemu-img libguestfs
ISO_OUT="$PWD/appliance/out-$(date +%Y%m%d-%H%M%S)"
sudo env OUTPUT_DIR="$ISO_OUT" ./appliance/build-iso.sh --release latest
```

O builder usa por padrão a última release pré-compilada do `reims-vgpu`, monta
e valida a ISO e grava um checksum `.sha256` junto dela. O diretório de saída
precisa ser novo a cada build. Para fixar uma release, troque `latest` pela tag,
por exemplo `--release v1.1.0`.

Para empacotar as fontes locais do fork e fazer a compilação no primeiro boot do
appliance, use `--from-source` e indique o checkout:

```bash
ISO_OUT="$PWD/appliance/out-fontes-$(date +%Y%m%d-%H%M%S)"
sudo env OUTPUT_DIR="$ISO_OUT" ./appliance/build-iso.sh \
  --from-source --source "$HOME/reims-vgpu"
```

### Testar a ISO sem instalar no host

Instale QEMU e OVMF no host que vai abrir a ISO e inicie o verificador:

```bash
sudo pacman -S --needed qemu-desktop qemu-system-x86 qemu-img edk2-ovmf
bash appliance/verify-iso.sh --vmware-svga
```

Sem um caminho de ISO, `verify-iso.sh` escolhe a mais recente em
`appliance/out/` ou `appliance/out-*/`. Também é possível informar uma ISO
específica: `bash appliance/verify-iso.sh /caminho/vhackintosh-os.iso
--vmware-svga`.

`--vmware-svga` é um modo de teste interativo para rodar o appliance dentro do
QEMU sem uma GPU Vulkan física. O script passa os marcadores
`vhack_skip_vulkan=true` e `vhack_graphics=vmware-svga` por SMBIOS Type 11 e
mantém o boot normal pela ISO/OVMF/GRUB. O menu de boot do firmware fica
habilitado: pressione `Esc` para escolher entre a ISO e o disco virtual. O
primeiro marcador pula apenas a barreira de verificação Vulkan; o segundo
configura as VMs macOS criadas nessa sessão para usar VMware SVGA, cuja janela
QEMU pode ser exibida dentro do appliance. Somente nessa sessão de teste o
wlroots permite renderização por software (`WLR_RENDERER_ALLOW_SOFTWARE=1`),
necessária para a tela emulada do QEMU externo. Sem `--vmware-svga`, esse
fallback não é habilitado e o fluxo padrão continua usando Reims vGPU com
Vulkan físico. Esse modo permite testar criação da VM, OpenCore e o fluxo do
instalador, mas **não testa aceleração gráfica Reims vGPU**. O valor legado
`vhack_skip_vulkan=1` continua aceito.

Depois de alterar o código do appliance, gere uma ISO nova antes de testá-la:
o `verify-iso.sh` envia os marcadores de teste via SMBIOS, mas o código Python
executado vem de dentro da ISO escolhida.

Para iniciar o appliance no QEMU sem trocar o backend gráfico da VM macOS,
use `--window` ou não passe `--vmware-svga`. Sem a flag, o comportamento normal
usa Reims vGPU e exige Vulkan 1.2+ físico.

O parâmetro `vhack_skip_vulkan=true` também pode ser acrescentado manualmente à
linha do kernel no GRUB para um boot de teste. No uso em um Linux já iniciado,
o equivalente é `VHACK_SKIP_VULKAN_CHECK=true`; esse bypass é temporário e não
torna a GPU compatível. Não use essas opções para avaliar suporte Vulkan real.

`--build-test` é outro teste, independente e headless: inicializa a ISO e
verifica se o build do `reims-vgpu` termina, imprimindo
`VHACK_BUILD_TEST_RESULT=PASS|FAIL`. Ele não testa a criação de uma VM macOS.

### Criar uma VM macOS

1. Inicialize o appliance (pela ISO em modo de teste ou depois de instalá-lo no
   computador) e aguarde a tela do gerenciador.
2. Escolha **2 – Criar Nova VM macOS**.
3. Informe o nome, escolha Tahoe (experimental), Sequoia (recomendada) ou
   Ventura, e confirme CPU, memória e tamanho do disco. Os valores sugeridos
   são calculados para o hardware detectado; o disco virtual padrão é 64 GB e
   tem alocação dinâmica.
4. Se houver mais de uma GPU, escolha a desejada. No modo `--vmware-svga` a
   seleção física de GPU não é usada para a VM de teste.
5. Baixe o instalador oficial quando solicitado. O assistente cria os discos,
   prepara o OpenCore e os identificadores SMBIOS e pergunta se deve iniciar a
   instalação.
6. Durante a instalação, deixe a janela aberta. O appliance acompanha os
   reboots programados do macOS e relança a VM até a fase de instalação terminar.

Com uma VM já cadastrada, use **1 – Gerenciar VMs** para iniciar, retomar a
janela, parar ou editar recursos. Na CLI, `./bin/vhackintosh start "Nome da VM"`
inicia uma VM cadastrada pelo nome.

---

## 🌐 Recursos do Appliance (pt-BR)

- **Idioma**: no ISO Live, a escolha aparece antes do menu de instalação em
  disco; no primeiro início da TUI também aparece caso ainda não tenha sido
  escolhida. Selecione **Português (Brasil)** ou **English (US)**. A escolha é
  copiada para o sistema instalado e persiste nos reboots. Para trocar depois:
  ```bash
  ./bin/vhackintosh-lang pt   # ou: en
  ```
  Idioma inválido é recusado e a lista de suportados é exibida.
- **Build do `reims-vgpu`**: o padrão da ISO é empacotar uma release
  pré-compilada, sem compilação no primeiro boot. No modo `--from-source`, a
  árvore e os caches do Cargo podem permitir build offline; se faltar cache,
  o build usa a rede e informa o que está ausente. `REIMS_CARGO_OFFLINE=1`
  força offline; `=0` libera a rede.
- **reims-vgpu pré-compilado (padrão)**: compilar o QEMU do fork dentro da VM
  levava horas (e a ISO é live, sem garantia de rede), então o padrão voltou a
  ser **empacotar o binário já compilado**, vindo de uma release do GitHub. A
  ISO nasce pronta para lançar a VM; nada é compilado no primeiro boot.
  A forma recomendada de gerar a ISO está em
  [Construir a ISO](#construir-a-iso); o parâmetro `--release` escolhe a tag.
  O modo fontes continua existindo e é **opt-in**: use
  `--from-source --source "$HOME/reims-vgpu"` e um diretório de saída novo.
  Em ambos os casos a ISO leva o `vendor/qemu/pc-bios` (ROMs: pxe, vgabios,
  edk2) — sem elas o QEMU pré-compilado não sobe.
- **Atualizar o reims-vgpu de dentro do appliance**: com a árvore pré-compilada,
  o lançamento da VM oferece procurar uma release nova e trocá-la sem
  recompilar. Fora da TUI, o mesmo caminho pelo script:
  ```bash
  ./appliance/build-iso.sh --update-sources --source "$HOME/reims-vgpu" \
      --release latest
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

- **Language**: on the Live ISO, the choice appears before the disk installer
  menu; the TUI also asks on first start if no choice exists yet. Pick
  **English (US)** or **Português (Brasil)**. The choice is copied to the
  installed system and survives reboots. To switch later:
  `./bin/vhackintosh-lang en` (or `pt`).
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
