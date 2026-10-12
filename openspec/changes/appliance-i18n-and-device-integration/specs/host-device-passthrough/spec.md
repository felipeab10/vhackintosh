## Purpose

Faz com que dispositivos plugados no host — USB (armazenamento e periféricos),
e quando disponíveis câmera e adaptadores sem fio — apareçam e funcionem dentro
do macOS convidado, degradando com clareza quando o host não oferece o
dispositivo.

## ADDED Requirements

### Requirement: Dispositivos USB do host são visíveis no macOS
Com a VM em execução, dispositivos USB conectados ao host (HDs externos, pen
drives, periféricos) SHALL ser utilizáveis dentro do macOS: reconhecidos pelo
sistema e montáveis/visíveis para o usuário.

#### Scenario: HD externo plugado com a VM ligada
- **WHEN** o usuário conecta um HD externo USB com a VM do macOS em execução
- **THEN** o dispositivo aparece no macOS (Finder/Disco) e pode ser montado e
  acessado

#### Scenario: Pen drive plugado antes do boot
- **WHEN** um pen drive está conectado ao host antes de a VM iniciar
- **THEN** ele também está disponível dentro do macOS após o boot

### Requirement: Câmera e adaptadores sem fio passam quando o host os tem
Quando o host expõe câmera e/ou adaptadores Wi-Fi/Bluetooth utilizáveis como
dispositivos de passagem, o appliance SHALL permiti-los no convidado; quando o
host não os tem ou o driver não permite, SHALL iniciar a VM normalmente com uma
mensagem clara indicando o que não está disponível.

#### Scenario: Câmera presente
- **WHEN** o host tem uma câmera USB e o usuário a libera para a VM
- **THEN** a câmera aparece como dispositivo do macOS (ex.: usável pelo Photo
  Booth)

#### Scenario: Sem câmera/Wi-Fi no host
- **WHEN** o host não possui câmera nem Wi-Fi passável
- **THEN** a VM inicia normalmente e o appliance informa que esses dispositivos
  não estão disponíveis

### Requirement: A passagem não destabiliza o host
A ativação de passagem SHALL deixar a sessão do host e a TUI utilizáveis:
nenhum processo do appliance SHALL travar, e a remoção segura de dispositivo
SHALL ser tratada sem derrubar a VM.

#### Scenario: Desconexão de HD com a VM ligada
- **WHEN** o usuário remove um HD externo enquanto o macOS está usando
- **THEN** o macOS reage à remoção (aviso do sistema) e a VM e a TUI continuam
  de pé
