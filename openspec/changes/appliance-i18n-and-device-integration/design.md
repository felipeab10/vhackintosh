## Context

A TUI (`ui/tui.py`, ~1300 linhas) é toda escrita em PT-BR hardcoded, sem camada
de idioma; a instalação começa direto nas opções de VM. O pós-instalação oferece
um prompt de controles de Reboot/Shutdown cujas duas opções, na prática, fazem o
OpenCore não encontrar o disco instalado (bug a ser diagnosticado — ver
`specs/guest-power-controls`). O argv do QEMU é montado hoje pelo `boot-x86.sh`
do reims-vgpu **antigo** embutido na ISO (`appliance/archiso/airootfs/opt/reims-vgpu`),
com device `reims-vgpu-pci` — a árvore nova do fork usa `apple-gfx-pci` e o
`reims-ctl`; a migração de launch está **pendente de decisão A/B** (delegar ao
`reims-ctl` vs portar o argv) e este change não a resolve, mas todo item que toca
o argv precisa de um ponto de extensão que sirva para os dois lados.

Referências: proposta em `proposal.md`; contratos em `specs/*/spec.md`.

## Goals / Non-Goals

**Goals:**
- i18n com dois idiomas (en-US, pt-BR), tela de idioma primeiro, persistente,
  trocável por script
- Corrigir o bug de boot dos controles de Reboot/Shutdown (diagnóstico + correção)
- Passthrough USB/câmera/sem-fio, hotkeys de brilho/volume, áudio e nested via
  mecanismos que funcionem com **qualquer** das duas arquiteturas de launch (A/B)
- Checagens de prontidão novas correspondentes (áudio, nested)

**Non-Goals:**
- A própria migração de launch (A/B) — decisão separada, fora deste change
- Idiomas além de en-US/pt-BR
- GPU passthrough, PCI passthrough completo
- Garantir que todo host suporte nested (só detectar/explicar quando não suporta)

## Decisions

**D1 — i18n por dicionário central (`core/i18n.py`), uma chave por string.**
Catálogos `en`/`pt` em Python (dicts), função `t("chave", **fmt)`; idioma guardado
no config do appliance (`~/.config/vhackintosh/…`, campo `language`). *Alternativas
rejeitadas*: gettext/arquivos .po (peso e tooling desnecessário p/ 2 idiomas);
strings inline com `if lang` (inviável em 1300 linhas). A tela de idioma é o
primeiro passo do **mesmo** TUI/processo que a instalação — uma fonte só de
verdade para instalação e pós-instalação. Script `vhackintosh-lang` (pt/en) lê e
grava o campo; idioma inválido → erro + lista (spec).

**D2 — Bug do Reboot/Shutdown: diagnosticar antes de decidir a correção.**
Suspeita principal: a escolha altera os caminhos/variáveis de boot
(`PERSISTENT_DIR`, `DISK_MASTER`, `OPENCORE_MASTER`, modo do rail) e faz a VM
subir com outro OpenCore/disco. Plano: reproduzir as duas opções, capturar o argv
e o picker do OpenCore em cada caso, isolar a variável que muda. Correção
dirigida: a escolha passa a afetar **apenas** o mapeamento de energia
(`core/install_phase.py`/`core/power.py`), nunca argumentos de boot — com
regressão nos cenários do spec. Se o root cause for outra coisa, o design da
correção volta a ser revisado antes da implementação.

**D3 — Passthrough por hotplug via QMP (`device_add`/`device_del`), não no argv.**
Um módulo `core/passthrough.py` lista dispositivos USB do host (libusb/`lsusb`)
e os conecta/desconecta com a VM viva pelo monitor QMP (que já existe nos dois
lados A/B). *Por que não no argv*: hotplug dispensa relaunch e é independente de
quem monta o argv. Câmera/sem-fio USB usam o mesmo caminho; adaptador Wi-Fi
PCI/USB-BT só quando o driver do host permitir libusb (spec exige degradação
clara). Permissão: regras udev do appliance dão acesso ao usuário da sessão.

**D4 — Hotkeys por leitura evdev própria, não por keybindings do compositor.**
Um listener em `core/` lê `/dev/input` por teclas `XF86MonBrightnessUp/Down`,
`XF86AudioRaiseVolume/LowerVolume/Mute` e aplica via `brightnessctl` (ou sysfs)
e comando de volume do áudio do host. *Alternativa rejeitada*: keybindings do
compositor (o identity do waysession não está fixado; evdev funciona em qualquer
sessão e no TTY). Sem hardware → fallback silencioso (spec).

**D5 — Áudio: backend do host + dispositivo já existente na VM.**
Prioridade: garantir PipeWire (com camada PulseAudio) instalado **e em execução**
na sessão do appliance (causa provável do som mudo) e alinhar o backend de áudio
do QEMU a ele. Dispositivo do convidado: manter o que o launch já expõe; só mudar
model se o som continuar mudo com o host ok. Readiness ganha checagem de áudio
(não-crítica, com instrução).

**D6 — Nested: habilitar no host + flag na CPU apresentada.**
`/etc/modprobe.d/` com `options kvm_intel nested=1` / `kvm_amd nested=1` aplicado
pelo appliance (e checagem de `/sys/module/kvm_*/parameters/nested`); a CPU da VM
gana a flag de virtualização no ponto único de montagem da CPU (serve A e B — em
B, campo do `vm.toml`; em A, onde quer que o argv seja montado). Readiness distingue
"sem KVM" de "sem nested".

**D7 — Ponto único de extensão do launch (`core/vm_launch.py`).**
Tanto a decisão A (gera `vm.toml` + chama `reims-ctl`) quanto B (argv próprio)
passam por um módulo que recebe: caminhos de disco/OpenCore/OVMF, cpus/RAM da
sugestão do host, `cpu_model` fixo `Haswell-noTSX`, flags (vmx), áudio, e expõe
QMP socket fixo por VM. Itens 2/3/5/6/7 do spec programam contra esse módulo, o
que torna a decisão A/B indiferente aos requisitos deste change.

## Risks / Trade-offs

- [Diagnóstico do bug 2 revelar causa diferente da suspeita] → Plano de
  diagnóstico é a primeira task; design do fix revisado com o usuário se
  necessário (não muda o spec, que só exige "boot sempre funciona").
- [Massa de strings no i18n: texto esquecido em PT] → Checklist de extração +
  varredura por strings literais em `ui/`/`core/` como task de validação.
- [usb-host precisa de permissão/udev e pode conflitar com o host usando o
  dispositivo] → Regras udev no appliance; UI deixa claro que o dispositivo fica
  com a VM enquanto conectado; remoção tratada (spec).
- [Nested depende de BIOS/driver; pode ser impossível em alguns hosts] →
  prontidão explica; non-goal de garantir.
- [Áudio pode exigir troca de model de dispositivo do convidado e afetar
  compatibilidade] → Mudança só com evidência (fase 2 do D5), regressão ouvida.

## Migration Plan

1. i18n entra com catálogo pt completo (status quo) + en; tela de idioma só em
   instalação nova (sistemas instalados ganham `language` com default pt-BR e
   script de troca).
2. Bug 2: correção entregue com regressão nos dois cenários do spec.
3. Passthrough/hotkeys/áudio/nested: ativação incremental por módulo; cada um
   degrada silenciosamente quando o host não suporta.
- Rollback: cada item é aditivo e independente; reverter o commit do módulo não
  afeta o fluxo de instalação.

## Open Questions

- Identidade do waysession do appliance (sway/cage/…) — não bloqueia: D4 é
  independente de compositor.
- Root cause exato do bug 2 — respondido pela primeira task de diagnóstico antes
  da correção (não altera spec nem quebra de fase).
