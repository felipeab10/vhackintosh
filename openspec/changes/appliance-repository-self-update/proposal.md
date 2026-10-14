## Why

A opção atual de atualização busca uma release binária do reims-vgpu no GitHub, mas o fluxo do appliance agora distribui fontes e compila localmente. Isso deixa a opção de atualização incompatível com a estratégia atual e pode instalar artefatos sem correspondência com o código-fonte; ela deve atualizar os dois repositórios implantados e reconstruir o motor localmente.

## What Changes

- Substituir a atualização de release/binário do reims-vgpu por atualização do repositório Git local `felipeab10/reims-vgpu` dentro do appliance.
- Atualizar também o repositório Git `felipeab10/vhackintosh` implantado no appliance, com tratamento explícito de diretório sujo, falhas de rede e conflitos/non-fast-forward.
- Após atualizações bem-sucedidas, rebuildar integralmente reims-vgpu, QEMU, reims-ctl e ROM/firmware necessários pelo fluxo de build suportado pelo appliance.
- Executar atualizações e rebuild em ordem segura; só anunciar sucesso quando ambos os repositórios estiverem atualizados e o rebuild completo tiver terminado sem erro.
- Preservar a versão instalada em uso quando atualização, sincronização ou compilação falhar, e apresentar ao usuário o estado/ação necessária.

## Capabilities

### New Capabilities
- `appliance-source-self-update`: Atualização local, sincronização e recompilação dos componentes-fonte do appliance.

### Modified Capabilities
- Nenhuma — o diretório `openspec/specs/` não contém especificações existentes; o comportamento de atualização será especificado como capacidade nova.

## Impact

- `core/updater.py`: substituir download/extract de releases e organizar sincronização dos dois repositórios.
- `core/reims_build.py`, `core/builder.py` e/ou `scripts/install-ctl.sh`: reutilizar o caminho de rebuild completo existente e detectar/retornar falhas corretamente.
- `ui/tui.py` e `core/i18n.py`: atualizar rótulo, mensagens, confirmação/progresso/resultado da opção de atualização nos catálogos PT/EN.
- Empacotamento e instalação em `/opt/reims-vgpu` e `/opt/vhackintosh`; permissões, ownership e política para alterações locais precisam ser tratados sem perda silenciosa de dados.
- Testes para sucesso, falhas parciais, idempotência, repositórios sujos e disponibilidade dos binários após erro.
