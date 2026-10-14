## Purpose

Garante que cada VM criada pelo appliance receba uma identidade OpenCore própria, persistente e validada, gerada conforme o fluxo macserial adotado pelo usuário e aplicada integralmente antes do primeiro boot, sem expor ou compartilhar identificadores entre VMs.

## ADDED Requirements

### Requirement: Generate one internally consistent identity for each new VM
Durante o provisionamento, o appliance SHALL gerar um conjunto novo por VM com modelo, serial Apple, MLB, UUID e ROM, usando macserial do OpenCorePkg ou um componente oficial compatível e rastreável. Não deve reutilizar a identidade de template, de outra VM, de execução anterior nem da VM Tahoe preexistente.

#### Scenario: Provisionar nova VM
- **WHEN** o assistente cria uma VM
- **THEN** gera um único conjunto de identidade para o modelo selecionado, valida formato/consistência e o associa exclusivamente ao ID dessa VM

#### Scenario: macserial indisponível ou retorna dados inválidos
- **WHEN** a ferramenta oficial não pode ser obtida/executada ou sua resposta não é válida para o modelo
- **THEN** o provisionamento falha com etapa e ação corretiva explícitas, sem recorrer silenciosamente ao gerador randômico legado

#### Scenario: Duas VMs provisionadas
- **WHEN** duas VMs novas são criadas separadamente
- **THEN** cada configuração persistida tem UUID e tupla Serial/MLB/ROM próprios, sem colisões entre VMs

### Requirement: Persist and protect VM identity across restarts
O appliance SHALL persistir a identidade em configuração específica da VM com permissões apropriadas e SHALL reutilizar exatamente a mesma identidade em reinicializações e atualizações da VM. A ISO e logs não devem conter ou revelar os valores pessoais gerados.

#### Scenario: Reiniciar a mesma VM
- **WHEN** uma VM com identidade criada é iniciada novamente
- **THEN** OpenCore/QEMU usam os mesmos valores persistidos, sem regeneração implícita

#### Scenario: Identidade incompleta ou configuração antiga
- **WHEN** a configuração de uma VM existente não tem todos os campos necessários
- **THEN** o sistema informa que a VM requer diagnóstico/reparo confirmado e não troca identificadores automaticamente

### Requirement: Apply identity atomically to all relevant OpenCore fields before boot
O provisionamento SHALL aplicar uma única tupla validada a `PlatformInfo -> Generic` e assegurar consistência com `DataHub`, `PlatformNVRAM` e `SMBIOS` segundo a configuração OpenCore usada, em uma alteração atômica da imagem EFI. Nenhum boot pode começar com aplicação parcial ou apenas um subconjunto de campos atualizado.

#### Scenario: Injeção bem-sucedida
- **WHEN** a imagem EFI pode ser lida, atualizada e validada novamente
- **THEN** os campos relevantes correspondem ao conjunto persistido da VM e a EFI só é promovida ao caminho ativo após validação

#### Scenario: Leitura, escrita ou validação EFI falha
- **WHEN** qualquer operação falha ou não consegue confirmar os campos injetados
- **THEN** o arquivo anterior permanece íntegro, o provisionamento para e nenhum boot é iniciado

### Requirement: User can inspect VM identity and explicitly recover an existing VM
A interface SHALL permitir verificar modelo e presença/consistência da identidade sem expor segredos em logs, e qualquer regeneração de identidade em VM existente SHALL exigir ação explícita, aviso de impacto em serviços Apple e confirmação do usuário, preservando opção de backup.

#### Scenario: Diagnóstico sem regeneração
- **WHEN** usuário consulta identidade/diagnóstico de VM existente
- **THEN** interface apresenta apenas estado/últimos caracteres necessários e não modifica EFI nem identidade

#### Scenario: Regeneração explicitamente confirmada
- **WHEN** usuário escolhe regenerar e confirma após aviso e backup
- **THEN** o sistema cria nova tupla por VM, atualiza EFI e persistência atomicamente e registra que o estado de sessão/ativação no macOS pode exigir recuperação
