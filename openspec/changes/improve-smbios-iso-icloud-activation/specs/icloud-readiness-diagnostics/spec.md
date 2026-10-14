## Purpose

Ajuda o operador a identificar falhas locais de identidade, provisionamento EFI e pré-requisitos de ativação observáveis quando o macOS apresenta erro de iCloud, sem prometer que identidade gerada garantirá aceitação/ativação pelos serviços Apple.

## ADDED Requirements

### Requirement: Report local activation-readiness checks truthfully
O appliance SHALL diagnosticar presença e consistência da tupla SMBIOS persistida, correspondência com a EFI e configuração de inicialização da VM, distinguindo checks aprovados de fatores externos não verificáveis (rede, Apple ID, estado dos servidores, elegibilidade e políticas Apple).

#### Scenario: Identidade local consistente
- **WHEN** a identidade persistida está completa e coincide com OpenCore
- **THEN** o diagnóstico informa apenas que os checks locais passaram, sem alegar que iCloud necessariamente ativará

#### Scenario: Campo ausente ou divergente
- **WHEN** serial, MLB, UUID, ROM ou modelo está ausente/divergente entre persistência e EFI
- **THEN** o relatório nomeia discrepância de forma redigida e indica como inspecionar/reprovisionar com segurança, sem revelar valores completos e sem alterá-los automaticamente

#### Scenario: Causa não verificável pelo host
- **WHEN** os checks locais passam mas a Apple retorna erro de ativação
- **THEN** o appliance declara que não pode verificar a decisão/conta/servidor da Apple e apresenta passos legítimos de diagnóstico em vez de afirmar falha de serial ou sucesso garantido

### Requirement: Existing VM recovery requires explicit consent and preserves evidence
Para uma VM já instalada, o appliance SHALL priorizar diagnóstico sem mudança e SHALL solicitar confirmação antes de backup, troca de SMBIOS ou qualquer operação que possa invalidar sessão/ativação existente. Não SHALL copiar identidade da VM de referência nem reutilizar automaticamente identificadores de hardware de terceiros.

#### Scenario: Erro “unknown error” em VM existente
- **WHEN** o operador solicita ajuda após falha de iCloud
- **THEN** o sistema oferece checks reversíveis e solicita consentimento antes de modificar EFI/identidade, explica efeito potencial sobre Apple ID/serviços e preserva backup recuperável

#### Scenario: Usuário não confirma alteração
- **WHEN** operador cancela a proposta de regenerar ou reparar
- **THEN** nenhum arquivo/configuração/estado NVRAM da VM é modificado
