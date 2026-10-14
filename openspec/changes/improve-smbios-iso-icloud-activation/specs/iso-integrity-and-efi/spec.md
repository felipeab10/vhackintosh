## Purpose

Garante que a ISO publicável contenha fontes e dependências identificáveis do reims-vgpu, uma EFI OpenCore coerente com a kext exigida e apenas firmware genérico, detectando falhas de integridade e exposição acidental de identidade pessoal antes de concluir o build.

## ADDED Requirements

### Requirement: A ISO SHALL include a complete, traceable ReimsVgpu build input
O build SHALL identificar e registrar o commit da raiz reims-vgpu e o commit efetivo do submódulo QEMU, incluir os arquivos de fonte e dependências necessários à estratégia de build declarada, e falhar em vez de produzir uma ISO aparentemente válida quando algum input obrigatório faltar ou estiver incoerente.

#### Scenario: Dependências locais configuradas para build offline
- **WHEN** o perfil declara build sem rede no primeiro boot
- **THEN** a ISO inclui fontes vendorizadas e configuração Cargo que as ativa, e valida a presença dos scripts e QEMU no commit identificado antes de aceitar a imagem

#### Scenario: Fonte parcial ou submódulo incoerente
- **WHEN** faltar uma fonte/dependência requerida ou o submódulo QEMU não corresponder ao pin anunciado
- **THEN** a geração encerra com erro identificando o componente e não publica a ISO como sucesso

#### Scenario: Identificar conteúdo de fonte da ISO existente
- **WHEN** o operador inspeciona uma ISO concluída
- **THEN** manifestos verificáveis informam os commits/pins e hashes do payload necessários para comparar com o checkout pretendido

### Requirement: A ISO SHALL contain a valid, generic OpenCore EFI with ReimsVgpu enabled
O artefato OpenCore empacotado SHALL ser estruturalmente íntegro, conter a kext ReimsVgpu e dependências necessárias (incluindo Lilu), com entradas de Kernel/Add habilitadas e compatíveis com a faixa de macOS declarada. O config externo distribuído SHALL corresponder à cópia na imagem, e OVMF SHALL ser o modelo genérico, nunca VARS/NVRAM extraído de uma VM ativa.

#### Scenario: Validar EFI durante empacotamento
- **WHEN** o build prepara a ISO
- **THEN** valida a imagem OpenCore, extrai sua configuração, confirma kexts habilitadas e faixa-alvo, compara o config externo e confirma a integridade de OVMF genérico; falha em qualquer diferença inesperada

#### Scenario: Kext ausente ou desabilitada
- **WHEN** a imagem não contém ReimsVgpu/Lilu ou a entrada está desabilitada/incompatível com a faixa declarada
- **THEN** o build falha e aponta a configuração inválida

### Requirement: A ISO SHALL not disclose any VM-specific SMBIOS or NVRAM identity
O template EFI e a ISO SHALL NOT conter serial, MLB, UUID, ROM ou dados de NVRAM provenientes de uma VM criada/usada. Valores neutros/placeholders são permitidos apenas no template e não devem ser tratados como identidade para boot de VM.

#### Scenario: Scan de identidade pessoal detecta valores
- **WHEN** qualquer campo SMBIOS/NVRAM do config ou VARS apresenta identidade não-placeholder da VM
- **THEN** o build falha sem copiar ou divulgar o valor em logs, reportando somente campo e arquivo a corrigir

#### Scenario: EFI template sanitizada
- **WHEN** a ISO passa todas as validações
- **THEN** os campos específicos da VM estão vazios/neutros, a configuração é idêntica à incluída no OpenCore.qcow2, e OVMF_VARS é uma cópia genérica do template versionado

### Requirement: A ISO final SHALL pass integrity verification before publication
O build SHALL validar bootabilidade declarada, manifestos internos e correspondência byte/hash entre artefatos críticos esperados e os incluídos na ISO, preservando outputs anteriores até que o novo build passe e seja publicado com checksum externo atualizado.

#### Scenario: Build e publicação corretos
- **WHEN** o build termina com todos os checks estruturais, de conteúdo e integridade aprovados
- **THEN** a ISO nova e seu checksum são publicados e o relatório indica resultado, tamanho, hash e revisão de fonte

#### Scenario: Validação final falha
- **WHEN** hash, conteúdo, EFI ou boot catalog não passa
- **THEN** o build informa falha, não substitui artefato anteriormente válido nem gera checksum de sucesso
