## Purpose

Configurar o hardware virtual e opções avançadas do QEMU instalado com previsibilidade, compatibilidade macOS e persistência entre ciclos de execução.

## ADDED Requirements

### Requirement: Configuração visual organizada por hardware
O painel SHALL oferecer seções para máquina/aceleração, CPU/modelo/flags/topologia/afinidade, memória/NUMA/backends disponíveis, discos/controladores/boot, firmware/NVRAM/OpenCore/SMBIOS, rede/MAC/portas, áudio, exibição, PCI/USB e política de execução, indicando valores herdados, capacidades disponíveis e necessidade de desligamento. O perfil macOS seguro SHALL continuar disponível.

#### Scenario: Editar hardware com a VM desligada
- **WHEN** o administrador altera CPU, RAM, rede e ordem de boot para uma combinação suportada
- **THEN** a prévia mostra os valores efetivos e a próxima partida utiliza a configuração salva

### Requirement: Opções avançadas sem execução de shell
O painel SHALL fornecer editor de argumentos QEMU como sequência de tokens e propriedades estruturadas adicionais, permitindo expressar opções do binário instalado não cobertas por formulários. SHALL preservar ordem e limites entre tokens e SHALL detectar conflitos com parâmetros gerenciados. Argumentos SHALL NOT substituir canais de controle, PID, isolamento, política de energia ou caminhos protegidos do serviço.

#### Scenario: Opção rara válida
- **WHEN** o administrador adiciona uma opção avançada suportada que não conflita com o gerenciamento
- **THEN** ela aparece na invocação efetiva e permanece idêntica após salvar, reiniciar serviços e iniciar novamente a VM

#### Scenario: Override de um canal gerenciado
- **WHEN** argumentos avançados tentam substituir QMP, pidfile ou executar um hook privilegiado
- **THEN** o sistema recusa a configuração e identifica o parâmetro protegido e o caminho suportado para a intenção correspondente

### Requirement: Capacidades do runtime efetivamente selecionado
O sistema SHALL vincular a lista de capacidades à versão e identidade do QEMU/Reims selecionado, distinguir disponível, incompatível com macOS, desconhecido e indisponível e revalidar configurações após troca de runtime. SHALL NOT prometer suporte por existir uma opção na documentação de outra versão.

#### Scenario: Runtime perde um dispositivo
- **WHEN** a versão selecionada do runtime deixa de oferecer um dispositivo configurado
- **THEN** a configuração é preservada, a incompatibilidade aparece e a partida é bloqueada até correção ou escolha de runtime compatível

### Requirement: Prévia validada e configuração sem perdas
Antes de salvar e iniciar, o sistema SHALL validar referências, conflitos, portas, recursos e opções conhecidas, mostrar diferenças e invocação efetiva com segredos mascarados e distinguir validação sintática de compatibilidade realmente testada. Valores avançados e MAC/EFI/identidade SHALL sobreviver à regeneração da configuração do runtime e às transições de instalação.

#### Scenario: Instalação remove a mídia temporária
- **WHEN** a instalação é confirmada e a mídia de recuperação é removida da configuração
- **THEN** as opções avançadas, GPU, MAC, firmware, NVRAM e identidade permanecem preservadas

### Requirement: Configuração pendente e configuração em execução
O sistema SHALL mostrar separadamente configuração salva e efetiva do processo atual, aplicando alterações a quente somente quando a capacidade estiver explicitamente suportada e confirmada. Mudanças de máquina, firmware, CPU ou GPU SHALL exigir parada completa.

#### Scenario: Troca de CPU durante execução
- **WHEN** o administrador salva um novo modelo de CPU para uma VM ligada
- **THEN** o painel informa que a mudança está pendente e mantém o processo atual com sua configuração original até a próxima partida
