## Context

Veja [proposal.md](proposal.md) e os contratos em `specs/`. Observado no código atual: `build-iso.sh` copia a árvore `/home/felipeab10/reims-vgpu` por padrão, não copia `.cargo/config.toml` a menos que exista regra `vendored-sources` e não grava manifesto de pins. O perfil OpenCore usa `templates/config.plist` e `templates/OpenCore.qcow2`; a ISO examinada continha serial/MLB/UUID/ROM não neutros nas seções `DataHub`, `PlatformNVRAM` e `SMBIOS`, embora `Generic` estivesse neutro. `DiskProvisioner.provision()` copia o template e tenta injetar SMBIOS apenas se `guestfish` existe, ocultando exceções; `GenSMBIOS` atual cria serial/MLB aleatórios, enquanto o fluxo ULT observado usa `macserial -a`, filtra pelo modelo e consome ProductName, serial, board serial, UUID e ROM.

A implantação de appliance precisa continuar capaz de inicializar e compilar localmente. O repositório não deve inserir identificadores Apple reais nem prometer aprovação do iCloud: macserial gera candidatos no formato esperado, e a aceitação final depende da Apple/conta/rede e outras condições não verificáveis pelo host. A VM Tahoe de referência contém identidade ativa e não pode ser usada como fonte/cópia.

## Goals / Non-Goals

**Goals:**
- Separar template EFI genérico distribuído da identidade individual persistida/injetada em cada VM.
- Tornar build da ISO fail-closed: inputs declarados, verificações finais e manifesto comprovável.
- Integrar geração `macserial` e injeção EFI transacional ao provisionamento, com erro explícito em falha.
- Oferecer diagnóstico local seguro para VMs antigas sem trocar identidade por iniciativa própria.

**Non-Goals:**
- Garantir ativação iCloud, contornar controles Apple ou reutilizar identidade de hardware/da Tahoe.
- Substituir/alterar automaticamente serial de VM instalada, cookies, tokens Apple ID ou dados NVRAM persistentes.
- Copiar código do ultimate-macOS-KVM sem avaliação de licença/proveniência; tomar seu comportamento observado como referência.
- Fazer alteração no reims-vgpu upstream/QEMU. A especificação de QEMU vendorizado limita-se a proveniência e empacotamento local.

## Decisions

1. **Um modelo genérico, identidade per-VM.** O template distribuído terá placeholders neutros e OVMF_VARS genérico; na criação uma tupla é gerada e guardada junto da VM. Alternativa: distribuir identidade fixa na ISO; rejeitada por vazamento, compartilhamento de identidade e inconsistência entre VMs.

2. **`macserial` oficial como gerador, com dependência versionada e verificada.** O appliance deve obter/empacotar build compatível do OpenCorePkg macserial, fixar versão/hash e invocá-lo explicitamente para o modelo escolhido; interpretar e validar a saída. A ROM é gerada conforme modo GenSMBIOS adotado (prefixos se procedência/licença permitir ou geração local compatível); não importar automaticamente lista de ROM pessoal/terceira. Alternativa: formato randômico atual; rejeitada porque não equivale à geração/validação macserial observada.

3. **Aplicação atômica e persistência antes do boot.** Criar imagem EFI temporária a partir do template, atualizar todos os caminhos de identidade coerentes com `PlatformInfo`/OpenCore, ler de volta e validar contra modelo/tupla persistida; somente então renomear/promover EFI e gravar registro protegido da VM. Falha mantém template/VM anterior utilizável, cancela boot e não registra sucesso. Não engolir exceção de `guestfish`.

4. **Modelo escolhido e configuração OpenCore determinam a tupla.** A seleção atual de modelo deve ser explícita, apoiada pela versão Tahoe e configuração testada; defaults como `MacPro7,1` não podem divergir entre nome de modelo, macserial e `SystemProductName`. `Generic` é a entrada de verdade para SMBIOS automática; demais seções `DataHub`, `PlatformNVRAM`, `SMBIOS` devem ser neutras/consistentes com a estratégia OpenCore validada, não conter sobras estáticas.

5. **Build de ISO comprova payload, não só presença.** Validar `qemu-img check`, extrair o plist interno com ferramenta disponível no build, comparar byte/hash com config previsto, checar kext e limites de kernel, varrer valores pessoais/identificadores não placeholder, verificar OVMF e confirmar sha do SquashFS e ISO. Gerar manifesto com commit raiz, gitlink/commit QEMU, lockfile, pins/macserial, hashes dos componentes e checks; recusar fallback implícito para checkout dirty ou submódulo divergente, a menos que explicitamente escolhido e anotado. Dependências de Cargo vendorizadas exigem `.cargo/config.toml` efetiva e teste offline ou build de appliance declara rede requerida e valida disponibilidade.

6. **Separar “readiness” de sucesso Apple.** Diagnóstico local pode dizer se tupla e EFI coincidem, e apresentar checagens legítimas (rede/data e hora, conta e estado Apple); não coleta credenciais/token, não envia identificadores à rede, e não dá “iCloud ativado” como conclusão sem verificação feita no macOS pelo usuário.

7. **Reparo de VM existente é opt-in e com backup.** Primeiro executar checks somente leitura. Se sugerir troca de identidade, explicar possível invalidade de sessão/ativação e criar backup consistente de EFI/config; pedir confirmação explícita. Não aplicar mudança a VM Tahoe de referência por padrão.

## Risks / Trade-offs

- [Apple pode rejeitar identificadores sintéticos ou a conta por motivos fora da configuração local] → comunicar limites e não garantir ativação; teste final é feito pelo usuário no macOS e reportado como resultado empírico.
- [Formato macserial/ROM ou campos OpenCore mudam entre versões] → fixar versão, criar testes de amostra sem dados reais e validar no build antes de distribuir.
- [Injeção na imagem ou persistência pode falhar a meio] → usar temporários, verificação pós-write, promoção atômica e backup restaurável.
- [ISO pode trazer valores residuais fora de `Generic` ou no OVMF_VARS] → scan de todas as seções/fontes de configuração e denylist de placeholder conhecida, além de exigir OVMF de origem versionada.
- [Build offline não ter todos os crates/submódulos] → definir modo online/offline de forma explícita, registrar inputs e testar com rede desabilitada quando offline for prometido.
- [Identificadores SMBIOS são sensíveis e podem aparecer em logs/configs exportadas] → restringir permissões de persistência, redigir logs e nunca incluir esses valores no manifesto da ISO.

## Migration Plan

1. Corrigir/sanitizar `config.plist`, OpenCore.qcow2 e OVMF templates; automatizar leitura do plist da imagem para comprovar igualdade e ausência de valores pessoais.
2. Definir contrato/pins do código-fonte reims-vgpu e QEMU, completude da estratégia Cargo vendored/offline e manifest de build; construir e inspecionar uma ISO limpa.
3. Implementar geração oficial por modelo e armazenamento por VM; migrar VMs existentes apenas para leitura/diagnóstico sem preencher identidade silenciosamente.
4. Aplicar EFI temporária, validar todos os campos e só então promover; teste automatizado de falha em cada etapa sem corromper artefatos.
5. Testar VM Tahoe recém-criada até desktop, confirmar carregamento da kext e checagens de identidade em macOS; usuário verifica ativação iCloud. Registrar resultado observado, não como garantia universal.
6. Para ISO inválida, conservar versão anterior e não publicá-la; rollback de VM existente restaura backup de EFI/config sem alterar disco macOS ou identidade Tahoe sem consentimento.

## Open Questions

- Confirmar durante implementação os pins/hashes concretos do OpenCorePkg/macserial e os meios aprovados de distribuição no perfil Archiso.
- O usuário deverá confirmar se deseja um fluxo explícito para reparar/regenerar identidade em uma VM já instalada; a mudança assume que há apenas diagnóstico e nenhum reset automático.
