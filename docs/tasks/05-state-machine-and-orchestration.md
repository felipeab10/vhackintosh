# Tarefa T05: State Machine & Orquestrador de Instalação

## Objetivo
Acompanhar o ciclo de vida e múltiplos reboots durante a instalação do macOS, identificando o estado exato da VM e selecionando automaticamente a partição correta no OpenCore.

## Itens a Desenvolver
- [ ] Monitor de eventos QMP e porta serial:
  - Estados rastreados:
    1. `INICIANDO`: Boot inicial do OpenCore.
    2. `EM_INSTALACAO`: Execução do instalador Apple Recovery / BaseSystem.
    3. `REINICIANDO_POS_INSTALACAO`: Apple reboot durante o estágio de cópia.
    4. `INSTALACAO_CONCLUIDA`: Instalação finalizada com sucesso.
    5. `INICIANDO_MACOS`: Inicialização normal do sistema instalado.
    6. `ERRO_AO_INICIAR`: Detecção de Kernel Panic ou falha de inicialização.
- [ ] Auto-Boot no OpenCore:
  - Ajuste de parâmetros no OpenCore (`ShowPicker=false`, `PollAppleHotKeys=true`) ou envio de comandos QMP para navegação automática no picker.
  - Prevenção de shutdown prematuro durante os reboots intermediários.
