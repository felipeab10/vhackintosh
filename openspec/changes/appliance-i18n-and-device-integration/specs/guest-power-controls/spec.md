## Purpose

Define como os controles de Reboot e Shutdown oferecidos após a instalação do
macOS se comportam: a escolha afeta apenas o mapeamento de energia entre o
convidado e o host, e nunca impede o OpenCore de encontrar o disco onde o
macOS foi instalado.

## ADDED Requirements

### Requirement: O prompt de controles aparece após a instalação do macOS
Depois de o macOS estar instalado no disco da VM, o appliance SHALL perguntar se
os controles de Reboot e Shutdown (reiniciar/desligar vindo do macOS) devem
mapear para o computador host; as duas opções (com ou sem esse mapeamento) SHALL
ser igualmente válidas.

#### Scenario: Prompt exibido no pós-instalação
- **WHEN** a instalação do macOS termina e o usuário inicia a VM do macOS já
  instalada pela primeira vez
- **THEN** o appliance apresenta a escolha dos controles de Reboot/Shutdown

### Requirement: A escolha nunca quebra o boot do disco instalado
Independentemente da opção escolhida (mapeamento ligado ou desligado), iniciar a
VM SHALL resultar no OpenCore encontrando e oferecendo o disco com o macOS
instalado, e o macOS SHALL iniciar normalmente. A escolha SHALL alterar apenas o
que reboot/desligar do convidado faz com o host.

#### Scenario: Mapeamento ligado mantém o boot
- **WHEN** o usuário escolhe ter os controles de Reboot/Shutdown mapeados para o
  host e depois inicia a VM
- **THEN** o OpenCore mostra o disco do macOS instalado e o sistema inicia

#### Scenario: Mapeamento desligado mantém o boot
- **WHEN** o usuário escolhe NÃO ter o mapeamento e depois inicia a VM
- **THEN** o OpenCore mostra o disco do macOS instalado e o sistema inicia

#### Scenario: Trocar a escolha depois não quebra o boot
- **WHEN** o usuário altera a preferência de controles para a outra opção e
  inicia a VM de novo
- **THEN** o OpenCore continua encontrando o disco instalado

### Requirement: O mapeamento age conforme a escolha
Com o mapeamento ativo, reiniciar ou desligar vindo do macOS SHALL reiniciar ou
desligar o computador host; com o mapeamento inativo, essas ações SHALL afetar
apenas a VM (o host permanece ligado e a VM pode ser religada pelo appliance).

#### Scenario: Reboot com mapeamento ativo
- **WHEN** o mapeamento está ativo e o usuário escolhe Reiniciar dentro do macOS
- **THEN** o host reinicia (a VM termina junto)

#### Scenario: Shutdown com mapeamento inativo
- **WHEN** o mapeamento está inativo e o usuário escolhe Desligar dentro do macOS
- **THEN** a VM encerra e o host permanece ligado
