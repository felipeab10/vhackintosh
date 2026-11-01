## Purpose

Selecionar a placa física usada pela aceleração Reims ou pelo passthrough de uma VM macOS, evitando escolhas ambíguas e conflitos com o host.

## ADDED Requirements

### Requirement: Inventário gráfico por dispositivo
O painel SHALL listar placas Intel, AMD e NVIDIA detectadas com nome, endereço PCI, identificadores disponíveis, driver/ICD, capacidade Vulkan verificada, vínculo com display do host e uso por VMs. Duas placas do mesmo modelo SHALL aparecer como dispositivos distintos quando detectáveis; ausência de sensor ou informação SHALL ser indicada.

#### Scenario: Host com duas GPUs iguais
- **WHEN** duas placas possuem o mesmo fabricante e modelo
- **THEN** o inventário permite distingui-las pelo endereço PCI e identificador estável disponível, sem uni-las por nome ou driver

### Requirement: Seleção física exata para Reims
A seleção explícita SHALL resolver a placa física, persistir por VM e ser aplicada ao runtime. O sistema SHALL verificar a identidade efetiva quando o runtime fornecer evidência e SHALL NOT marcar seleção como confirmada com base apenas em nome ou arquivo ICD. Sem mecanismo para distinguir as placas candidatas, a partida com seleção exata SHALL ser recusada com diagnóstico.

#### Scenario: Mesmo driver atende duas placas
- **WHEN** o administrador seleciona uma entre duas GPUs atendidas pelo mesmo ICD
- **THEN** a seleção identifica uma única placa e o runtime a utiliza, ou o sistema bloqueia a partida informando que a versão atual não suporta seleção inequívoca

#### Scenario: GPU selecionada removida
- **WHEN** a GPU salva deixa de estar disponível
- **THEN** o painel mantém a escolha e bloqueia a partida, sem substituir silenciosamente por outra GPU

### Requirement: Troca de GPU exige parada completa
O painel SHALL permitir alterar a placa de uma VM desligada e validar Vulkan, permissões, sessão de exibição e compatibilidade antes da próxima partida. SHALL mostrar GPU configurada, GPU efetiva conhecida e diagnóstico. Troca a quente entre placas físicas SHALL NOT ser oferecida.

#### Scenario: Alteração com a VM ligada
- **WHEN** o administrador escolhe outra GPU durante a execução
- **THEN** a interface exige desligamento completo para aplicar a troca e não migra o contexto gráfico ativo

### Requirement: Reims compartilhável e VFIO exclusivo
O painel SHALL distinguir renderização Reims no driver do host de atribuição VFIO exclusiva, validar grupos IOMMU e funções dependentes e impedir captura de GPU necessária ao console/host sem uma alternativa explicitamente configurada. Compartilhamento Reims SHALL depender de recursos e capacidade real, sem garantir divisão fixa de VRAM ou desempenho.

#### Scenario: VFIO já ocupado
- **WHEN** uma segunda VM solicita uma GPU ou grupo IOMMU reservado por outra VM
- **THEN** a partida é negada com identificação do conflito

#### Scenario: Duas VMs compartilham renderização Vulkan
- **WHEN** duas VMs usam uma GPU Reims compatível e há recursos suficientes
- **THEN** o sistema permite a execução e identifica o compartilhamento sem apresentar a GPU como VFIO exclusiva
