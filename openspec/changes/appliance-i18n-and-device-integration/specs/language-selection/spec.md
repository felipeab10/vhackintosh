## Purpose

Permite que qualquer usuário entenda e use o appliance no idioma que escolhe —
Inglês ou Português do Brasil — desde a primeira tela da instalação, com a
escolha valendo para toda a interface e persistindo depois de instalado.

## ADDED Requirements

### Requirement: A primeira tela da instalação escolhe o idioma
Antes de qualquer outro passo da instalação do Linux appliance, o sistema SHALL
exibir uma tela com exatamente as opções Inglês (en-US) e Português do Brasil
(pt-BR). Nenhuma outra tela ou prompt SHALL aparecer antes dessa escolha.

#### Scenario: Boot limpo mostra só o idioma
- **WHEN** o appliance inicia para instalar (primeiro boot da ISO/live)
- **THEN** a primeira tela mostra apenas a escolha entre Inglês e Português do
  Brasil, sem exibir texto de outras telas antes

#### Scenario: Escolha seguida pela interface traduzida
- **WHEN** o usuário seleciona um idioma e confirma
- **THEN** todo texto subsequente da instalação é exibido no idioma escolhido

### Requirement: A interface inteira segue o idioma escolhido
Todas as telas da instalação e a TUI do appliance SHALL ser exibidas
integralmente no idioma escolhido (nenhum texto fixo em outro idioma), uma vez
que a escolha foi feita.

#### Scenario: TUI pós-instalação no idioma escolhido
- **WHEN** o usuário escolheu Português do Brasil na instalação e depois abre a
  TUI do appliance
- **THEN** toda a TUI aparece em português do Brasil

#### Scenario: Inglês traduz tudo
- **WHEN** o usuário escolhe Inglês e completa a instalação
- **THEN** instalação e TUI aparecem integralmente em inglês

### Requirement: O idioma persiste após a instalação
A escolha de idioma SHALL persistir após a instalação do Linux appliance, sem
que o usuário precise escolher novamente em boots seguintes.

#### Scenario: Reboot mantém o idioma
- **WHEN** o appliance já está instalado e o usuário reinicia o computador
- **THEN** a TUI abre no idioma escolhido na instalação, sem nova tela de idioma

### Requirement: Troca de idioma por script depois de instalado
Após a instalação SHALL existir um script de linha de comando que troca o idioma
do appliance; a troca SHALL valer imediatamente para a próxima execução da TUI e
SHALL persistir em reboots.

#### Scenario: Trocar para inglês via script
- **WHEN** o usuário roda o script de troca de idioma escolhendo Inglês
- **THEN** a próxima abertura da TUI aparece em inglês e a mudança sobrevive a
  um reboot

#### Scenario: Idioma inválido é recusado
- **WHEN** o usuário passa ao script um idioma não suportado (ex.: `fr`)
- **THEN** o script recusa a entrada, mantém o idioma atual e informa os idiomas
  suportados
