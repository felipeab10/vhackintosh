# Templates de boot

A combinação versionada aqui é a que **provou funcionar**: macOS Tahoe 26.6.2
bootando no reims-vgpu (KVM + GPU virtual, desktop e Dock completos), usada como
base para toda VM nova criada pelo appliance (`core/disk.py` copia o `.qcow2`
pra pasta da VM e injeta os seriais GenSMBIOS com `guestfish`).

| Arquivo | O que é | Tamanho esperado |
|---|---|---|
| `OpenCore.qcow2` | Partição EFI com OpenCore e o `config.plist` abaixo | ~21 MB |
| `OVMF_VARS.fd` | NVRAM do OVMF (par do `OVMF_CODE_4M.fd`) | 540672 bytes |
| `OVMF_CODE_4M.fd` | Código do OVMF 4M (se o host não levar o próprio) | 3653632 bytes |
| `config.plist` | Cópia em texto do dentro do `OpenCore.qcow2`, versionada pra review | — |

## A config que funciona (resumo do `config.plist`)

- **`Kernel > Emulate > Cpuid1Data`** = `54 06 05 00 …` com máscara `ff ff ff 00`
  — emula Haswell (CPUID 0x050654); pareia com `-cpu Haswell-noTSX` no QEMU
- **`Kernel > Emulate > DummyPowerManagement`** = true
- **boot-args** = `keepsyms=1 tlbto_us=0 vti=9` (quirks do QEMU/paravirt)
- **`Misc > Boot`**: `ShowPicker` true, `Timeout` 45 s, `PollAppleHotKeys` true

Depois de instalar o macOS no guest, gravar `METAL_ERROR_MODE=5` em
`/Library/Preferences/.GlobalPreferences` (no volume *- Data) — sem isso o
`iconservicesagent` entra em crash-loop no Tahoe x86-64 e o desktop fica sem
Dock/ícones. É o fix documentado no reims-vgpu.

## Proveniência e licença

- **OpenCore**: partir do build do **[Ultimate-macOS-KVM (ULTMOS)]**
  (https://github.com/UltimatemacOSKVM) — configuração pensada pra QEMU/KVM —
  sobre o **OpenCorePkg** (BSD-3-Clause, `OCLP-Resources` via PatcherAuxTools).
- **SMBIOS**: os seriais de cada VM são gerados localmente pelo `GenSMBIOS`
  (MacSerial) na criação — nada de seriais versionados aqui.
- `config.plist` aqui é o estado salvo da imagem; ao mudar o `.qcow2`,
  re-extrair (`7z e OpenCore.qcow2 0.primary.img` → `EFI/OC/config.plist`) e
  commitar junto.
