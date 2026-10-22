# Templates de boot

A configuração descrita foi testada com macOS Tahoe no reims-vgpu (KVM + GPU virtual, desktop e Dock completos). No build da ISO, `appliance/build-iso.sh` substitui a imagem OpenCore de desenvolvimento por uma EFI gerada pelo `reims-util` oficial, com os kexts atuais de ReimsVgpu/APV. A appliance leva essa imagem pronta para que a criação de VMs também funcione offline. A identidade SMBIOS é injetada por VM.

| Arquivo | O que é | Tamanho esperado |
|---|---|---|
| `OpenCore.qcow2` | Fallback de desenvolvimento; a ISO substitui pelo resultado de `reims-util` | 384 MiB virtuais (ISO: 128 MiB) |
| `OVMF_VARS.fd` | NVRAM do OVMF (par do `OVMF_CODE_4M.fd`) | 540672 bytes |
| `OVMF_CODE_4M.fd` | Código OVMF x86_64 4M com splash vHackintosh embutido | 3653632 bytes |
| `vhackintosh-boot-logo.bmp` | Splash 8-bit usado pelo OVMF, em 386×116 px | 46086 bytes |
| `vhackintosh-boot-logo.png` | Arte original do splash, com transparência | — |
| `config.plist` | Configuração do fallback de desenvolvimento; a ISO grava aqui a configuração gerada pelo utilitário | — |

O splash foi inspirado na identidade visual do [site vHackintosh](https://vhackintosh.proclin.com.br/). O BMP é incorporado em `MdeModulePkg/Logo/Logo.bmp` no OVMF; novas VMs recebem o logo pelo `OVMF_CODE_4M.fd` deste template.

## Compatibilidade com ReimsVgpu

O OpenCore gerado para a ISO inclui Lilu, `ReimsVgpu.kext` e os kexts APV incluídos por padrão no `reims-util`, limitados às versões de kernel do macOS 26. Serial/MLB ficam vazios e UUID/ROM neutros no artefato da ISO; cada VM recebe identidade SMBIOS própria na criação. `OVMF_VARS.fd` versionado é o modelo limpo, não o NVRAM de uma VM.

A logo permanece em `OVMF_CODE_4M.fd`: `reims-util opencore --no-ovmf` gera apenas o disco OpenCore, e o vHackintosh continua copiando seu firmware OVMF personalizado para cada VM.

**Fallback de desenvolvimento:** o `OpenCore.qcow2` ainda versionado no repositório é o artefato anterior de 384 MiB, com OpenCore 1.0.8. A appliance não o usa: o build substitui esse arquivo por uma imagem do `reims-util`, confere kexts/identidade neutra e valida o `config.plist` incluído.

A mesma experiência exige o host build específico do `reims-vgpu` (QEMU customizado com Apple-GFX e Vulkan), submódulo QEMU e pin Metal2Vulkan. Uma instalação/rebuild só da branch `master` não garante os mesmos artefatos. Ter a kext no OpenCore, por si só, não prova aceleração; valide desktop, carregamento da kext e `chrome://gpu` na VM.

## Configuração do fallback versionado

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
- Para a imagem da ISO, o `config.plist` é extraído do EFI recém-gerado e
  empacotado junto dela; a configuração acima descreve apenas o fallback
  versionado do checkout.
