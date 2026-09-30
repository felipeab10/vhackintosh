# Tarefa T03: Core GenSMBIOS & OpenCore Injector

## Objetivo
Gerar automaticamente números de série válidos da Apple (SMBIOS) e injetá-los no arquivo `config.plist` do OpenCore sem necessidade de ferramentas manuais.

## Itens a Desenvolver
- [ ] Módulo GenSMBIOS nativo em Python:
  - Geração de modelo apropriado por versão do macOS (ex: `MacPro7,1`, `iMacPro1,1`, `MacBookPro16,1`).
  - Geração de Serial Number com checksum válido de ano/semana/fábrica.
  - Geração de Board Serial (MLB de 17 caracteres).
  - Geração de SmUUID aleatório (UUID v4 formatado).
  - Geração de ROM (MAC Address virtual de 12 hex).
- [ ] Injetor no `config.plist`:
  - Modificação in-place usando `plistlib` nos nós `PlatformInfo -> Generic`.
  - Configuração de `Automatic = true`, `UpdateDataHub = true`, `UpdateNVRAM = true`, `UpdateSMBIOS = true`.
