## 1. Empacotamento e clone inicial do reims-vgpu

- [x] 1.1 Remover do build-iso download de releases e cópia de binários locais; empacotar somente fontes/dependências para compilação no Linux appliance.
- [x] 1.2 Implementar clone inicial do fork reims-vgpu em diretório temporário, verificando remote e branch antes de promover atomicamente para `/opt/reims-vgpu` (testado com bare remote local; smoke real ainda pendente).
- [x] 1.3 Definir e testar a branch de atualização padrão para ambos os remotes; rejeitar URLs/branches inesperadas e checkout sem metadados Git válido.
- [ ] 1.4 (Não implementado deliberadamente: o requisito vigente do appliance é updater root-only, sem `sudo`.)

## 2. Atualizador Git seguro dos dois projetos

- [x] 2.1 Refatorar updater para identificar `/opt/vhackintosh` e `/opt/reims-vgpu`, verificar origem Git esperada, branch, working tree limpa e commits locais antes de modificar.
- [x] 2.2 Implementar fetch e preflight fast-forward para os dois repositórios antes de aplicar updates; não fazer reset, stash nem merge automático em divergência.
- [x] 2.3 Aplicar fast-forward e devolver estado por repositório (commit anterior/novo, já atualizado, atualizado, falha); tratar e comunicar execução parcial e permitir retry idempotente.
- [x] 2.4 Remover do caminho de atualização da TUI a consulta `/releases/latest`, download/extract tar.gz e escrita de `.version` como se fosse build de fonte.

## 3. Rebuild completo e integração com a TUI

- [x] 3.1 Após sync bem-sucedido, executar o rebuild integral existente para reims-vgpu, ROM, QEMU e reims-ctl através do caminho de build local, usando explicitamente o checkout sincronizado em `/opt/reims-vgpu`.
- [x] 3.2 Diferir a troca do symlink QEMU `current` até após compilar reims-ctl; não gravar stamp do fingerprint em falha. A validação final em hardware real permanece pendente.
- [x] 3.3 Atualizar a opção 4 da TUI e mensagens PT/EN para explicar sync Git + compilação, progresso por etapa, sucesso composto, falhas e necessidade de reabrir/reiniciar para carregar o código Python atualizado.
- [x] 3.4 Não anunciar sucesso global se um dos repositórios ou qualquer etapa do rebuild falhar; exibir hashes/etapa e instrução de retry/recuperação.

## 4. Testes e verificação do fluxo

- [x] 4.1 Adicionar testes de integração Git para repos limpos/atualizados, fast-forward, checkout sujo, branch adiantada/divergente, remote, falha parcial e falha de clone.
- [x] 4.2 Testar clone inicial sem `.git` em caminho temporário equivalente a `/opt`, preservação da árvore em falha de rede/submódulo ausente, recusa segura de backup preexistente e promoção após validação.
- [x] 4.3 Testar que falha de etapa não altera o stamp e que a troca do `current` ocorre somente depois de compilar/validar reims-ctl.
- [ ] 4.4 Smoke manual depende de appliance físico conectado: atualizar ambos os repos, verificar commits, rebuild e iniciar uma VM; repetir sem commits para verificar idempotência.
- [x] 4.5 Validar catálogo PT/EN e `openspec validate appliance-repository-self-update`.
