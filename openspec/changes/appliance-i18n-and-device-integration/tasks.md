## 1. Diagnóstico e correção do bug de boot (guest-power-controls)

- [ ] 1.1 Reproduzir o bug: VM de teste com macOS instalado, escolher a opção de
      controles de Reboot/Shutdown, iniciar e capturar argv+variáveis da VM e o
      estado do picker do OpenCore (as duas opções)
- [ ] 1.2 Isolar a variável que muda o boot (PERSISTENT_DIR / DISK_MASTER /
      OPENCORE_MASTER / modo do rail) e registrar o root cause no design
- [ ] 1.3 Corrigir: a escolha afeta só o mapeamento de energia; caminhos e
      argumentos de boot idênticos nas duas opções
- [ ] 1.4 Regressão manual: os 4 cenários de `specs/guest-power-controls` passam

## 2. i18n (language-selection)

- [ ] 2.1 Módulo `core/i18n.py` com catálogos `pt`/`en` e `t()`; campo
      `language` no config do appliance (default pt-BR)
- [ ] 2.2 Tela de idioma como primeira tela da instalação (antes de qualquer
      outra opção)
- [ ] 2.3 Extrair as strings de `ui/tui.py` e do fluxo de instalação para chaves
      (catálogo pt = status quo, sem regressão de texto)
- [ ] 2.4 Catálogo `en` completo para as mesmas chaves
- [ ] 2.5 Script `vhackintosh-lang pt|en` para trocar o idioma depois de
      instalado (valida entrada, persiste, vale na próxima TUI/reboot)
- [ ] 2.6 Varredura por literais residuais em `ui/` e `core/` (nenhum texto
      fixo fora do catálogo)
- [ ] 2.7 Regressão manual: cenários de `specs/language-selection` (boot limpo,
      TUI traduzida, persistência, troca por script, idioma inválido)

## 3. Passthrough de dispositivos (host-device-passthrough)

- [ ] 3.1 Dependências na ISO + regras udev para o usuário da sessão acessar
      dispositivos USB
- [ ] 3.2 `core/passthrough.py`: listar dispositivos USB do host, anexar/detach
      em runtime via QMP (`device_add`/`device_del`)
- [ ] 3.3 TUI: menu de dispositivos (listar/conectar/desconectar) com a VM viva
- [ ] 3.4 Câmera e adaptadores sem fio pelo mesmo caminho, com degradação
      explícita quando indisponível (mensagem na TUI/prontidão)
- [ ] 3.5 Regressão manual: HD externo plugado com VM ligada e antes do boot,
      remoção segura, host sem câmera/wi-fi inicia normal

## 4. Hotkeys de brilho e volume (laptop-hotkeys)

- [ ] 4.1 Listener evdev das teclas XF86 (brilho/volume/mute) na sessão do
      appliance, independente de compositor
- [ ] 4.2 Brilho via `brightnessctl`/sysfs com fallback silencioso sem hardware
- [ ] 4.3 Volume/silenciar via backend de áudio do host, silencioso sem áudio
- [ ] 4.4 Teste em notebook (funciona com TUI e com VM aberta) e em desktop sem
      controle de brilho (inofensivo)

## 5. Áudio do convidado (guest-audio)

- [ ] 5.1 Diagnosticar o som mudo: PipeWire/PulseAudio instalado **e rodando** na
      sessão do appliance; corrigir a ISO/sessão se for essa a causa
- [ ] 5.2 Alinhar o backend de áudio do QEMU ao pipeline do host e validar som
      no convidado (vídeo + alerta)
- [ ] 5.3 Se continuar mudo com host ok: avaliar troca do model do dispositivo
      de áudio do convidado (fase 2, com evidência)
- [ ] 5.4 Readiness: checagem de áudio do host (não-crítica, com instrução)
- [ ] 5.5 Regressão manual: cenários de `specs/guest-audio`

## 6. Virtualização aninhada (guest-virtualization)

- [ ] 6.1 `/etc/modprobe.d/` com `nested=1` (kvm_intel/kvm_amd) aplicado pelo
      appliance + verificação runtime de `/sys/module/kvm_*/parameters/nested`
- [ ] 6.2 Expor a flag de virtualização (vmx/svm) na CPU da VM no ponto único de
      launch (`core/vm_launch.py`)
- [ ] 6.3 Readiness distingue "sem KVM" de "sem nested", com aviso acionável
- [ ] 6.4 Verificação dentro do macOS: `sysctl machdep.cpu.features` indica VMX

## 7. Ponto único de launch (pré-requisito dos itens 3/5/6/7)

- [ ] 7.1 Criar `core/vm_launch.py`: config de launch (discos/OpenCore/OVMF,
      cpus/RAM da sugestão do host, `cpu_model=Haswell-noTSX`, flags, áudio,
      QMP por VM)
- [ ] 7.2 Integrar a decisão A/B pendente da migração do reims-vgpu
      (delegar ao `reims-ctl` vs portar o argv) — **bloqueada até a decisão**
- [ ] 7.3 Smoke: VM sobe pelo novo launch com OpenCore template novo
      (templates do commit `940f6a1`) e disco instalado é encontrado

## 8. Validação final

- [ ] 8.1 Checklist dos cenários dos 6 specs executado numa VM de teste real
- [ ] 8.2 Documentação: README do appliance (idioma, hotkeys, passthrough,
      áudio, nested) atualizada nos dois idiomas
