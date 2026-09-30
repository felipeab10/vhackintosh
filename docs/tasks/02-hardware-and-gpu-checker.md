# Tarefa T02: Core Hardware & GPU Checker

## Objetivo
Analisar os recursos de hardware do host Linux para sugerir a melhor alocação de vCPUs/RAM e checar a compatibilidade da GPU (Reims vGPU Vulkan ou Passthrough VFIO).

## Itens a Desenvolver
- [ ] Detecção de topologia de CPU: Cores físicos, P-Cores vs E-Cores (Intel híbrido 12th+ gen), threads totais.
- [ ] Recomendação inteligente de vCPU: Deixar folga para o host Linux (ex: num sistema de 16 threads, sugerir 12 vCPUs).
- [ ] Detecção de RAM: Total disponível, cálculo de RAM segura para a VM sem ativar `systemd-oomd`.
- [ ] Verificador de GPU:
  - Detecção de GPUs NVIDIA, AMD, Intel via `lspci`.
  - Checagem contra matriz de compatibilidade do Reims vGPU (Vulkan 1.2+).
  - Alertas sobre limitações e recomendações de flags (ex: X11 para NVIDIA).
- [ ] Helper de IOMMU e VFIO:
  - Leitura de `/sys/kernel/iommu_groups/`.
  - Identificação de IDs PCI para isolamento `vfio-pci`.
