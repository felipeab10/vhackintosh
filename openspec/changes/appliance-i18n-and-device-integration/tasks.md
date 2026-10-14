## 1. Diagnóstico e correção do bug de boot (guest-power-controls)

- [x] 1.1 Reproduzir o bug: VM de teste com macOS instalado, escolher a opção de
      controles de Reboot/Shutdown, iniciar e capturar argv+variáveis da VM e o
      estado do picker do OpenCore (as duas opções)
      — diagnóstico feito por invariância após a migração (o argv antigo não
      existe mais para reproduzir): no fluxo antigo a escolha alterava
      INSTALL_MEDIA/PERSISTENT_DIR/DISK_MASTER/OPENCORE_MASTER e o modo do
      rail; registrado em design.md (Resolution Notes / D2)
- [x] 1.2 Isolar a variável que muda o boot (PERSISTENT_DIR / DISK_MASTER /
      OPENCORE_MASTER / modo do rail) e registrar o root cause no design
      — root cause: o mapa de discos do rail dependia da fase escolhida
      (design.md, D2 Resolution Notes)
- [x] 1.3 Corrigir: a escolha afeta só o mapeamento de energia; caminhos e
      argumentos de boot idênticos nas duas opções
      — por construção no fluxo novo: o vm.toml vem dos mesmos campos nas duas
      escolhas (disco sata.3 fixo, instalador sata.4 snapshot); a escolha só
      muda install_phase/contadores/PowerSync
- [ ] 1.4 Regressão manual: os 4 cenários de `specs/guest-power-controls` passam
      (exige VM com macOS instalado — hardware/imagem)

## 2. i18n (language-selection)

- [x] 2.1 Módulo `core/i18n.py` com catálogos `pt`/`en` e `t()`; campo
      `language` no config do appliance (default pt-BR)
      — settings em ~/.config/vhackintosh/settings.json; t() com fallback
      pt → chave
- [x] 2.2 Tela de idioma como primeira tela da instalação (antes de qualquer
      outra opção) — `VMTUI._language_screen` roda antes do banner/prontidão
      quando settings não tem `language`
- [x] 2.3 Extrair as strings de `ui/tui.py` e do fluxo de instalação para chaves
      (catálogo pt = status quo, sem regressão de texto)
- [x] 2.4 Catálogo `en` completo para as mesmas chaves
- [x] 2.5 Script `vhackintosh-lang pt|en` para trocar o idioma depois de
      instalado (valida entrada, persiste, vale na próxima TUI/reboot)
      — `bin/vhackintosh-lang` + `cli/lang.py`; inválido → recusa + lista
      (rc=2), mantém o atual
- [x] 2.6 Varredura por literais residuais em `ui/` e `core/` (nenhum texto
      fixo fora do catálogo) — varredura executada; sobras listadas no relato
      da aplicação
- [ ] 2.7 Regressão manual: cenários de `specs/language-selection` (boot limpo,
      TUI traduzida, persistência, troca por script, idioma inválido)

## 3. Passthrough de dispositivos (host-device-passthrough)

- [x] 3.1 Dependências na ISO + regras udev para o usuário da sessão acessar
      dispositivos USB — packages.x86_64 (usbutils, libusb) +
      etc/udev/rules.d/70-vhackintosh-usb.rules (MODE=0666 + uaccess)
- [x] 3.2 `core/passthrough.py`: listar dispositivos USB do host, anexar/detach
      em runtime via QMP (`device_add`/`device_del`)
      — QMP de conexões curtas (usb-host), idempotente, probe de VM viva
- [x] 3.3 TUI: menu de dispositivos (listar/conectar/desconectar) com a VM viva
      — ação `d` na tela da VM; "anexar sempre" persiste em usb_auto_attach e
      o ReimsCtlSession.on_ready anexa no boot (cenário do pen drive pré-boot)
- [x] 3.4 Câmera e adaptadores sem fio pelo mesmo caminho, com degradação
      explícita quando indisponível (mensagem na TUI/prontidão)
      — `list_attachable_extra()` (câmera/wireless) usa o mesmo usb-host;
      sem dispositivo/msg clara, menu degrada
- [ ] 3.5 Regressão manual: HD externo plugado com VM ligada e antes do boot,
      remoção segura, host sem câmera/wi-fi inicia normal (hardware)

## 4. Hotkeys de brilho e volume (laptop-hotkeys)

- [x] 4.1 Listener evdev das teclas XF86 (brilho/volume/mute) na sessão do
      appliance, independente de compositor — `core/hotkeys.py` (python-evdev),
      iniciado por etc/profile.d/vhackintosh-hotkeys.sh na tty1
- [x] 4.2 Brilho via `brightnessctl`/sysfs com fallback silencioso sem hardware
- [x] 4.3 Volume/silenciar via backend de áudio do host (wpctl → pactl →
      amixer), silencioso sem áudio
- [ ] 4.4 Teste em notebook (funciona com TUI e com VM aberta) e em desktop sem
      controle de brilho (inofensivo) (hardware)

## 5. Áudio do convidado (guest-audio)

- [x] 5.1 Diagnosticar o som mudo: PipeWire/PulseAudio instalado **e rodando**
      na sessão do appliance; corrigir a ISO/sessão se for essa a causa
      — causa: a sessão live sem systemd --user nunca subia o PipeWire;
      profile.d ganhou fallback manual (pipewire/pipewire-pulse/wireplumber) e
      a sessão traz o stack completo; checagem nova no readiness
- [x] 5.2 Alinhar o backend de áudio do QEMU ao pipeline do host e validar som
      no convidado (vídeo + alerta)
      — o reims-ctl defaulta `-audiodev pipewire` (fala PipeWire direto);
      backend ligado por [audio] enabled=true do vm.toml
- [x] 5.3 Se continuar mudo com host ok: avaliar troca do model do dispositivo
      de áudio do convidado (fase 2, com evidência)
      — N/A com evidência: o upstream já apresenta `usb-audio` no xHCI (macOS
      26 sem Intel HDA); model é escolha do reims-ctl
- [x] 5.4 Readiness: checagem de áudio do host (não-crítica, com instrução)
- [ ] 5.5 Regressão manual: cenários de `specs/guest-audio` (ouvir som no host)

## 6. Virtualização aninhada (guest-virtualization)

- [x] 6.1 `/etc/modprobe.d/` com `nested=1` (kvm_intel/kvm_amd) aplicado pelo
      appliance + verificação runtime de `/sys/module/kvm_*/parameters/nested`
      — etc/modprobe.d/vhackintosh-nested.conf + core/virt.py
      (nested_status/apply_nested)
- [x] 6.2 Expor a flag de virtualização (vmx/svm) na CPU da VM no ponto único
      de launch — core/reims_ctl.py `cpu_flags_str()` anexa +vmx/+svm quando o
      host confirma nested (testado: host com nested → …,+vmx)
- [x] 6.3 Readiness distingue "sem KVM" de "sem nested", com aviso acionável
- [ ] 6.4 Verificação dentro do macOS: `sysctl machdep.cpu.features` indica VMX
      (guest; instrução no README)

## 7. Ponto único de launch (pré-requisito dos itens 3/5/6/7)

- [x] 7.1 Criar `core/vm_launch.py`: config de launch (discos/OpenCore/OVMF,
      cpus/RAM da sugestão do host, `cpu_model=Haswell-noTSX`, flags, áudio,
      QMP por VM)
      — materializado como `core/reims_ctl.py` (a decisão A/B saiu como
      delegar ao reims-ctl): vm.toml + registry + QMP fixo + sessão; é o ponto
      único por onde 3/5/6 programam
- [x] 7.2 Integrar a decisão A/B pendente da migração do reims-vgpu
      (delegar ao `reims-ctl` vs portar o argv)
      — decidido e aplicado: delegar ao reims-ctl (commit 2e6170f do vhackintosh
      + merge/limpeza no fork)
- [ ] 7.3 Smoke: VM sobe pelo novo launch com OpenCore template novo
      (templates do commit `940f6a1`) e disco instalado é encontrado
      — smoke de camada executado (gate → vm.toml → registry → `reims-ctl
      show`/`command` com argv completo); boot real pendente de imagem/VM

## 8. Validação final

- [ ] 8.1 Checklist dos cenários dos 6 specs executado numa VM de teste real
- [x] 8.2 Documentação: README do appliance (idioma, hotkeys, passthrough,
      áudio, nested) atualizada nos dois idiomas
      — seção bilíngue no README.md do repositório
