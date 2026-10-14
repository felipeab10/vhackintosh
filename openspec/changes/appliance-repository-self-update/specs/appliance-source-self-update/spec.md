## Purpose

Permite que a appliance atualize código-fonte dos dois repositórios que a compõem e regenere localmente o motor gráfico compatível com esse código, sem depender de releases binárias pré-compiladas.

## ADDED Requirements

### Requirement: Atualizar os dois repositórios implantados a partir de seus remotes Git
A appliance SHALL atualizar os repositórios `felipeab10/reims-vgpu` e `felipeab10/vhackintosh` instalados localmente, obtendo a referência remota configurada para cada um e sem baixar assets binários de GitHub Releases. A operação SHALL verificar o branch remoto, conectividade e estado local antes de alterar arquivos.

#### Scenario: Atualização normal dos dois repositórios
- **WHEN** o usuário escolhe atualizar e ambos os checkouts estão limpos, em branches compatíveis, e seus remotes têm novos commits fast-forward
- **THEN** a appliance atualiza ambos os checkouts para os commits remotos e informa os hashes/referências instalados

#### Scenario: Um dos repositórios já está atualizado
- **WHEN** um checkout já corresponde ao remoto e o outro tem commits novos aplicáveis
- **THEN** a appliance mantém o checkout atualizado sem alterações e atualiza o outro, sem exigir release ou asset binário

#### Scenario: Sem conectividade ou remote inválido
- **WHEN** um remote não pode ser consultado ou não corresponde ao repositório esperado
- **THEN** a atualização informa qual repositório falhou, não declara sucesso global e não descarta o estado local

### Requirement: Proteger modificações locais e garantir atualização coerente
A appliance SHALL detectar modificações locais, commits locais não enviados, branches incompatíveis e atualizações não fast-forward antes de substituir qualquer checkout. Ela MUST NOT descartar, resetar ou sobrescrever alterações locais silenciosamente; deve interromper a atualização com instruções para o usuário preservar ou resolver o estado.

#### Scenario: Checkout com arquivos locais modificados
- **WHEN** qualquer um dos dois checkouts contém arquivos modificados ou não rastreados que seriam afetados
- **THEN** a appliance interrompe a atualização antes de substituir arquivos e identifica o checkout que requer limpeza ou backup

#### Scenario: Atualização exige merge ou troca de branch
- **WHEN** o checkout local divergiu do remote, ou a branch ativa não corresponde à branch de atualização configurada
- **THEN** a appliance não faz reset nem merge automático e apresenta a ação de recuperação necessária

### Requirement: Rebuild completo e atômico do motor local
Após atualizar com sucesso ambos os checkouts, a appliance SHALL executar o fluxo suportado de rebuild completo do reims-vgpu, incluindo biblioteca/ROM, QEMU e reims-ctl, usando as fontes locais atualizadas. O sistema SHALL manter os binários previamente instalados disponíveis até que o rebuild novo termine com sucesso, e SHALL só informar sucesso final após validação dos artefatos novos.

#### Scenario: Rebuild completo concluído
- **WHEN** ambos os repositórios atualizaram e todas as etapas de compilação e instalação completam com sucesso
- **THEN** a appliance aponta para os novos artefatos locais, atualiza o stamp de conteúdo e informa os commits e build instalados

#### Scenario: Rebuild falha após atualização do fonte
- **WHEN** qualquer etapa do rebuild falha
- **THEN** a appliance informa etapa e erro, mantém o binário/build anterior utilizável, não grava stamp de sucesso para o fonte novo e não relata atualização completa

#### Scenario: Fonte já foi atualizado mas rebuild ainda está pendente
- **WHEN** a sincronização dos dois repositórios conclui e o rebuild não iniciou ou falhou
- **THEN** a appliance detecta no próximo uso que o stamp não corresponde ao fonte, solicita/reexecuta o rebuild antes de iniciar uma VM e não usa silenciosamente um binário incompatível

### Requirement: Apresentar progresso e resultado verídico
A interface SHALL explicar que a operação atualiza fontes e compila localmente, mostrar progresso/resultado por repositório e por etapa do rebuild, e diferenciar sucesso total, sem atualizações, falha recuperável e estado parcial que exige ação do usuário.

#### Scenario: Atualização e rebuild bem-sucedidos
- **WHEN** as duas atualizações e o rebuild terminam com sucesso
- **THEN** a interface mostra uma confirmação de sucesso total com os commits/build instalados

#### Scenario: Falha parcial
- **WHEN** um repositório foi atualizado, mas o segundo ou o rebuild falha
- **THEN** a interface relata o estado parcial e a ação necessária sem mostrar mensagem de sucesso total
