## Purpose

Garante que as teclas de brilho e de volume dos notebooks funcionem no Linux
appliance — aumentando e diminuindo o brilho da tela e o volume do áudio do
host — esteja a TUI aberta ou a VM em execução.

## ADDED Requirements

### Requirement: Teclas de brilho ajustam a tela do host
As teclas XF86 de brilho (aumentar/diminuir) SHALL alterar o brilho da tela do
computador host em qualquer ponto da sessão do appliance.

#### Scenario: Aumentar brilho
- **WHEN** o usuário pressiona a tecla de aumentar brilho do notebook
- **THEN** o brilho da tela do host aumenta de forma perceptível

#### Scenario: Diminuir brilho
- **WHEN** o usuário pressiona a tecla de diminuir brilho
- **THEN** o brilho da tela do host diminui

### Requirement: Teclas de volume ajustam o áudio do host
As teclas de volume (aumentar/diminuir/silenciar) SHALL alterar o volume do áudio
do host, inclusive enquanto a VM do macOS estiver em execução.

#### Scenario: Aumentar volume com a VM ligada
- **WHEN** a VM do macOS está em execução e o usuário pressiona aumentar volume
- **THEN** o volume do host sobe

#### Scenario: Silenciar
- **WHEN** o usuário pressiona a tecla de silenciar
- **THEN** o áudio do host é silenciado

### Requirement: Falta de hardware não quebra a sessão
Em máquinas sem controle de brilho suportado (ex.: desktop), as teclas SHALL
ser inofensivas e a sessão SHALL continuar normalmente.

#### Scenario: Desktop sem controle de brilho
- **WHEN** o usuário pressiona a tecla de brilho num host sem suporte
- **THEN** nada acontece (sem erro, sem travamento) e a TUI segue utilizável
