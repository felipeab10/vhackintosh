# Tarefa T06: UI TUI & Boot Interceptor

## Objetivo
Criar uma interface moderna em terminal usando `rich`, com banner/logo personalizado, painel detalhado de gerenciamento de VMs e contagem regressiva de 5 segundos no boot.

## Itens a Desenvolver
- [ ] Banner e Logo ASCII/ANSI estilizado do `vHackintosh`.
- [ ] Boot Interceptor (5 segundos):
  - Contagem regressiva visual no boot do appliance.
  - Listener não-bloqueante para a tecla `ESPAÇO`.
  - Se `ESPAÇO` for pressionado -> Abre o Gerenciador de VMs.
  - Se o tempo expirar -> Inicia a VM marcada com auto-start.
- [ ] Painel Interativo de Gerenciamento de VMs:
  - Lista de VMs com status e ícones de versão.
  - Ao selecionar uma VM: Exibir card completo com hardware alocado, disco, serial SMBIOS e opções de ação (Iniciar, Editar, Deletar, Definir Auto-Start).
  - Wizard guiado passo a passo para criar novas VMs.
