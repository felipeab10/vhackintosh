## Purpose

Permitir administração remota autenticada do appliance Linux, limitando o acesso a configurações, dispositivos e operações privilegiadas do host.

## ADDED Requirements

### Requirement: Inicialização administrativa e transporte protegido
O sistema SHALL servir o painel por HTTPS e exigir criação de uma credencial administrativa própria usando um código temporário exibido somente no console local. Nenhuma senha padrão ou código reutilizável SHALL ser distribuído na ISO. O código SHALL ser de uso único, expirar e ser invalidado quando substituído.

#### Scenario: Primeiro acesso
- **WHEN** um appliance ainda sem administrador é iniciado
- **THEN** apenas o fluxo de ativação fica disponível remotamente e a criação do administrador exige o código válido mostrado no console local

#### Scenario: Código expirado ou já utilizado
- **WHEN** alguém apresenta um código vencido, substituído ou consumido
- **THEN** a ativação é recusada sem revelar dados do host ou conceder uma sessão

### Requirement: Sessões e autorização administrativa
O sistema SHALL autenticar todas as leituras administrativas, operações, streams e downloads privados; SHALL expirar e revogar sessões, proteger cookies e mutações contra CSRF, validar origem e Host, limitar tentativas de login e permitir logout e recuperação pelo console local. A primeira versão SHALL possuir um papel administrador local.

#### Scenario: Acesso sem sessão
- **WHEN** um cliente sem sessão solicita métricas, logs, backup ou alteração de uma VM
- **THEN** o acesso é negado sem executar a operação ou transmitir conteúdo privado

#### Scenario: Sessão revogada durante transmissão
- **WHEN** uma sessão com stream ativo é revogada
- **THEN** a transmissão deixa de fornecer novos dados e a interface exige nova autenticação

### Requirement: Operações do host com privilégios limitados
O serviço HTTP SHALL executar sem privilégios de root. A execução normal do QEMU e a interpretação de argumentos avançados SHALL ocorrer sem root. Operações privilegiadas SHALL passar por uma interface local autenticada, com ações explícitas, validação de parâmetros e de caminhos; a API SHALL NOT oferecer execução arbitrária de shell ou um terminal root remoto.

#### Scenario: Argumento ou caminho malicioso
- **WHEN** uma requisição contém metacaracteres de shell, caminho fora do armazenamento autorizado ou tentativa de executar outro programa
- **THEN** ela não produz execução de shell nem acesso privilegiado arbitrário e retorna erro explicativo quando viola o contrato

### Requirement: Confirmação e rastreabilidade de ações destrutivas
Desligamento forçado, exclusão de discos, restauração, rollback e ações de energia do host SHALL exigir confirmação explícita identificando o alvo e SHALL registrar ator, horário, resultado e identificador de operação. Senhas, tokens, seriais SMBIOS e outros valores sensíveis SHALL ser omitidos dos logs usuais e exportações de diagnóstico.

#### Scenario: Desligamento forçado confirmado
- **WHEN** o administrador confirma forçar o desligamento de uma VM identificada
- **THEN** somente aquela VM é afetada e a ação e seu resultado aparecem no histórico

### Requirement: Exposição de rede controlada
O painel SHALL estar disponível nas interfaces LAN habilitadas do appliance sem abertura automática de portas no roteador, túneis externos ou desativação geral do firewall. QMP e a interface privilegiada SHALL permanecer locais.

#### Scenario: Serviço iniciado na LAN
- **WHEN** o appliance obtém um endereço de rede
- **THEN** o endereço HTTPS configurado permite chegar ao login e não expõe o monitor QMP ou o canal privilegiado pela rede
