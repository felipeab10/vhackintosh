# Templates de boot

A configuração descrita foi testada com macOS Tahoe 26.6.2 no reims-vgpu (KVM + GPU virtual, desktop e Dock completos). O template EFI abaixo é preparado para novas VMs; sua kext, isoladamente, não garante reprodução do runtime. O fluxo appliance copia o `.qcow2` e injeta SMBIOS por VM.

| Arquivo | O que é | Tamanho esperado |
|---|---|---|
| `OpenCore.qcow2` | Partição EFI com OpenCore e o `config.plist` abaixo | ~21 MB |
| `OVMF_VARS.fd` | NVRAM do OVMF (par do `OVMF_CODE_4M.fd`) | 540672 bytes |
| `OVMF_CODE_4M.fd` | Código do OVMF 4M (se o host não levar o próprio) | 3653632 bytes |
| `config.plist` | Cópia em texto do dentro do `OpenCore.qcow2`, versionada pra review | — |

## Compatibilidade com ReimsVgpu

O template inclui `ReimsVgpu.kext` habilitada para `x86_64` e macOS 26 (Kernel 25.x), construída a partir de `reims-util` commit `5405b2d`. Lilu foi atualizado para a versão usada pela combinação testada e também limitado a macOS 26; isto é intencional, não um template genérico para versões anteriores/futuras. Serial/MLB estão vazios e UUID/ROM são placeholders neutros; cada VM deve receber identidade SMBIOS própria na criação. `OVMF_VARS.fd` versionado é o modelo limpo, não o NVRAM da VM testada.

**Estado de verificação (2026-10-13):** a EFI foi inspecionada e sanitizada em `DataHub`, `Generic`, `PlatformNVRAM` e `SMBIOS`; o `config.plist` versionado tem o mesmo SHA-256 da cópia extraída da imagem. `qemu-img check` não encontrou erros. Isso valida o artefato EFI, não um boot Tahoe nem a GPU acelerada.

A mesma experiência exige o host build específico do `reims-vgpu` (QEMU customizado com Apple-GFX e Vulkan), submódulo QEMU e pin Metal2Vulkan. Uma instalação/rebuild só da branch `master` não garante os mesmos artefatos. Ter a kext no OpenCore, por si só, não prova aceleração; valide desktop, carregamento da kext e `chrome://gpu` na VM.

## A config que funciona (resumo do `config.plist`)

- **`Kernel > Emulate > Cpuid1Data`** = `54 06 05 00 …` com máscara `ff ff ff 00`
  — emula Haswell (CPUID 0x050654). O appliance hoje usa
  `-cpu Skylake-Client-noTSX-IBRS` (default medido do reims-vgpu —
  docs/qemu.md, "x86-64 macOS guests: the CPU model": model 94 que o XNU
  conhece e boota **com ou sem** este spoof); o spoof do ULTMOS continua aqui,
  inofensivo, e foi o que garantiu o boot quando o modelo era Haswell-noTSX.
- **`Kernel > Emulate > DummyPowerManagement`** = true
- **boot-args** = `keepsyms=1 tlbto_us=0 vti=9` (quirks do QEMU/paravirt)
- **`Misc > Boot`**: `ShowPicker` true, `Timeout` 45 s, `PollAppleHotKeys` true

Depois de instalar o macOS no guest, gravar `METAL_ERROR_MODE=5` em
`/Library/Preferences/.GlobalPreferences` (no volume *- Data) — sem isso o
`iconservicesagent` entra em crash-loop no Tahoe x86-64 e o desktop fica sem
Dock/ícones. É o fix documentado no reims-vgpu; num install **novo** o desktop
nem sobe sem ele — faça-o do Recovery (OpenCore > Recovery, Utilities >
Terminal, no volume *Data; ver docs/qemu.md, "Metal's error mode").

## Proveniência e licença

- **OpenCore**: partir do build do **[Ultimate-macOS-KVM (ULTMOS)]**
  (https://github.com/UltimatemacOSKVM) — configuração pensada pra QEMU/KVM —
  sobre o **OpenCorePkg** (BSD-3-Clause, `OCLP-Resources` via PatcherAuxTools).
- **SMBIOS**: os seriais de cada VM são gerados localmente pelo `GenSMBIOS`
  (MacSerial) na criação — nada de seriais versionados aqui.
- `config.plist` aqui é o estado salvo da imagem; ao mudar o `.qcow2`,
  re-extrair (`7z e OpenCore.qcow2 0.primary.img` → `EFI/OC/config.plist`) e
  commitar junto.
