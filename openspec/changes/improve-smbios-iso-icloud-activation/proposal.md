## Why

A ISO atual inclui a kext ReimsVgpu, mas sua EFI empacotada ainda expõe serial/MLB/UUID/ROM pessoais, e a árvore ReimsVgpu é copiada sem prova de proveniência nem garantia de compilação offline. Além disso, o provisionamento gera seriais com formatos aleatórios próprios e pode ocultar falha de injeção; em contraste, o fluxo usado com sucesso na VM Tahoe pelo ultimate-macOS-KVM usa `macserial` para gerar os campos SMBIOS, e o appliance atual não ativa iCloud com confiabilidade.

## What Changes

- Validar o conteúdo da ISO final — fontes/pins completos do reims-vgpu, OpenCore, OVMF e configuração EFI — e falhar o build se um requisito estiver ausente, incoerente ou ainda contiver identificadores de uma VM.
- Empacotar um template OpenCore sanitizado, mantendo `OVMF_VARS.fd` genérico e nunca incorporando NVRAM ativo.
- Substituir o gerador aleatório de identidade por geração por VM usando `macserial` (ou componente oficial compatível), com identidade única/persistente e validação antes do boot.
- Garantir que a mesma identidade consistente esteja em `PlatformInfo -> Generic` e nos caminhos automáticos OpenCore relevantes; não reutilizar identidade da ISO, de outra VM ou a identidade de Tahoe existente.
- Tornar falhas ao gerar/injetar SMBIOS impeditivas para concluir provisionamento; remover exceções silenciosas.
- Acrescentar diagnóstico e procedimento confirmado para VMs existentes com falha de iCloud, sem regeneração automática nem promessa de ativação garantida.
- Documentar que identificadores gerados são identidades técnicas sintéticas; não representam hardware Apple legítimo e não contornam termos, verificações ou restrições de serviço da Apple.

## Capabilities

### New Capabilities
- `iso-integrity-and-efi`: Build reprodutível/verificável da ISO, conteúdo esperado e proteção de dados sensíveis na EFI.
- `per-vm-smbios-identity`: Geração, validação, persistência e injeção atômica de identidade OpenCore própria por VM.
- `icloud-readiness-diagnostics`: Verificações de consistência e orientações de diagnóstico para erro de ativação, sem garantia de aceitação pela Apple.

### Modified Capabilities
- Nenhuma. `openspec/specs/` não possui capacidades existentes a modificar; a mudança estabelece contratos novos.

## Impact

- `appliance/build-iso.sh`, perfil Archiso, dependências/fontes QEMU e build de rootfs: pins de fonte, vendoring/offline, validação de hash e inspeção da ISO concluída.
- `core/smbios.py`, `core/disk.py`, `core/config.py`, `ui/tui.py` e `core/i18n.py`: geração `macserial`, campos SMBIOS, persistência, validação e fluxo de erro.
- Templates `templates/config.plist`, `templates/OpenCore.qcow2`, OVMF e testes para detectar valores pessoais, confirmar presença/estado habilitado da kext e comparar a EFI da ISO.
- `ultimate-macOS-KVM/resources/python/smbios/GenSMBIOS.py` será referência observável do comportamento (`macserial -a`, ProductName, SerialNumber, BoardSerialNumber, SmUUID e ROM), não uma fonte para copiar código sem revisar licença/proveniência.
