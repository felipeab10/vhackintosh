## Purpose

Gerenciar imagens e discos e oferecer mecanismos offline de recuperação e cópia sem comprometer VMs existentes, suas identidades ou o armazenamento do host.

## ADDED Requirements

### Requirement: Biblioteca de mídia e discos
O painel SHALL listar imagens de recuperação e discos com tamanho virtual, espaço ocupado, local, vínculos e espaço livre do host, permitindo download Apple, importação e upload com limites e progresso. Operações SHALL validar formato, integridade disponível e caminhos autorizados, sem alterar discos associados a processos ativos.

#### Scenario: Upload excede capacidade
- **WHEN** uma mídia excede o limite ou o espaço livre reservado para recebimento
- **THEN** o sistema recusa ou interrompe a operação com motivo, remove somente o temporário da tarefa e preserva arquivos existentes

### Requirement: Backup offline consistente e restauração revisável
O sistema SHALL permitir backup de uma VM desligada, incluindo discos necessários, EFI/OpenCore, NVRAM, configuração, identidade e manifesto com verificações de integridade. Restauração SHALL validar integridade, espaço e conflitos antes de publicar a VM restaurada e exigir confirmação antes de substituir dados.

#### Scenario: Restaurar backup íntegro
- **WHEN** o administrador restaura um conjunto íntegro para o mesmo identificador com a VM desligada
- **THEN** a configuração e a identidade originais são recuperadas com todos os discos declarados ou a tarefa falha antes da publicação se faltar um componente

### Requirement: Clonagem com identidade independente
O sistema SHALL oferecer clonagem offline, atribuir novo identificador, MAC e identidade SMBIOS e gerar EFI/NVRAM consistentes para o clone, mantendo a origem intacta. Um restore que preserve identidade SHALL NOT ser tratado como clone e SHALL impedir duas cópias ativas com identidade conflitante.

#### Scenario: Clonar VM instalada
- **WHEN** uma VM desligada é clonada
- **THEN** o clone recebe disco independente e identidade própria, sem modificar firmware, discos ou seriais da origem

### Requirement: Snapshots condicionados à compatibilidade
Snapshots e rollback SHALL ser oferecidos somente para conjuntos de discos offline suportados pelo formato e runtime; SHALL incluir o estado persistente de EFI/NVRAM necessário e distinguir snapshot de disco de snapshot de memória. Configurações incompatíveis com VFIO, discos externos ou dependências não incluídas SHALL expor a restrição. Snapshot SHALL NOT ser anunciado como backup independente.

#### Scenario: Configuração não suportada
- **WHEN** o conjunto de armazenamento contém uma dependência que o mecanismo não pode capturar consistentemente
- **THEN** o controle de snapshot é bloqueado com motivo e a opção de backup compatível é apresentada quando disponível

### Requirement: Exclusão e recuperação de tarefas
Excluir cadastro e apagar seus discos SHALL ser ações separadas. O sistema SHALL impedir exclusão de mídia compartilhada em uso, serializar operações de armazenamento por VM e recuperar ou identificar temporários de tarefas interrompidas sem remover arquivos de outras VMs.

#### Scenario: Excluir somente cadastro
- **WHEN** o administrador confirma remover uma VM sem marcar exclusão dos discos
- **THEN** o cadastro é removido e os discos permanecem identificáveis para posterior importação
