# Gate: sincronização opcional de energia do host

O comportamento do painel permanece estritamente `vm-only`: desligar ou
reiniciar um guest não pode desligar/reiniciar o Linux. A política antiga da
TUI é um caminho separado e não deve ser reutilizada implicitamente pelo
agente web.

Existe agora uma decisão pura de elegibilidade (`core/host_power_policy.py`)
para preparar uma futura opção `kiosk`. Ela recusa por padrão e só considera
um caso explicitamente pedido quando a instalação está confirmada, a sessão
kiosk é exclusiva, não há outra VM ativa e não há tarefa de armazenamento
ativa. Essa decisão não executa ações e não substitui uma reserva/lock.

## Bloqueio de integração

Não habilitar nem oferecer o opt-in ainda. O painel permite várias VMs
simultâneas, enquanto o serviço de sessão/seat atual não fornece ao agente um
broker persistente que possa provar exclusividade e manter uma reserva
atômica durante a janela entre o último recheck e a ação de energia. Uma
checagem apenas consultiva tem corrida: outra VM ou tarefa pode começar logo
depois e antes de `systemctl poweroff`.

Para remover o bloqueio, um broker do appliance precisa serializar a decisão
de energia com admissões de VM e tarefas de armazenamento, revalidar o estado
ao receber o evento, manter o guard até iniciar a ação, e oferecer somente
ações fechadas de host. O caminho de autostart também precisa interceptar o
evento de guest sem escapar para a política legada da TUI. Até então, a API,
UI e persistência web devem manter `vm-only` como único modo aplicável.

## Validação preparada

`tests/test_host_power_policy.py` cobre default seguro, fase de instalação,
exclusividade kiosk, segunda VM ativa, tarefa de armazenamento ativa/estado
desconhecido e elegibilidade do caso exclusivo. Isso valida somente a função
de decisão, não o broker nem desligamento real do host.
