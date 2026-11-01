## Purpose

Manter o painel disponível durante a operação do appliance e tornar seu acesso descobrível no console, na ISO Live e no sistema instalado.

## ADDED Requirements

### Requirement: Inicialização independente do terminal
O serviço web e o supervisor de VMs SHALL iniciar no boot do appliance e permanecer disponíveis sem TUI aberta. Fechar o terminal, trocar de VT, encerrar uma sessão web ou reiniciar apenas o serviço HTTP SHALL NOT encerrar VMs. A recuperação de falhas SHALL possuir limites e diagnóstico local.

#### Scenario: Encerrar TUI com VM ativa
- **WHEN** o usuário sai da TUI após iniciar uma VM pelo painel
- **THEN** a VM continua ativa e o painel continua acessível

### Requirement: Endereço correto no script e no painel
O script/gerenciador SHALL mostrar estado real do serviço, protocolo, porta e endereços LAN utilizáveis, atualizar a informação após mudança de rede e distinguir sem IP, aguardando rede e serviço indisponível. Endereços de loopback, bridges privadas de VMs e interfaces inadequadas SHALL NOT ser anunciados como acesso LAN principal.

#### Scenario: DHCP ainda não concluiu
- **WHEN** o console abre antes de o host receber IP
- **THEN** mostra que aguarda endereço e passa a apresentar a URL correta quando a rede fica pronta

### Requirement: Compatibilidade entre Live e instalado
A ISO SHALL incluir frontend pronto, dependências e serviços habilitados. O sistema instalado SHALL preservar cadastro, credenciais, certificados e configuração conforme política documentada. Live SHALL informar dados voláteis e gerar segredos exclusivos da sessão. Uma instalação nova a partir de Live SHALL regenerar códigos temporários e identidade TLS quando necessário para não reutilizar segredos distribuídos ou copiados inadvertidamente.

#### Scenario: Boot de nova ISO
- **WHEN** uma ISO nova inicia com rede disponível
- **THEN** o painel chega ao fluxo de ativação sem downloads de dependências, compilação frontend ou necessidade de acesso à Internet

### Requirement: Sessão gráfica e áudio compartilhados de forma controlada
Partidas remotas SHALL usar a sessão gráfica/áudio válida do appliance ou um mecanismo supervisionado capaz de criá-la, sem exigir que o processo HTTP seja dono do display. Múltiplas VMs SHALL compartilhar uma sessão local quando suportado, sem tentar capturar o mesmo seat por compositores concorrentes. Ausência de saída gráfica suportada SHALL gerar diagnóstico antes da partida.

#### Scenario: Iniciar segunda VM remotamente
- **WHEN** uma VM já usa o compositor local e outra VM compatível é iniciada pelo painel
- **THEN** a segunda utiliza a sessão supervisionada existente sem derrubar a primeira ou iniciar outro compositor no mesmo seat

### Requirement: Atualização e falhas recuperáveis
Atualizações SHALL validar compatibilidade entre frontend, API, catálogo e runtime, preservar estado e permitir retorno ao conjunto anterior sem regravar discos macOS. Atualizações incompatíveis com uma VM em execução SHALL aguardar manutenção explícita. Falha do painel SHALL deixar diagnóstico no console e preservar os caminhos locais de recuperação.

#### Scenario: Atualização falha antes da ativação
- **WHEN** uma nova versão não passa na validação de serviço ou compatibilidade
- **THEN** o conjunto anterior permanece selecionado e as VMs e seus discos não são reprovisionados
