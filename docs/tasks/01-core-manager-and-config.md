# Tarefa T01: Core Manager & Config

## Objetivo
Implementar a estrutura de persistência e gerenciamento de múltiplas VMs macOS, garantindo execução exclusiva (uma VM por vez) e controle de parâmetros.

## Itens a Desenvolver
- [ ] Modelo de dados de VM em Python (`VMConfig`):
  - Nome, versão do macOS (Tahoe, Sequoia, Sonoma, etc.), vCPUs, RAM, tamanho do disco, seriais SMBIOS, status de auto-start, caminho do disco virtual e porta QMP.
- [ ] Gerenciador de persistência JSON (`~/.config/vhackintosh/vms.json`).
- [ ] Validador de Lock Exclusivo: Garante que apenas 1 instância de VM esteja em execução.
- [ ] CRUD completo (Criar VM, Listar VMs, Obter detalhes, Modificar configurações, Deletar VM).
- [ ] Controle de flag `auto_start` (apenas uma VM pode ter auto-start ativo por vez).
