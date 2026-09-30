# Tarefa T07: Appliance Kiosk Mode & Power Sync

## Objetivo
Configurar a execução da VM em modo Kiosk exclusivo (fullscreen 100%, captura de mouse/teclado sem escape) e sincronizar o estado de energia do macOS com o computador Host (Desligar/Reiniciar).

## Itens a Desenvolver
- [ ] Modo Kiosk Fullscreen:
  - Janela `reims-vgpu` em tela cheia na resolução nativa do monitor.
  - Captura exclusiva de teclado (`x11_grab_keyboard`) e mouse sem bordas.
- [ ] Sincronização de Energia Host:
  - Captura do evento QMP `POWERDOWN` / `SHUTDOWN` -> Executa `systemctl poweroff` no host.
  - Captura do evento QMP `RESET` durante execução normal -> Executa `systemctl reboot` no host.
  - Distinção inteligente entre reboots de instalação interna e comandos de energia intencionais do usuário.
