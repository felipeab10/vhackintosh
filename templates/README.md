# Templates de boot

A configuração foi validada em uma instalação limpa do macOS Tahoe 26.7.1 no reims-vgpu: o primeiro desktop e o Dock apareceram sem aplicar `METAL_ERROR_MODE=5` pelo Recovery. A EFI da VM foi gerada pelo `reims-util` oficial com os kexts ReimsVgpu/APV. O provisionador agora gera a EFI de cada VM com a versão instalada do utilitário, em vez de copiar o fallback antigo do checkout.

| Arquivo | O que é | Tamanho esperado |
|---|---|---|
| `OpenCore.qcow2` | Fallback de desenvolvimento; a ISO gera e valida uma EFI de referência | 384 MiB virtuais (ISO: 128 MiB) |
| `OVMF_VARS.fd` | NVRAM do OVMF (par do `OVMF_CODE_4M.fd`) | 540672 bytes |
| `OVMF_CODE_4M.fd` | Código OVMF x86_64 4M com splash vHackintosh embutido | 3653632 bytes |
| `vhackintosh-boot-logo.bmp` | Splash 8-bit usado pelo OVMF, em 386×116 px | 46086 bytes |
| `vhackintosh-boot-logo.png` | Arte original do splash, com transparência | — |
| `config.plist` | Configuração do fallback de desenvolvimento; a ISO grava aqui a configuração gerada pelo utilitário | — |

O splash foi inspirado na identidade visual do [site vHackintosh](https://vhackintosh.proclin.com.br/). O BMP é incorporado em `MdeModulePkg/Logo/Logo.bmp` no OVMF; novas VMs recebem o logo pelo `OVMF_CODE_4M.fd` deste template.

## Compatibilidade com ReimsVgpu

O `reims-util opencore` inclui Lilu, `ReimsVgpu.kext` e os kexts APV arm64-port por padrão, limitados às versões de kernel do macOS 26. Cada VM recebe sua identidade SMBIOS na geração da EFI. `OVMF_VARS.fd` versionado é o modelo limpo, não o NVRAM de uma VM.

A logo permanece em `OVMF_CODE_4M.fd`: `reims-util opencore --no-ovmf` gera apenas o disco OpenCore, e o vHackintosh copia seu firmware OVMF personalizado para cada VM.

**Fallback de desenvolvimento:** o `OpenCore.qcow2` ainda versionado no repositório é o artefato anterior de 384 MiB, com OpenCore 1.0.8. Novas VMs não usam esse fallback: a criação exige um `reims-util` que ofereça os kexts APV; se ele estiver ausente ou desatualizado, o provisionamento falha com uma instrução de atualização em vez de gerar uma EFI sem a correção.

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

Na instalação limpa validada com Tahoe 26.7.1 e os kexts APV arm64-port, o
desktop apareceu sem definir `METAL_ERROR_MODE=5`. Essa observação cobre a
combinação testada de Tahoe, reims-vgpu e reims-util; se outra versão voltar a
apresentar o crash do `iconservicesagent`, o procedimento de Recovery descrito
em `docs/qemu.md` continua disponível como diagnóstico e recuperação.

## Proveniência e licença

- **OpenCore**: partir do build do **[Ultimate-macOS-KVM (ULTMOS)]**
  (https://github.com/UltimatemacOSKVM) — configuração pensada pra QEMU/KVM —
  sobre o **OpenCorePkg** (BSD-3-Clause, `OCLP-Resources` via PatcherAuxTools).
- **SMBIOS**: os seriais de cada VM são gerados localmente pelo `GenSMBIOS`
  (MacSerial) na criação — nada de seriais versionados aqui.
- Para a imagem da ISO, o `config.plist` é extraído do EFI recém-gerado e
  empacotado junto dela; a configuração acima descreve apenas o fallback
  versionado do checkout.
