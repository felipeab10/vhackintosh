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
- **Backend Wayland Nativo (sway kiosk):** O ambiente gráfico do appliance é uma sessão Wayland mínima com o **sway** em modo kiosk. A janela do reims-vgpu (winit) usa superfície Wayland nativa (`VK_KHR_wayland_surface`), sem Xorg, sem `xinit`, sem Openbox e sem XWayland. O aviso de GPU proprietária NVIDIA é suprimido por `SWAY_UNSUPPORTED_GPU=true` — env var em vez da flag `--unsupported-gpu` porque flag desconhecida é erro fatal e env var desconhecida é simplesmente ignorada.
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
- **Reinicialização Unificada com `-no-reboot`:** O QEMU, por padrão, reinicia a máquina virtual internamente sem encerrar o processo host. Para forçar a sincronização de reboot, o vHackintosh passa obrigatoriamente a flag `-no-reboot` em VMs com sistema instalado. Ao selecionar "Reiniciar..." no macOS, o QEMU termina e emite `guest-reset`/`RESET`. O host Linux executa `systemctl reboot` e reinicia o computador físico.
- **Detecção de VM Instalada por Fase Explícita (`core/install_phase.py`):** Substituiu a heurística frágil de uso real do disco (`>= 4 GB`). O vHackintosh agora rastreia uma fase persistida por VM (`VMConfig.install_phase`: `pending` / `installing` / `installed`) e só desacopla a mídia `BaseSystem.img`, aplica `-no-reboot` e ativa o *Host Power Sync* quando a fase é `installed`.
- **Garantia Contra Reboot do Host Durante a Instalação:** A instalação do macOS é multi-estágio e grava vários GB já na primeira fase — a heurística de `4 GB` marcava o sistema como pronto cedo demais, fazendo um reboot intermediário da Apple encerrar o QEMU e **reiniciar o computador físico no meio da instalação**. Agora, enquanto a fase não for `installed`, os reboots do instalador são absorvidos internamente pelo QEMU e o host **nunca** reinicia/desliga.
- **Confirmação Explícita de Conclusão:** Ao encerrar a VM em fase de instalação, uma sonda somente-leitura do disco (`guestfish --ro` + `qemu-img info`) verifica a presença do container APFS e o uso real. Havendo evidência de instalação completa, o gerenciador **pergunta** ao usuário se deve marcar como concluída e ativar a sincronização de energia. A promoção automática é proibida por design: um falso positivo reativaria exatamente o bug original.
- **Alternância Manual no Gerenciador:** A opção `[i]` no painel da VM permite alternar entre `INSTALANDO` e `INSTALADO` a qualquer momento, com exibição da evidência detectada no disco e do efeito sobre o comportamento de energia.
- **Status Visível na TUI:** A tabela principal ganhou a coluna `Status` e o painel de detalhes exibe a fase, o número de reboots do instalador contabilizados via QMP (`QMPPowerMonitor.reset_count`) e se a sincronização de energia do host está ativa.
- **Paridade Total no Motor Rail (`boot-x86.sh`):** O harness rail do reims-vgpu nunca reiniciava o host (não possui chamadas a `systemctl`), o que significava que ele também **nunca** acompanhava o reboot do macOS após a instalação. Agora o TUI traduz a fase em `QEMU_REBOOT_ACTION`:
  - `reset` durante a instalação — o QEMU absorve os reboots da Apple e o host permanece ligado;
  - `exit` após instalado — equivale semanticamente ao `-no-reboot` do motor nativo: `-action reboot=shutdown` faz o reboot do guest encerrar o QEMU com evento QMP `guest-reset`, permitindo ao host acompanhar.
- **Descoberta Dinâmica do Socket QMP:** O rail cria o socket em diretório temporário e publica o caminho em `$RUN_DIR/qmp.path`. O `QMPPowerMonitor` passou a aceitar `path_file` e resolve o socket real em tempo de execução, unificando a sincronização de energia entre os motores nativo e rail via o helper `_apply_host_power_policy`.

### 2.15. Auto-Boot Direto do OpenCore, Resolução Nativa e Boot Gráfico Apple Limpo
- **Configuração Silenciosa do Bootloader:** `ShowPicker = False`, `Timeout = 0`, `PollAppleHotKeys = True`, `AllowSetDefault = True`.
- **Comportamento Apple Nativo:** O macOS inicia diretamente no volume padrão (`OSX`) sem telas intermediárias. Segurar ou pressionar a **barra de espaço** ou **Option/Alt** durante a inicialização abre o menu gráfico completo do OpenCore.
- **Resolução Máxima Nativa:** Injeção de `Resolution = Max` e `ForceResolution = True` no bloco `UEFI/Output` do `config.plist`, garantindo que o firmware OVMF GOP assuma a resolução nativa máxima do monitor (1080p/2K/4K) desde o primeiro instante de boot.
- **Modo Verbose (`-v`) MANTIDO Temporariamente para Diagnóstico:** A flag `-v` foi **reativada** nos `boot-args` de `core/smbios.py` (junto de `keepsyms=1`, `debug=0x10A` e `msgbuf=1048576`) para permitir visualizar texto de **kernel panic** durante a depuração da instalação do macOS. O boot gráfico limpo da Apple (§2.15 original) fica adiado até que o fluxo de instalação esteja estável — remover a flag apenas quando a instalação estiver confiável.
- **Injeção Dinâmica via Guestfish:** O vHackintosh altera essa configuração in-place dentro de `OpenCore.qcow2` através do menu de gerenciamento da VM (opção 3).

### 2.16. Pipeline de Distribuição Contínua por GitHub Releases, Auto-Updater & ISO Tags
- **GitHub Actions CI (`felipeab10/reims-vgpu`):** Workflow `.github/workflows/release.yml` compila na nuvem a ROM UEFI GOP (Rust) e o QEMU com backend Vulkan, empacotando em `reims-vgpu-linux-x86_64.tar.gz` com checksum SHA-256 em cada tag/release.
- **Auto-Updater Inteligente (`core/updater.py`):** O comando `vhackintosh update` consulta a API de Releases do GitHub, baixa e descompacta os binários pré-compilados em `/opt/reims-vgpu/` com barra de progresso visual no Rich e executa `git pull` no vHackintosh, atualizando o sistema inteiro em menos de 10 segundos sem compilação local.
- **Isolamento de Romfiles do QEMU (`-L` Flag):** Binários compilados no CI possuem prefixos de build remotos (`/home/runner/...`). O vHackintosh injeta flags `-L` apontando dinamicamente para o diretório local `vendor/qemu/pc-bios`, garantindo que ROMs essenciais (`kvmvapic.bin`, `vgabios-stdvga.bin`) sejam encontradas em qualquer ambiente.
- **Gerador de ISO com Parâmetro de Release (`appliance/build-iso.sh --tag <TAG>`):** O script aceita `--tag <TAG>` (ex: `v1.0.0` ou `latest`), baixando os binários pré-compilados do GitHub Releases e gerando imagens ISO prontas para pendrive USB em qualquer máquina host sem compilação local.

### 2.17. Arquitetura Gráfica Kiosk Wayland (sway) & Suporte a Touchpads (Dell G15)
- **Por que Wayland e não mais X11:** A janela do reims-vgpu era apresentada por uma sessão X11 (`xinit` + Openbox). Num compositor tiling (niri), o pedido de fullscreen vira uma dica EWMH que o compositor pode recusar, e a janela ficava *tiled* em vez de ocupar o monitor — o desktop do guest aparecia menor que a tela. Em Wayland nativo, `Fullscreen::Borderless` vira `xdg_toplevel.set_fullscreen()`, que o compositor honra por protocolo.
- **Independência de resolução:** O sway entrega ao cliente o output inteiro (uma janela só, tile = tela) e o reims-vgpu encaixa o framebuffer do guest por **aspect-fit** — ver `crates/reims-vgpu/src/backend/window/viewport.rs`, `aspect_fit`: *"the largest centered rectangle with src's aspect ratio that fits dst"*. O host **nunca recorta**: 1080p, 1440p e 4K exibem o desktop inteiro, sem configuração por monitor.
- **Sessão mínima (`/usr/local/bin/vhackintosh-waysession` + `/etc/vhackintosh/sway.conf`):** O sway sobe com `sway -c /etc/vhackintosh/sway.conf`; a config declara `exec /run/vhackintosh/sway-app.sh`, gerado em tempo de execução pela sessão com o comando exato da VM. A VM vira filha do sway (herdando `WAYLAND_DISPLAY` e `SWAYSOCK`) e, ao terminar, o script derruba o sway devolvendo o console à TUI. Sem barra, sem bordas, sem gaps.
- **Tapping do touchpad — o motivo de ser sway e não cage:** O libinput **não possui atributo de tapping** na sua tabela de quirks (verificado na string table do binário: `AttrEventCode`, `AttrInputProp`, `AttrFuzz`, `AttrPressureRange`, `AttrSizeHint`…, nenhum de tap). Tapping só existe via API do compositor, e o `cage` não lê arquivo de configuração algum — logo não tem como habilitá-lo. O sway declara `input "type:touchpad" { tap enabled; natural_scroll enabled; }`, reproduzindo o antigo `/etc/X11/xorg.conf.d/40-touchpad.conf`.
- **Captura Automática de Cursor:** `grab-on-hover=on` no display GTK do QEMU continua ativo para captura transparente de entrada no motor nativo.
- **Nota honesta sobre X11 residual:** `libxcb` e `xcb-util-*` continuam instalados como dependência transitiva do `wlroots` e do `gtk3` (via `qemu-desktop`). São bibliotecas cliente, não um servidor X: nenhum processo Xorg roda e o XWayland não é instalado.
- **`swaybg` é obrigatório, embora pareça opcional:** O sway **sempre** tenta executar `swaybg` — o default embutido de `swaybg_command` (ver `sway.5`), disparado mesmo sem diretiva `bg` na config. Como `swaybg` não é dependência do sway, ele está declarado explicitamente em `packages.x86_64`. Sem o pacote o sway registra `failed to execute 'swaybg'` e o fundo do output fica indefinido. Diagnóstico inicial equivocado: atribuí o erro à diretiva `bg` e removi-a, mas o teste com **config vazia** continuou disparando o spawn — o gatilho é o sway, não a config. São 41 KB.
- **Seat: `seatd.service` habilitado.** O wlroots exige um seat e o `libseat` deste build tem apenas os backends `logind`, `seatd` e `noop` — **não há `builtin`**. Habilitar o `seatd` remove a dependência de uma sessão logind completa, que é frágil sob autologin de root no `tty1`. Se o seat falhar, o sway não sobe.
- **`WLR_RENDERER_ALLOW_SOFTWARE=true`:** Sem esta variável o wlroots **aborta** quando o único renderizador disponível é software (`Software rendering detected, please use the WLR_RENDERER_ALLOW_SOFTWARE environment variable to proceed`). Cobre o caso de virtio-gpu em VM de teste; em hardware real a aceleração é usada normalmente.
- **Validação de config tem limite conhecido:** `sway -C -c <conf>` detecta **nomes** de comando inválidos (exit 1), mas **não valida valores de enum** — `tap banana` passa silenciosamente. Por isso os valores foram conferidos um a um contra o `sway-input(5)`/`sway(5)` do sway 1.12 extraído da própria ISO.
- **`FORCE_X11` removido também do launcher global:** O script `/usr/local/bin/vhackintosh` (invocado pelo `profile.d` no `tty1`) exportava `FORCE_X11=1`. Isso faria o harness `vm/boot-x86.sh` desfazer o `WAYLAND_DISPLAY` herdado, forçar `WINIT_UNIX_BACKEND=x11` e apontar `DISPLAY=:1` — endereço onde não há servidor X, pois Xorg e XWayland não são instalados. A variável sobreviveu à primeira varredura por não casar com os padrões de busca iniciais; a remoção dela é o que garante que a migração valha em runtime, e não só na configuração.

### 2.18. Detecção Robusta de Mídia Live USB & Menu Interativo do Instalador
- **Falha de Detecção Tradicional (`/run/archiso/bootmnt`):** Em inicializações UEFI modernas com GRUB e systemd, o ponto de montagem do pendrive pode variar, fazendo checagens rígidas de diretório falharem e omitirem o instalador do sistema operacional.
- **Falso Positivo em Sistemas Instalados (`vhackintosh-install`):** A presença do binário do instalador era tratada como sinal de Live USB, mas ele é copiado propositalmente para o sistema definitivo (atalho permanente `[I]`). Resultado: o menu de instalação reaparecia a cada boot do sistema já instalado no SSD.
- **Detector Canônico de Modo (`/usr/local/bin/vhackintosh-mode`):** Fonte única de verdade que imprime `live` ou `installed`, com precedência determinística:
  1. Marcador positivo `/etc/vhackintosh/installed` (gravado pelo instalador bare-metal);
  2. Tipo do sistema de arquivos raiz (`overlay`/`squashfs`/`erofs` ⇒ Live);
  3. Origem da raiz (`/dev/loop*` ⇒ Live; `/dev/*` ⇒ instalado);
  4. Fallbacks exclusivos do Archiso (`/run/archiso` e parâmetros `archisobasedir`/`archisosearchuuid`/`img_dev`/`img_loop`), ignorando a string genérica `archiso`.
- **Menu Inicial Condicional:** O menu do `tty1` agora se adapta ao contexto real de execução:
  - **Live USB:** menu completo com `[1] Instalar no disco`, `[2] Modo Live/Demonstração`, `[3] Wi-Fi` e `[0] Reiniciar`.
  - **Instalado + sem Internet:** menu enxuto com `[1] Configurar Wi-Fi (nmtui)`, `[2] Continuar sem Internet` e `[0] Reiniciar`.
  - **Instalado + com Internet:** segue direto para o gerenciador (zero atrito), sem menu intermediário.
- **Detecção Multifator de Mídia Live:** O sistema afere os caminhos `/run/archiso`, `/run/archiso/img_dev` e parâmetros exclusivos da linha de comando do kernel em `/proc/cmdline`.
- **Marcador de Instalação (`/etc/vhackintosh/installed`):** Evidência positiva gravada pelo `vhackintosh-install` logo após a cópia do sistema (após o `rsync --delete`), contendo data, disco-alvo e partições. É o critério de maior prioridade e resolve também instalações antigas com initramfs residual do Archiso.
- **Atalho Permanente no Gerenciador:** No TUI do `vhackintosh`, a opção `★ [I] - INSTALAR vHackintosh OS no SSD/Disco` permanece fixada e acessível sempre que o executável de instalação estiver presente, independentemente de a sessão ser Live ou instalada (`core/mode.py`).

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
- **2026-10-01:** Implementação do pipeline de CI no GitHub Actions (`felipeab10/reims-vgpu`) e comando `vhackintosh update` para atualização instantânea via releases oficiais pré-compiladas.
- **2026-10-01:** Resolução de dependências de ROM BIOS do QEMU através da injeção dinâmica de caminhos `-L`.
- **2026-10-02:** Correção da sincronização de reinicialização do host: detecção de disco com macOS instalado (`>= 4 GB`), desacoplamento automático da mídia de instalação e imposição da flag `-no-reboot` no QEMU para capturar `guest-reset` e acionar `systemctl reboot`.
- **2026-10-02:** Configuração do template base do OpenCore com auto-boot silencioso (`ShowPicker = False`, `Timeout = 0`, `PollAppleHotKeys = True`, `AllowSetDefault = True`), resolução máxima nativa (`Resolution = Max`) e remoção da flag `-v` (verbose) dos `boot-args` para exibição limpa da maçã gráfica da Apple.
- **2026-10-02:** Diagnóstico e resolução da tela pequena e perda de foco de entrada (teclado/touchpad) no Dell G15: integração do gerenciador de janelas Openbox Kiosk, script `vhackintosh-xsession`, driver `xf86-input-libinput` e configuração de touchpad com tapping.
- **2026-10-02:** Aperfeiçoamento do script de verificação `appliance/verify-iso.sh` para emulação fiel do hardware bare-metal em modo tela cheia nativa sem barras de menu e com display virtio.
- **2026-10-02:** Adição de suporte a tags de versão (`--tag <TAG>`) no script `appliance/build-iso.sh`, permitindo compilar a ISO oficial puxando binários do GitHub Releases em qualquer computador.
- **2026-10-02:** Resolução da falha de detecção de Live USB: substituição da checagem `/run/archiso/bootmnt` por detecção multifator (`vhackintosh-install`, `/run/archiso` e `/proc/cmdline`), criando menu interativo estável sem contadores regressivos apressados e fixando a opção `★ [I]` no menu principal.
- **2026-10-03:** Correção do reaparecimento indevido do menu de instalação em sistemas já gravados no SSD: o binário `vhackintosh-install` deixou de ser usado como sinal de Live USB (ele é copiado para o disco por design). Criado o detector canônico `vhackintosh-mode`, o marcador positivo `/etc/vhackintosh/installed` e o menu condicional no `tty1` (Live ⇒ instalar; Instalado sem Internet ⇒ Wi-Fi; Instalado com Internet ⇒ vai direto ao gerenciador).
- **2026-10-03:** Correção do reboot do computador físico durante a instalação do macOS: a heurística `uso do disco >= 4 GB` foi substituída pelo rastreamento explícito de fase (`VMConfig.install_phase` em `core/install_phase.py`). O *Host Power Sync* e a flag `-no-reboot` agora só são aplicados quando a fase é `installed`, e a conclusão exige confirmação do usuário a partir de sonda somente-leitura do disco (`guestfish --ro`). Adicionados status de instalação na TUI, contador de reboots via QMP e alternância manual `[i]`.
- **2026-10-03:** Reativação temporária da flag `-v` (verbose) nos `boot-args` do OpenCore, para permitir o diagnóstico de kernel panic durante a depuração da instalação do macOS.
- **2026-10-03:** Migração do ambiente gráfico do appliance de X11 para Wayland. Removidos `xorg-server`, `xorg-xinit`, `xorg-xrandr`, `xf86-input-libinput` e `openbox`, além da sessão `vhackintosh-xsession` e de `/etc/X11/xorg.conf.d/40-touchpad.conf`. Adicionados `sway`, `wayland`, `libinput` e `ttf-dejavu`. O `cage` foi descartado como compositor porque não lê arquivo de configuração e portanto não tem como habilitar tapping — e o libinput não expõe tapping como quirk. A TUI passou a envolver o QEMU com `vhackintosh-waysession` em TTY puro e deixou de exportar `FORCE_X11`.
- **2026-10-03:** Diagnóstico do corte de visualização ao selecionar 1920×1080 no macOS: o host nunca recorta (o `aspect_fit` do reims-vgpu é letterbox), mas sob compositor tiling o pedido de fullscreen em X11/XWayland era recusado e a janela ficava *tiled* em vez de ocupar o monitor. A adoção de Wayland nativo resolve a geometria de forma independente da resolução do monitor.
- **2026-10-03:** Correção de dois defeitos encontrados executando o `sway` extraído da própria ISO contra a config real: `swaybg` ausente (o sway sempre o spawna, mesmo sem `bg`) e `seatd.service` não habilitado (o `libseat` não tem backend `builtin`). Adicionados `swaybg` e `WLR_RENDERER_ALLOW_SOFTWARE=true`. A config foi validada com o parser real do sway 1.12 (0 erros), com cada valor conferido contra as man pages.
- **2026-10-03:** Correção no `appliance/verify-iso.sh`: `$1` era usado simultaneamente como caminho da ISO e como argumento posicional do QEMU, fazendo a imagem entrar duas vezes (uma como `-cdrom`, outra como disco); e `--window` virava `ISO_FILE`, abortando o script. Substituído por parsing com contrato explícito, `-h/--help` e repasse de argumentos extras. O vídeo de teste passou de `-vga virtio` para `-vga std`, espelhando o motor nativo do appliance.
- **2026-10-03:** Correção do menu de rede indevido em sistema instalado e já conectado (reportado em VM de teste com rede por cabo ativa). A checagem de conectividade rodava **antes** de o DHCP concluir a negociação e concluía "SEM INTERNET", exibindo o menu de configuração onde ele não era necessário. Adicionada a espera `vhack_wait_internet` baseada em `nm-online -t`, que retorna de imediato quando já há conexão ativa (caso feliz: ~0s; cenário do usuário com DHCP lento: ~2s; pior caso sem rede alguma: ~7s). O `ping` também foi corrigido para `8.8.8.8` e restrito a uma única sondagem ICMP, já que nunca é o primeiro critério.
- **2026-10-03:** Paridade de sincronização de energia no motor rail: o TUI passou a definir `QEMU_REBOOT_ACTION` conforme a fase (`reset` instalando / `exit` instalado) e a monitorar o socket QMP do rail via descoberta dinâmica (`$RUN_DIR/qmp.path`), com a lógica unificada no helper `_apply_host_power_policy`.
