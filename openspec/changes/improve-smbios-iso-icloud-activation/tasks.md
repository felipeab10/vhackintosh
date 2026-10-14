## 1. Sanitizar e verificar OpenCore/EFI templates

- [ ] 1.1 Sanitizar valores pessoais em `templates/config.plist` em todas as seções PlatformInfo/NVRAM e verificar o config dentro de `OpenCore.qcow2` bate byte/hash. (template passa testes locais de neutralidade, comparação guestfish e `qemu-img check`; sanitização ocorreu antes desta rodada e diff atual não permite atribuir/verificar a origem)
- [x] 1.2 Validar templates OVMF_CODE/OVMF_VARS provisionados para appliance e adicionar teste que detecta valores SMBIOS conhecidos sem imprimir valores sensíveis. (hashes comparados entre template e payload; ainda não prova ausência de todo estado NVRAM arbitrário)
- [x] 1.3 Adicionar teste de estrutura OpenCore: `qemu-img check`, plist parseável, ReimsVgpu/Lilu presentes e habilitadas com faixas de kernel esperadas. (check da imagem e do plist embutido passa; testes automatizados verificam campos e limites)

## 2. Proveniência, dependências e validação final da ISO

- [ ] 2.1 Definir e validar origem explícita/limpa do reims-vgpu, branch/commit raiz e pin real do submódulo QEMU; não publicar ISO com árvore parcial ou dirty não declarada. (criado worktree detached limpo em `/var/tmp/vhackintosh-reims-clean-20261013`, commit raiz `5ebd61ebb67a3af79af3689a4fece7a86e0b71e0`, QEMU no pin `1a72df984914fb9929d35b1217619178133d65a4`; build recusado por falta de root, portanto provenance no payload não validado)
- [ ] 2.2 Tornar estratégia Cargo vendored/offline explícita: empacotar configuração vendored, validar fontes/caches exigidos e testar `--offline`, ou marcar e validar dependência de rede. (worktree limpo compilou por `cargo check --offline --workspace --all-targets --no-default-features --features reims-vgpu-core/testkit,reims-vgpu/testkit,reims-vgpu/vulkan,reims-vgpu-c/vulkan`; `--locked` falha pois o lockfile versionado está incompleto para as dependências Git; aparelho ainda avisa que poderá exigir rede)
- [x] 2.3 Empacotar dependência `macserial` 2.1.8 com hash e licença/proveniência OpenCorePkg documentadas; cópia executável isolada, sem código-fonte de terceiros.
- [ ] 2.4 Criar validador do payload final: boot catalog UEFI, manifesto/hashes SquashFS, fontes/templates OpenCore e OVMF, plist interno/externo, kext e scan de identidade pessoal. (validador expandido para boot catalog, kext, scan SMBIOS, versão MacSerial, OVMF e manifesto; ISO existente sem MacSerial falha como esperado; build limpo e matriz de regressão ainda pendentes)
- [ ] 2.5 Publicar ISO/checksum/manifesto de forma atômica somente quando todas as validações passam; testes de regressão para ISO incompleta, divergência de hash e EFI não sanitizada. (checksum/manifesto usam temporários e rename atômico; artefato/manifesto validados antes de publicar checksum; build não executado e matriz de regressão pendente)

## 3. Geração macserial e identidade por VM

- [ ] 3.1 Definir modelo SMBIOS suportado e parser testável da saída de `macserial`; validar modelo, serial, MLB, UUID, ROM e unicidade. (modelo/parser/UUID/ROM e colisão MacSerial cobertos; unicidade global não é garantível entre instâncias concorrentes sem reserva transacional; Store recusa conflitos no cadastro)
- [x] 3.2 Substituir geração aleatória local por wrapper macserial versionado; tratar indisponibilidade, exit code e saída malformada como erro explícito. (wrapper fail-closed; versão 2.1.8 executada em teste local)
- [x] 3.3 Persistir tupla por VM com permissões restritas e migração compatível para VM antiga sem inventar identidade automaticamente. (persistência atômica mode 0600; teste restart/permissões/migração sem gerar)
- [ ] 3.4 Injetar identidade na EFI temporária nos campos OpenCore corretos; reler, validar igualdade e promover atomicamente, preservando EFI anterior em falha. (staging/releitura e rollback após promoção em falha de integridade implementados; faltam testes fault-injection/guestfish)
- [ ] 3.5 Fazer provisionamento falhar se geração/injeção/revalidação não terminar; remover exceções silenciosas e garantir que reims-ctl/QEMU não iniciem boot parcial. (provisionamento falha fechado; persistência da tupla recém-gerada pelo caminho comum ainda precisa ser validada)
- [ ] 3.6 Adicionar testes unitários de parser/modelos, identidade persistente/restart, colisões e falha simulada de leitura/escrita/guestfish. (parser, persistência e colisão MacSerial/cadastro cobertos; falhas injetadas guestfish/escrita ainda pendentes)

## 4. Diagnóstico e migração de VMs existentes

- [ ] 4.1 Adicionar diagnóstico somente leitura de identidade persistida versus config EFI com valores redigidos e orientação de checks locais/limites de validação Apple. (interface integrada; leitor atualmente assume guestfish e partição EFI /dev/sda1)
- [ ] 4.2 Exigir backup e confirmação separada antes de qualquer comando opcional que altere identidade em VM existente; teste cancelar não muda arquivos. (confirmação e cópia da EFI implementadas; não executar nem marcar completa sem rollback/armazenamento persistente transacional e teste de cancelamento)
- [x] 4.3 Adicionar mensagens PT/EN para falha “unknown error”, verificações locais aprovadas/divergentes e nenhuma garantia de ativação. (mensagens locais PT/EN adicionadas; encaminhamento de todos os erros unknown pendente)

## 5. Validação integral segura

- [ ] 5.1 Executar testes automatizados de regressão e build limpo de ISO; registrar commit/pins, hash e resultados dos checks sem serial/MLB/UUID/ROM da VM. (14 testes e checks estáticos passam; worktree limpo Reims em `/var/tmp/vhackintosh-reims-clean-20261013` validado com `cargo check --offline` nas features Linux; build da ISO parou no preflight por root/sudo não disponível; nenhum artefato/manifesto novo)
- [ ] 5.2 Verificar em VM isolada que SMBIOS da EFI corresponde à tupla persistida, a kext carrega no macOS alvo e desktop/reims-vgpu funcionam; manter o disco Tahoe existente intocado.
- [ ] 5.3 Solicitar verificação manual de iCloud pelo usuário e registrar resultado como observado, sem tratá-lo como aceitação universal nem automatizar autenticação.
