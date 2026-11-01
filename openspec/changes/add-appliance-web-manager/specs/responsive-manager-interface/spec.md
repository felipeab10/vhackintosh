## Purpose

Oferecer um painel de administração legível e responsivo, com identidade futurista coerente com o site vHackintosh e controles acessíveis para operações de infraestrutura.

## ADDED Requirements

### Requirement: Identidade visual futurista do projeto
O painel SHALL adotar tema escuro inspirado em https://vhackintosh.proclin.com.br: fundo quase preto, superfícies escuras, acentos azul/ciano, grade discreta, brilho contido e tipografia legível com números tabulares. A identidade SHALL ser consistente em formulários, tabelas, gráficos, diálogos e estados. Decoração SHALL NOT reduzir contraste ou consumir processamento contínuo desnecessário.

#### Scenario: Abrir páginas de administração
- **WHEN** o administrador alterna entre host, VM e configurações
- **THEN** navegação, cores semânticas, tipografia, controles e indicadores mantêm o mesmo design system

### Requirement: Páginas e navegação por contexto
O painel SHALL oferecer visão geral, host/telemetria, lista de VMs, criação, detalhes da VM com resumo/hardware/GPU/armazenamento/rede/console/logs/opções avançadas, biblioteca de mídia, backups, tarefas e configurações de acesso/runtime. Rotas SHALL aceitar links diretos e manter o contexto da VM, com estados vazios, carregamento, erro e desconexão.

#### Scenario: Link direto para hardware da VM
- **WHEN** uma sessão autenticada abre a rota de hardware de uma VM existente
- **THEN** a página seleciona a VM correta e mostra configuração salva, alterações pendentes e estado atual

### Requirement: Adaptação mobile e desktop
As páginas SHALL funcionar a partir de 360 px de largura e em tablet e desktop, sem rolagem horizontal da página. No mobile, navegação SHALL recolher, tabelas principais SHALL virar cartões ou listas, formulários SHALL empilhar e ações SHALL continuar acessíveis sem depender de hover. Código, logs e tabelas técnicas extensas SHALL ter rolagem local identificável quando necessária.

#### Scenario: Gerenciar VM pelo celular
- **WHEN** o administrador usa viewport de 375 px para criar, editar ou parar uma VM
- **THEN** os campos, erros, confirmações e ações ficam utilizáveis com toque e teclado virtual, sem corte do conteúdo principal

### Requirement: Acessibilidade dos controles e gráficos
A interface SHALL possuir navegação por teclado, foco visível, rótulos, contraste mínimo de 4,5:1 em texto normal e 3:1 em controles essenciais, alvos de toque de pelo menos 44 × 44 CSS px e respeito a movimento reduzido. Estados SHALL usar texto/forma além da cor; gráficos SHALL fornecer valores e alternativa textual. Atualizações contínuas SHALL permitir pausar a apresentação sem interromper a coleta.

#### Scenario: Usuário com movimento reduzido e leitor de tela
- **WHEN** o usuário habilita movimento reduzido e navega nos indicadores
- **THEN** animações decorativas são removidas, controles permanecem operáveis e valores/estados podem ser compreendidos sem depender de brilho, cor ou tooltip

### Requirement: Formulários previsíveis e falhas visíveis
Formulários SHALL separar opções básicas e avançadas, preservar entradas após erro, associar erros aos campos e exigir revisão antes de operações destrutivas. A interface SHALL apresentar controles indisponíveis com motivo e distinguir tarefa em andamento de operação concluída.

#### Scenario: Configuração inválida
- **WHEN** o administrador envia uma configuração com erros em vários campos
- **THEN** o formulário mantém os valores, apresenta resumo focalizável com links para os campos e não anuncia sucesso

### Requirement: Recursos locais e desempenho de apresentação
Fontes, ícones, scripts e estilos necessários SHALL estar empacotados localmente para uso offline. Gráficos, logs e editores pesados SHALL carregar sob demanda e manter buffers limitados. Telas ocultas SHALL reduzir renderização e assinaturas desnecessárias.

#### Scenario: Appliance sem Internet
- **WHEN** um dispositivo da LAN abre o painel de um appliance sem saída para a Internet
- **THEN** login, tema, fontes, ícones e gerenciamento de VMs existentes continuam funcionais
