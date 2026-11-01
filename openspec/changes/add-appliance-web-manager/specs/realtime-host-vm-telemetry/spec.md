## Purpose

Exibir consumo atual e histórico limitado do host Linux e de cada VM com atualização contínua, baixo custo e identificação correta da origem das métricas.

## ADDED Requirements

### Requirement: Telemetria do host
O painel SHALL apresentar CPU total e por processador lógico, carga, memória total/disponível/usada, swap, capacidade e atividade de armazenamento, tráfego por interface, uptime e sensores/GPU disponíveis. Cada amostra SHALL possuir horário, unidade, origem e estado de disponibilidade; coleta indisponível SHALL NOT ser substituída por zero.

#### Scenario: Host sem sensor térmico acessível
- **WHEN** o provedor não consegue obter temperatura
- **THEN** a interface mostra indisponibilidade para temperatura e mantém CPU, memória e demais séries válidas

### Requirement: Telemetria por VM com semântica explícita
Cada VM ativa SHALL apresentar uptime, estado, vCPUs e RAM configuradas, tempo de CPU, consumo residente do processo/grupo de execução, I/O e rede quando houver fonte atribuível. CPU SHALL informar a base percentual utilizada; RAM residente SHALL ser distinguida de uso interno do macOS. Dados do convidado SHALL exigir fonte real disponível e não serão inferidos de RAM alocada ou RSS.

#### Scenario: VM sem agente do convidado
- **WHEN** a VM está ativa sem integração de métricas internas do macOS
- **THEN** o painel mostra recursos configurados e consumo externo medido, marca uso interno como indisponível e não apresenta RAM alocada como RAM usada pelo macOS

#### Scenario: Rede NAT sem contador por VM
- **WHEN** o backend de rede não oferece contador atribuível à VM
- **THEN** a série de rede daquela VM aparece indisponível e os contadores globais do host não são atribuídos a ela

### Requirement: Atualização contínua e reconexão
Com o painel visível e a conexão disponível, amostras atuais SHALL ser publicadas em intervalo padrão de aproximadamente um segundo. A interface SHALL informar perda de conexão e dados atrasados, recompor estado ao reconectar e manter lacunas históricas. O trabalho de coleta por fonte SHALL ser compartilhado entre clientes e possuir filas limitadas.

#### Scenario: Vários navegadores conectados
- **WHEN** cinco clientes acompanham o mesmo host e duas VMs
- **THEN** todos recebem as amostras do mesmo ciclo de coleta, sem cinco sondagens separadas dos mesmos processos ou dispositivos

#### Scenario: Stream interrompido
- **WHEN** o navegador perde conexão
- **THEN** os gráficos indicam atraso, não mantêm a aparência de medição ao vivo e retomam com horário e lacuna identificados

### Requirement: Histórico limitado e custo controlado
O painel SHALL oferecer janelas de cinco minutos, uma hora e 24 horas com redução de resolução, retenção e tamanho de armazenamento limitados e configuração de menor frequência. Reinício do serviço HTTP SHALL preservar o histórico persistido; modo Live SHALL indicar a volatilidade dos dados. A validação SHALL medir custo de CPU, memória e tráfego com e sem clientes.

#### Scenario: Retenção alcançada
- **WHEN** as séries excedem o período ou limite de armazenamento configurado
- **THEN** dados antigos são agregados ou removidos sem crescimento indefinido ou interrupção das métricas atuais
