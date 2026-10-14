## Context

Ver `proposal.md` para a motivação. A opção `4` da TUI chama `SystemUpdater.run_full_update()`: hoje ela executa `git pull --ff-only` somente no checkout do vHackintosh e, separadamente, `update_reims_vgpu()` consulta `/releases/latest` e extrai assets binários. O build local já existe em `core/reims_build.py`: para a árvore nova, `build()` chama `scripts/install-ctl.sh --yes`, que constrói biblioteca/ROM, QEMU e reims-ctl, e só grava o fingerprint de fonte após comprovar o artefato esperado.

A ISO entrega as fontes em `/opt/reims-vgpu` e o aplicativo em `/opt/vhackintosh`; não entrega `.git` do reims-vgpu, portanto seu pacote não permite `git pull` no estado atual. A appliance também pode localizar uma cópia em `~/reims-vgpu` durante desenvolvimento. A atualização precisa garantir que os clones necessários existam e estejam configurados para os remotes corretos. O updater é código PT/EN via catálogo i18n e a opção atual é exposta na TUI.

## Goals / Non-Goals

**Goals:**
- Atualizar ambos os repositórios fonte corretos no appliance instalado.
- Compilar localmente o reims-vgpu completo e instalar o conjunto resultante pelo fluxo existente.
- Detectar estado local inseguro e relatar resultados parciais/falhas sem sucesso falso.
- Reaproveitar o build por fingerprint e as mensagens/progresso da TUI quando possível.

**Non-Goals:**
- Publicar releases, mudar o processo de geração da ISO ou atualizar firmware/BIOS fora do repositório.
- Fazer merge automático de branches divergentes, resetar arquivos modificados, ou suportar escolha arbitrária de branch/commit pelo usuário nesta mudança.
- Recompilar o app Python em binário; a atualização do repo vHackintosh deve ser refletida na próxima execução apropriada, com política de reinício/recarregamento definida na implementação.

## Decisions

1. **Usar Git para fonte, sem GitHub Releases.** Atualizar remotes dos checkouts locais e remover a consulta/extract de assets binários do fluxo da opção da TUI. Alternativa considerada: manter releases como fallback; rejeitada porque reinstalaria binários que não foram compilados para o Linux/host corrente e conflita com o build local como fonte de verdade.

2. **Validar os dois checkouts antes de modificar qualquer um.** Confirmar checkout Git, remote esperado, branch de integração configurada, working tree limpa (incluindo arquivos não rastreados relevantes) e possibilidade de fast-forward para ambos. Se qualquer validação falhar, não iniciar pulls. Depois de obter referências remotas para ambos, aplicar fast-forward; se ocorrer falha transitória durante a segunda atualização após a primeira já avançar, reportar estado parcial/commit por checkout e permitir repetição idempotente. Alternativa considerada: `git pull` sequencial sem preflight; rejeitada porque pode deixar resultado parcial sem explicação.

3. **Clonar reims-vgpu na primeira atualização.** A ISO continuará empacotando fontes sem o histórico Git. Quando `/opt/reims-vgpu` ainda não for clone Git, o atualizador fará um clone do fork público `felipeab10/reims-vgpu` para área temporária, verificará remote/branch e somente então promoverá a árvore para o caminho de build. Isso mantém a ISO menor e evita converter a cópia empacotada em um repositório Git parcial. A primeira atualização exige rede e espaço temporário. Não copiar o `.git` do submódulo QEMU como se fosse o repositório raiz; preservar o mecanismo de vendor/submodule no clone.

4. **Atualizar aplicação e motor na sequência segura definida pelo build.** Primeiro validar estado, fetch e fast-forward de ambos os repos; depois compilar pelo `core.reims_build.build()`/`scripts/install-ctl.sh --yes`, reaproveitando logs/progresso e evitando lógica de build duplicada no updater. A TUI deve executar o rebuild depois do update do fonte e antes de comunicar sucesso. A estratégia de instalação deve manter o build anterior disponível (o instalador já versiona QEMU em diretórios por build e troca `current` no fim); validar o mesmo comportamento para biblioteca e reims-ctl.

5. **Resultado composto e truthful.** Retornar estado estruturado por checkout e etapa de build, e exibir sucesso global apenas quando ambos os repos e rebuild forem bem-sucedidos. Sem mudanças remotas pode ser sucesso idempotente se o build instalado corresponder ao fingerprint; divergência local é bloqueio com instruções, não uma atualização forçada.

3. **Validar submódulo QEMU antes da promoção inicial.** Além dos metadados raiz e demais fontes, exigir `vendor/qemu/meson.build` na árvore staged; um clone com gitlink não populado não é um clone íntegro e nunca substitui a árvore empacotada.

6. **Evitar que o processo Python em execução masque atualização do próprio app.** O checkout de `/opt/vhackintosh` pode avançar enquanto o processo atual segue com módulos já importados; reportar que o novo código passa a valer na próxima inicialização da TUI/appliance (ou solicitar reinício explícito) em vez de sugerir hot reload que não existe.

## Risks / Trade-offs

- [A appliance Live/Kiosk executa a TUI como root no tty1, mas uma instalação manual pode executá-la como usuário comum] → o updater exigirá privilégios root para operar nos caminhos `/opt` e reportará um erro explicativo quando chamado sem eles; não adicionará regra sudoers nem fará elevação implícita.

- [Primeira atualização depende de rede e espaço para clonar o reims-vgpu] → clonar em diretório temporário, validar antes da promoção e preservar a árvore empacotada até o clone estar íntegro.
- [Os diretórios `/opt` podem ter ownership/permissões incompatíveis com o usuário da TUI] → definir e testar mecanismo de atualização privilegiada restrito à operação, sem executar todo o app como root e sem alterar ownership de maneira ampla.
- [Fontes avançam mas build falha] → manter build instalado anterior, não atualizar stamp de sucesso e deixar o próximo início detectar mismatch; comunicar os commits já avançados e a opção de retry.
- [Mudança em API/ABI entre commits dos dois repos] → atualizar ambos antes do build, compilar o par conjunto e só disponibilizar o `current` após sucesso.
- [Network/remote muda durante fetch/pull] → guardar os commits alvo obtidos no preflight e reportar claramente se aplicação parcial ocorrer; operação idempotente permite retry.

## Migration Plan

1. Alterar o empacotamento/atualizador para que `/opt/reims-vgpu` possa atualizar via Git com origem configurada, sem incluir binários precompilados como substituto das fontes.
2. Implementar validação/fetch/fast-forward dos dois checkouts e resultado por etapa.
3. Conectar o sucesso de sync ao rebuild completo existente e atualizar strings da TUI.
4. Testar primeira execução/clonagem, upgrade normal, sem mudanças, árvore suja, falha de remote, falha de build e preservação do QEMU anterior.
5. Rollback: antes de avançar commits, erro interrompe sem mutação; após avanço parcial ou build falho, informar os commits e permitir novo update/rebuild. O build anterior segue selecionado até um install novo concluir.

## Open Questions

Nenhuma decisão de escopo pendente. A primeira clonagem do reims-vgpu foi definida para ocorrer na primeira atualização, em diretório temporário, para não aumentar o tamanho da ISO.
