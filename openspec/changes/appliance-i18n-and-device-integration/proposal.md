## Why

O appliance vHackintosh está funcional para instalar macOS, mas acumula sete lacunas
que impedem o uso real no dia a dia: não há idioma inglês (usuários fora do BR não
conseguem usar a instalação), a escolha dos controles de reboot/shutdown quebra o
boot do disco já instalado (o OpenCore "perde" o macOS), dispositivos USB/áudio/
câmera/bluetooth do host não chegam ao convidado, as teclas de brilho/volume do
notebook não funcionam, e o convidado não expõe suporte a virtualização aninhada.
Ajustar isso agora evita que a próxima rodada de testes revalide problemas já
conhecidos e destrave o uso cotidiano da VM.

## What Changes

- **Seleção de idioma na primeira tela da instalação do Linux appliance**: escolha
  entre Inglês e Português do Brasil antes de qualquer outro passo; a escolha
  traduz toda a interface (instalação e TUI), persiste após o install, e um script
  de linha de comando permite trocar o idioma depois de instalado.
- **BUG — controles de Reboot/Shutdown quebram o boot**: qualquer uma das duas
  opções do pós-instalação faz o OpenCore não encontrar o disco com o macOS na
  próxima inicialização da VM; corrigir para que a escolha afete apenas o
  mapeamento de energia (host) e nunca o boot do disco instalado.
- **Passthrough USB do host**: HDs externos, pen drives e periféricos USB plugados
  no host devem aparecer dentro do macOS; os controladores USB do host podem ser
  direcionados à VM.
- **Teclas de notebook (brilho e volume)**: o Linux appliance trata as teclas
  XF86 de brilho e volume (aumentar/diminuir) para a tela do host — hoje não
  fazem nada.
- **Passthrough de Bluetooth, câmera e Wi-Fi quando possível**: expor ao convidado
  os adaptadores USB/PCI correspondentes, com degradação explícita e não-crash
  quando o host não os tiver ou o driver não permitir.
- **Áudio do convidado**: fazer o som funcionar na VM do macOS (foco no host:
  driver/backend de áudio ausente ou errado no appliance).
- **Virtualização aninhada no convidado**: a VM do macOS precisa expor VT-x/AMD-V
  ao guest (flag da CPU + `nested=1` no módulo KVM do host) para que o macOS
  rode suas próprias VMs.

## Capabilities

### New Capabilities

- `language-selection`: escolha e persistência de idioma (en-US / pt-BR) no
  appliance — tela inicial da instalação, tradução de toda a interface, troca
  pós-instalação via script.
- `guest-power-controls`: controles de reboot/shutdown do convidado — o prompt
  pós-instalação, o mapeamento para energia do host, e a garantia de que a
  escolha nunca impede o OpenCore de bootar o disco instalado.
- `host-device-passthrough`: passagem de dispositivos do host ao convidado —
  USB (armazenamento e periféricos) e, quando disponíveis, adaptadores sem fio e
  câmera.
- `laptop-hotkeys`: teclas de brilho e volume do notebook tratadas pelo appliance
  (aumentar/diminuir), funcionando na sessão gráfica onde a TUI roda.
- `guest-audio`: áudio funcional no convidado macOS (backend do host + dispositivo
  de áudio da VM).
- `guest-virtualization`: virtualização aninhada — VT-x/AMD-V exposto ao guest
  macOS.

### Modified Capabilities

*(nenhuma — o projeto não tem specs existentes; todas as capabilities acima são novas)*

## Impact

- **Interface/TUI**: `ui/tui.py` (todas as strings ~1300 linhas), `core/config.py`
  (persistência do idioma), novo módulo de i18n (dicionário en/pt-BR + lookup);
  primeira tela passa a ser a de idioma.
- **Instalação do appliance**: `appliance/archiso/` (profile.d, sessão inicial,
  teclado/sessão gráfica para as hotkeys), `appliance/archiso/packages.x86_64`
  (possíveis deps de áudio/brightnessctl).
- **Fluxo de energia pós-install**: `core/install_phase.py`, `core/power.py`,
  `ui/tui.py` (prompt de reboot/shutdown) — bug do disco do OpenCore.
- **Launch da VM/QEMU**: argv do QEMU (USB passthrough, `-device` de áudio, flag
  `vmx` da CPU, controles) — **depende da decisão pendente A/B** da integração
  com o reims-vgpu fork (delegar ao `reims-ctl` vs portar o argv); o design
  assume o ponto de extensão comum, mas o local exato do argv muda conforme a
  decisão.
- **Readiness**: `core/readiness.py` (checagem de `nested=1`, áudio do host,
  controladores USB).
