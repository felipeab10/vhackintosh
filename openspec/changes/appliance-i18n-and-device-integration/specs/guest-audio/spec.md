## Purpose

Faz o som do macOS convidado chegar aos alto-falantes do host, com o appliance
detectando e avisando quando o host não tem backend de áudio pronto.

## ADDED Requirements

### Requirement: Áudio do convidado sai pelo host
Com a VM em execução, som produzido no macOS (vídeo, música, alertas) SHALL ser
reproduzido pelos alto-falantes/saída de áudio do host, sem atraso perceptível
que impeça o uso (vídeo e chamadas).

#### Scenario: Reproduzir vídeo com som
- **WHEN** o usuário reproduz um vídeo com áudio dentro do macOS
- **THEN** o som é ouvido na saída de áudio do host

#### Scenario: Alertas do sistema
- **WHEN** o macOS toca um som de alerta (ex.: erro de tecla)
- **THEN** o som é ouvido no host

### Requirement: O appliance detecta a falta de áudio do host
Antes/durante a execução, o appliance SHALL checar se o host tem backend de
áudio funcional; se não tiver, SHALL reportar de forma acionável (o que instalar
ou habilitar), sem impedir o restante do uso da VM.

#### Scenario: Host sem serviço de áudio
- **WHEN** o host não tem serviço de áudio (PipeWire/PulseAudio) em execução
- **THEN** a checagem de prontidão reporta o problema com instrução clara, e o
  usuário pode seguir com a VM (áudio sem som)

#### Scenario: Áudio ligado e funcionando
- **WHEN** o host tem áudio funcional
- **THEN** a checagem aprova e o som do convidado é ouvido
