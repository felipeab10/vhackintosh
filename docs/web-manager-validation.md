# Matriz de validação do painel web

Atualizada em 2026-10-31. Esta matriz separa evidência reproduzível em teste,
observação no host de desenvolvimento e validação ainda necessária no appliance.
Inventário de `--help` indica apenas o que o binário anuncia; não comprova que a
opção funciona com macOS, com o host ou com a release empacotada.

## Evidência disponível

| Área | Estado observado | Evidência e limite |
| --- | --- | --- |
| API, autorização e contratos | Validado por testes automatizados | A suíte completa passou com 941 testes, 4 ignorados e 336 subtestes; `api:types:check`, build do frontend, teste estático de acessibilidade (6/6), compilação Python, `git diff --check` e `openspec validate add-appliance-web-manager` passaram nesta retomada. Os testes foram executados com `TMPDIR` no volume de trabalho porque `/tmp` estava cheio. |
| Discos adicionais AHCI/NVMe | Editor e contratos preparados, runtime gated | O painel seleciona imagens existentes apenas em `vms/`, `media/` e `images/`, bloqueia mídia partilhada, mantém ordem estável e compara a projeção sem revelar caminhos absolutos. Testes cobrem seleção gerenciada, referência, inventário e compatibilidade legada. A extensão versionada foi integrada ao fork e publicada na release estável [Reims v1.5.0](https://github.com/felipeab10/reims-vgpu/releases/tag/v1.5.0). O pacote contém QEMU 11.1.50, `reims-ctl` e ROM construídos do mesmo checkout; `capabilities --json` anuncia `additional_disks`, e um smoke test QMP realizou discos principal AHCI e adicionais AHCI/NVMe. SHA-256 do pacote publicado: `8c1e5271ccb72913189c0113dcca9be2286e3afcbb3e787bbbbe5440431dc3ed`. O host de desenvolvimento ainda reporta v1.3.0; atualizar o bundle ativo e validar o boot de um guest com disco secundário continuam pendentes. |
| Inventário QEMU | Parcial | Consultas de ajuda isoladas do QEMU local 11.1.3 listaram opções e propriedades para até oito modelos permitidos. Cada sondagem respeita timeout agregado, saída limitada e cache por fingerprint. Tipos/defaults e validação no QEMU empacotado não foram comprovados. |
| Runtime Reims do host de desenvolvimento | Host ainda em v1.3.0; release estável v1.5.0 publicada | A v1.5.0 inclui o schema versionado `additional_disks` e o pacote anuncia essa capability. O host precisa instalar v1.5.0 em Configurações e iniciar a VM novamente para habilitar discos adicionais e VirtIO Sound; compatibilidade do áudio com macOS e boot do disco adicional ainda precisam de ensaio real. |
| VM `teste3` | Operacional no host de desenvolvimento | O usuário confirmou provisionamento, abertura da janela, métricas completas e estado coerente entre parada e execução. A tela de edição também foi inspecionada autenticada. Isso não substitui o ensaio de reinício/desconexão da API e do agente. |
| Recovery do Sequoia na `teste5` | Causa da opção ausente confirmada; novo boot pendente | O catálogo e o EFI da VM tinham `ShowPicker=true`, timeout de 45 s e `ScanPolicy=0`; o `BaseSystem.img` existe, contém volume HFS+ `macOS Base System` e `boot.efi`. Porém o `vm.toml` colocava `installer` na raiz em vez de `[q35].installer`, e o log de lançamento do Reims não continha o disco instalador. A projeção foi corrigida para o schema Reims e remove o campo legado na próxima inicialização; testes focados passaram. A tentativa anterior terminou também em core dump na biblioteca Vulkan NVIDIA (`wait_for_fences`), uma falha separada que ainda requer confirmação visual após o retry. |
| Concorrência de VMs | Duas VMs com 4 GiB cada já foram vistas ativas; correção do display compartilhado implementada, reteste físico pendente | A admissão mantém reservas independentes por VM e já confirmou dois processos QEMU ativos com 4 GiB por VM. Um teste posterior revelou que a segunda sessão Sway não conseguia assumir `/dev/dri/card0` (`Device or resource busy`, `Found 0 GPUs`). A correção usa um único serviço Sway, fora do cgroup de qualquer VM; cada janela Reims conecta-se ao mesmo socket Wayland e cria seu próprio contexto Vulkan na GPU compartilhada. Os serviços das VMs aguardam o socket, e a TUI inicia ou reutiliza esse compositor. Testes automatizados cobrem empacotamento, unidade e isolamento do fallback local. Ainda é necessário gerar ISO e confirmar duas VMs renderizando ao mesmo tempo no hardware real. |
| Telemetria NVIDIA | Já observada no host de desenvolvimento | A RTX 3050 reportou utilização, VRAM, temperatura e potência; `N/A` do driver permanece indisponível no painel. A coleta usa provedor compartilhado com timeout. |
| Alertas do host | Implementados; integração validada localmente | API autenticada, persistência deduplicada, reconhecimento/recuperação, limite de 500 eventos e retenção de 30 dias; a tela inclui estados vazio/erro/stale e consulta periódica. Na primeira inspeção o serviço de desenvolvimento carregava um processo anterior sem a nova rota (404); após reiniciar a unidade de desenvolvimento, a rota retornou 401 sem sessão e a tela autenticada exibiu o estado vazio correto. A instalação no appliance ainda precisa ser exercitada. |
| Console serial por VM | Implementado com fonte explícita do Reims | O source Reims v1.3.0 em `crates/reims-ctl/src/vm.rs` define `serial_log()` em `logs/serial.log`; `crates/reims-ctl/src/qemu.rs` passa `-serial file:...` quando `[logs].enabled` está ativo. O vHackintosh habilita essa opção na projeção `vm.toml`. A API lê apenas o arquivo de uma VM catalogada, com `O_NOFOLLOW`, máximo de 64 KiB e redação de credenciais/SMBIOS; testes cobrem rota autenticada, limite, redação e symlink. A saída serial real do guest e a versão/hash do bundle implantado ainda precisam de verificação no appliance (14.3/14.6). |
| Backup/restore com discos adicionais | Validado com `qemu-img` local; boot físico pendente | 21 testes cobrem disco principal e discos adicionais, manifesto/checksums, cadeia QCOW2 por disco, cancelamento, journal v1/v2, rollback após publicação parcial e falha do catálogo, symlink, duplicação, espaço e confinamento. O restore publica o conjunto gerenciado de forma recuperável. Boot de restore/clone/snapshot real segue como gate 10.7. |
| Falha parcial de inicialização | Validado por testes simulados; tentativa física inconclusiva | A tarefa de start agora espera até 30 s pelo inventário QEMU e supervisor saudável antes de concluir; retorno antecipado da unit é falha, e os diagnósticos expõem somente categorias allowlisted do journal. Retry é permitido apenas quando o runtime confirma QEMU ausente e a unit está falhada. O evento real de `teste4` não tem causa confirmável: a unit falhou em ~200 ms, mas o journal estava vazio. O `/tmp` cheio foi observado depois e é somente uma hipótese de ENOSPC. O comportamento físico com a nova implementação e os sockets/cgroups reais precisa de confirmação. |
| Pausa/retomada do QEMU empacotado | Smoke test QMP aprovado; controles web continuam bloqueados | O `qemu-system-x86_64` 11.1.50 do pacote Reims v1.5.0 respondeu `query-status(prelaunch) → cont → running → stop → paused → cont → running` e encerrou via `quit`. O teste não abriu uma VM pelo launcher Reims nem um guest macOS, por isso não habilita pause/resume na API ou na interface. |
| Topologia de CPU | Implementada no contrato local; runtime/release pendentes | O fork Reims tem commit local `de643f2a2954f2dd832f6897b818fbc00c7014c2` com `[q35.cpu_topology]` (`sockets`, `cores`, `threads`), valida produto igual a `cpus` e anuncia `cpu_topology`. No vHackintosh, API/UI só oferecem a opção quando a feature exata aparece no schema; omissão mantém `1×N×1` e valores legados são preservados. Testes de contrato, projeção, provisionamento e editor passaram na suíte completa. O commit ainda não está publicado em uma release nem instalado no host, e não houve boot macOS com topologia personalizada. |
| Layout, idioma e teclado | Parcial, com regressões locais automatizadas | A edição da VM foi verificada em 360, 375, 390, 768, 1024, 1440 px e 844×390 landscape sem rolagem horizontal. A página de host foi revista visualmente em 360, 390 e 768 px; os cards e alertas se reorganizam sem rolagem horizontal. O seletor PT-BR/English persiste em localStorage e atualiza o idioma do documento; todos os literais encontrados nas chamadas `translate()`/`t()` têm entrada inglesa. Dados variáveis de diagnóstico (nomes de VM/host, caminhos, PID, BDF, console e mensagens arbitrárias externas ao catálogo) são preservados sem tradução. O menu mobile fecha com Escape e devolve o foco ao botão; controles de histórico têm grupo nomeado e o foco recebe espaço acima da navegação inferior fixa. `npm run test:a11y` passou em nove verificações estáticas de idioma/viewport, cobertura do catálogo, skip link/landmark, foco, movimento reduzido, safe area e alvos touch; não simula layout renderizado. Host e Armazenamento quebram nomes longos dentro dos cards e limitam o seletor de arquivo à largura disponível. A sessão autenticada do navegador expirou; faltam revisar visualmente rotas protegidas, zoom, os fluxos completos e leitor de tela. |
| Bundle e latência local | Amostra local medida; baseline de appliance pendente | Build de produção: CSS principal 14,10 kB gzip; JS inicial 146,11 kB; editor da VM sob demanda 12,64 kB; gráficos uPlot 22,24 kB; CodeMirror 138,98 kB sob demanda. A divisão do editor manteve o chunk inicial abaixo do aviso de tamanho do Vite. Vinte requisições sequenciais locais deram p95 de 1,57 ms para `/` e 3,20 ms para `/api/v1/health`; amostra sem concorrência, sem tráfego SSE e sem guests, portanto não mede CPU/RSS nem substitui D13 ou o teste no appliance. |
| Bundle/rollback de código | Procedimento implementado e documentado | Bundles imutáveis, ponteiro trocado atomicamente, hashes/schema/runtime verificados e restauração do bundle anterior quando API/agente não retornam ativos. Consulte `docs/web-bundle-updates.md`. |
| Importação de VM legada ativa | Verificado por testes e inventário do Reims | A revisão lê `reims-ctl list --json`, mostra PID/estado e bloqueia importação enquanto ativa ou sem confirmação de parada. A consulta não envia comandos à VM nem altera os arquivos registrados. A migração completa de identidade/namespace continua condicionada ao procedimento offline 7.x. |
| Namespace de dados/runtime | Parcial, com defaults explícitos nas unidades do appliance | O agente e o supervisor recebem `VHACKINTOSH_CONFIG_DIR`, `VHACKINTOSH_REIMS_REGISTRY`, XDG e Cargo explicitamente; o serviço HTTP continua sem acesso ao HOME privilegiado e fala com o agente pelo socket local. O código rejeita overrides relativos e deriva cache de imagens/templates do diretório de estado. A resolução instalada do runtime tem testes simulados. Ainda falta validar leitura/execução com a identidade e permissões da ISO instalada, incluindo discos legados externos; nenhuma migração de dados foi executada neste host. |
| Migração/rollback do namespace legado | Testado em diretórios temporários; appliance pendente | `core/namespace_migration.py` oferece prévia somente leitura e cópia idempotente de `vms.json`, `settings.json` e `vms/*/vm.toml` para um destino novo. Opcionalmente aceita o registro Reims externo por caminho absoluto e o copia para `reims-ctl/vms.toml` sem reescrever os diretórios cadastrados. Antes de copiar, exige confirmação de serviços parados e inventário Reims vazio/conhecido; rejeita estado desconhecido/ativo, symlinks (inclusive quebrados), prévia obsoleta, metadata surgida durante a cópia e destino preexistente sem marcador. Faz backup de cada metadata no destino, verifica SHA-256, publica com `renameat2(RENAME_NOREPLACE)` e grava marcador; mantém as origens e não copia discos QCOW, EFI, NVRAM, imagens nem reescreve caminhos. Os testes adicionais simulam falha antes da publicação e verificam que a origem, os assets externos, SMBIOS, MAC, NVRAM e arquivos irmãos de HOME continuam intactos e que a metadata no staging é recuperável. Isso não valida ACLs específicas nem identidade/permissões do runtime instalado. A API/startup não chama esta operação; falta o ensaio controlado em appliance descartável, portanto 7.3/7.4/7.6 continuam parciais e nenhum dado real foi migrado. |
| Recuperação e primeiro acesso | Procedimentos documentados e restritos ao console local | `vhackintosh-web-admin setup` cria a primeira conta sem código no formulário e recusa SSH; `vhackintosh-web-recover` exige operador root local e revoga sessões sem tocar nos discos, catálogo ou TLS. Consulte `docs/web-manager-recovery.md`. |

## Mapeamento do editor para o runtime Reims

Este mapa descreve o que o writer do vHackintosh realmente projeta no
`vm.toml` que o `reims-ctl` consome. Valores em inventários QEMU são somente
anúncios até que o runtime os aplique e o ensaio correspondente passe.

| Opção visível | Campo/controle upstream observado | Lacuna concreta |
| --- | --- | --- |
| vCPU e RAM | `cpus`, `memory_mib`; schema novo `[q35.cpu_topology]` | O editor de sockets/cores/threads só aparece quando a feature exata é anunciada e valida o produto igual a `cpus`. O commit Reims que introduz o schema ainda não foi lançado/instalado; NUMA, afinidade e conjunto arbitrário de flags seguem sem editor, e `q35.cpu_flags` é preservado/default gerenciado. |
| Modelo de CPU | `q35.cpu_model` | Lista limitada a modelos descobertos no QEMU local; validação no bundle distribuído segue pendente. |
| Disco do sistema e dados adicionais | `disk`, `disk_format`, `q35.disk_port`; extensão versionada `additional_disks` | O painel edita discos adicionais AHCI/NVMe somente quando a release declara a feature; Reims v1.5.0 foi publicada e passou smoke test QMP de enumeração dos controladores, mas a atualização do host e o boot físico de um guest com disco adicional ainda são gates. Firmware e ordem de boot para vários discos do sistema continuam sem editor. |
| OpenCore e firmware | `q35.opencore`, `q35.opencore_format`, `q35.firmware_code`, `q35.firmware_vars` | Seleção do picker usa ação Reims separada; firmware e múltiplas entradas EFI não são editáveis. |
| Rede, MAC e SSH local | `network.enabled`, `network.mac`, `network.ssh_port`; user/NAT com SSH do host em `127.0.0.1:<porta>` para guest `:22` | SSH local está disponível quando o Reims anuncia `--ssh-port`; portas duplicadas entre VMs são recusadas e as portas são reservadas durante o start. O start testa o bind IPv4 de loopback e informa quando a porta já está ocupada; um processo externo ainda pode disputar a porta entre o preflight e o bind do QEMU. Encaminhamentos arbitrários e bridge/tap seguem indisponíveis; bridge/tap requer schema Reims e helper restrito. |
| Áudio | `audio.enabled`, `audio.backend` | O Reims fixa o dispositivo guest; VirtIO Sound é criado automaticamente somente pela release compatível, não há seletor do modelo. |
| Tela | `display.resolution`, `hidpi`, `borderless`, `refresh`, `count` | Quando `reims-ctl set --help` anuncia `--displays`, o editor permite de 1 a 8 saídas e escreve `display.count`; o Reims abre uma janela local por saída. O macOS pode não habilitar todos os displays em algumas versões, então o boot real continua necessário. |
| GPU virtual Reims | `[gpu].driver_file`, `device_id` e, quando suportado, identidade efetiva estruturada | O vHackintosh já exige correspondência exata de BDF/UUID com a capability anunciada e mostra configuração versus identidade efetiva; divergência ou ambiguidade falha fechada. Um checkout local do Reims adiciona inventário JSON e marcador de identidade, mas essa extensão não está publicada/instalada no runtime validado. A associação física e a seleção de duas GPUs equivalentes permanecem pendentes. |
| USB | Persistido em `VMConfig.usb_auto_attach`; anexo por QMP `usb-host` após start | Não existe campo de lista USB no `vm.toml`; configuração suporta VID:PID único e não oferece serial/persistência de caminho físico. |
| Argumentos QEMU | `extra_args` | O editor libera somente pares validados `-rtc base=utc` e `-msg timestamp=on`; argumentos de dispositivo, rede, armazenamento, identidade e isolamento seguem bloqueados. |
| Logs/serial | `logs.enabled`, `logs.level`; saída em `logs/serial.log` | A leitura web é um tail autenticado/redigido; ainda precisa ser validada com a VM real e o runtime do appliance. |
| PCI/VFIO e passthrough | Sem opção estruturada aplicada pelo launcher selecionado | Campo legado não é executado como passthrough; requer alteração do Reims, validação de grupos IOMMU e hardware dedicado. |

## Gate antes de declarar a entrega

### Política de energia do host (14.8)

O painel deve continuar em `vm-only`: desligar/reiniciar o macOS não pode
desligar nem reiniciar o Linux. A API rejeita `kiosk` no momento. O helper de
política é apenas um preflight testável; exige snapshots conhecidos de VMs,
jobs e tarefas de armazenamento, comprova que a VM atual está ativa e recusa
qualquer outro recurso ativo. Mesmo elegível, nenhuma ação de host pode ser
executada sem um guard atômico mantido do snapshot até a ação. Antes de expor
um opt-in kiosk, implementar esse guard compartilhado com admissão e submissão
de jobs, mostrar uma confirmação que diga claramente que o macOS poderá
reiniciar/desligar o host Linux, e testar em appliance descartável. A confirmação
deve ser por ação explícita do administrador; salvar a preferência não pode
disparar energia do host.

- Construir uma ISO nova e iniciar em Live e instalada; confirmar TLS/fingerprint,
  primeiro acesso, IP após DHCP, acesso por outro dispositivo da LAN, operação
  sem internet, persistência e recuperação.
- Com o bundle Reims que será distribuído, testar start, shutdown, pausa/retomada
  se habilitada, VirtIO Sound, GPU por identidade e troca de configuração após
  boot. Registrar tag e hashes exatos do bundle.
- Com VMs descartáveis e espaço reservado, testar criação, edição, duas VMs,
  reinício/desconexão dos serviços, backup/restauração/clone e recuperação de
  falha, sem usar discos de produção.
- Testar os caminhos VFIO e troca entre GPUs com grupos IOMMU diferentes,
  duas placas equivalentes e proteção da GPU que dirige o host. Caso falte
  hardware, registrar explicitamente o item como não verificado.
- Medir duração acima de dez minutos, acessibilidade nas demais páginas,
  zoom/reduced-motion e desempenho com dois guests e cinco clientes por dez
  minutos.
- Executar a migração offline somente em appliance descartável e com VMs
  paradas; verificar cópias, metadata, rollback, ACLs e discos externos antes
  de considerar concluídas as tarefas 7.x.

## Rollback

1. Para regressão no código do appliance, usar o mecanismo atômico descrito em
   `docs/web-bundle-updates.md`; manter o bundle anterior disponível.
2. Para recuperação de configuração/identidade da VM, usar backup/restauração e
   pontos de restauração offline pela interface, somente com a VM parada. O
   bundle verifica manifesto/checksums e publica com journal/rollback.
3. Não reverter uma migração de namespace apagando os diretórios de origem.
   Nenhuma migração deve ser ativada no host principal antes do ensaio offline
   que comprove cópia, identidade de SMBIOS/MAC, NVRAM, discos externos e
   recuperação integral.
