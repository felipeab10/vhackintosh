## Purpose

Administrar VMs macOS pelo navegador e pelo terminal sobre um catálogo consistente, com execução concorrente controlada e operações acompanháveis.

## ADDED Requirements

### Requirement: Catálogo comum e alterações concorrentes
O painel e a TUI/CLI SHALL mostrar as mesmas VMs e configurações persistidas. Alterações SHALL possuir revisão, rejeitar gravações obsoletas, preservar identidades, campos avançados e caminhos importados válidos e permitir importar VMs registradas no runtime mediante revisão prévia.

#### Scenario: Dois clientes editam a mesma VM
- **WHEN** um cliente tenta salvar sobre uma revisão que outro cliente já alterou
- **THEN** o sistema retorna conflito e oferece recarregar ou revisar diferenças sem sobrescrever silenciosamente a alteração anterior

#### Scenario: VM legada ativa no Reims
- **WHEN** o administrador revisa uma VM registrada no Reims que ainda não consta no catálogo vHackintosh
- **THEN** a revisão consulta o inventário somente de leitura, identifica a VM como ativa e mostra o PID; a importação fica bloqueada até o runtime confirmar que ela está parada, sem enviar comandos à VM nem alterar seus arquivos

#### Scenario: Runtime Reims não pode confirmar estado
- **WHEN** a consulta do runtime falha ou retorna dados inválidos durante a revisão/importação de uma VM legada
- **THEN** o painel mostra estado desconhecido e bloqueia a importação, sem inferir que a VM está parada

### Requirement: Assistente com paridade ao gerenciador atual
O painel SHALL permitir escolher nome, versão macOS oferecida pelo catálogo existente, imagem local ou download Apple, CPU, RAM, disco, SMBIOS/OpenCore, GPU, áudio, exibição, autostart, USB e fase de instalação. SHALL apresentar sugestões de hardware, validações prévias e progresso de download, conversão e provisionamento, sem requerer terminal interativo.

#### Scenario: Criar e instalar pelo navegador
- **WHEN** o administrador conclui um assistente válido com download Apple
- **THEN** uma tarefa provisiona mídia, disco e EFI próprios, registra a VM e permite iniciar a instalação, expondo etapas e falhas recuperáveis

### Requirement: Controle de ciclo de vida por VM
O painel SHALL oferecer iniciar, solicitar desligamento, forçar parada, reiniciar e, quando o runtime suportar com segurança, pausar/retomar. Estado do processo, disponibilidade de controle e fase de instalação SHALL ser distintos. Desligamento normal SHALL NOT virar parada forçada automaticamente após timeout. Mudanças incompatíveis com execução SHALL ser recusadas ou salvas explicitamente para a próxima partida.

#### Scenario: Desligamento normal expira
- **WHEN** o convidado não responde à solicitação de desligamento dentro do prazo
- **THEN** o painel informa o timeout, mantém o estado real da VM e oferece confirmação separada para forçar a parada

#### Scenario: Processo vivo sem QMP
- **WHEN** o processo da VM permanece vivo, mas o canal de controle está indisponível
- **THEN** o painel mantém o estado do processo como em execução, informa que o controle QMP está indisponível e não afirma que a VM está desligada

### Requirement: Admissão concorrente e exclusividade de dispositivos
O sistema SHALL permitir várias VMs simultâneas quando houver recursos e compatibilidade, reservar recursos atomicamente entre todos os clientes, impedir duas partidas da mesma VM e negar conflitos de disco gravável, VFIO, USB exclusivo, portas ou sessão gráfica indisponível. A reserva SHALL considerar VMs iniciadas antes da abertura do painel e manter margem para o host.

#### Scenario: Partidas simultâneas pelo terminal e navegador
- **WHEN** a TUI e o painel solicitam ao mesmo tempo a partida da mesma VM
- **THEN** apenas uma execução é criada e o outro cliente recebe o estado ou operação já existente

#### Scenario: Recurso insuficiente
- **WHEN** uma nova VM excederia a memória disponível com a margem do host ou usaria um dispositivo exclusivo ocupado
- **THEN** a partida é recusada com os recursos conflitantes e sem deixar uma reserva permanente

### Requirement: Energia isolada e reboots de instalação
Por padrão, desligamento/reboot do convidado e controles da VM SHALL afetar apenas a VM. O sistema SHALL preservar instalação multiestágio e relançamento supervisionado do processo quando exigido pelo Reims, sem presumir instalação concluída pelo número de reboots. Sincronização com energia do host SHALL ser opt-in, depender de instalação confirmada e modo kiosk exclusivo, e ser bloqueada quando houver outra VM ou tarefa de armazenamento ativa.

#### Scenario: Reiniciar uma VM com outra em execução
- **WHEN** o administrador reinicia a VM A enquanto a VM B está ativa
- **THEN** o host e a VM B permanecem ligados e somente A realiza seu ciclo de reinício

#### Scenario: Reboot intermediário do instalador
- **WHEN** o instalador macOS reinicia durante uma fase não concluída
- **THEN** o supervisor continua a instalação com os mesmos discos, EFI, identidade e mídia adequada, sem reiniciar o host

### Requirement: Tarefas duráveis e observabilidade de operações
Operações longas SHALL retornar um identificador e publicar progresso, fase, logs limitados, resultado e possibilidade de cancelamento seguro. Fechar o navegador, perder a rede ou reiniciar o serviço HTTP SHALL NOT encerrar VMs ou cancelar tarefas automaticamente. Após reinício do supervisor, tarefas interrompidas SHALL ser reconciliadas antes de nova execução.

#### Scenario: Reconectar após perda de rede
- **WHEN** o administrador reconecta depois de iniciar um provisionamento
- **THEN** encontra a mesma tarefa e o progresso atual, sem criar outro download ou disco

### Requirement: Logs e acesso ao console compatível
O painel SHALL fornecer logs de runtime, serial disponível e diagnósticos de boot, além de ação para visualizar a VM no display local. SHALL distinguir console serial de desktop gráfico remoto e informar a indisponibilidade deste quando o backend Reims não oferecer captura compatível.

#### Scenario: Backend Reims sem console gráfico web
- **WHEN** o administrador abre a área de console de uma VM nesse backend
- **THEN** encontra logs/serial disponíveis e orientação sobre o display local, sem uma imagem preta apresentada como sessão gráfica funcional
