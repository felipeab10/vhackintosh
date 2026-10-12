## Purpose

Dá ao convidado macOS suporte a virtualização aninhada — VT-x/AMD-V visível
dentro da VM — para que o macOS rode suas próprias virtual machines (Docker
com VM, UTM, xhyve), com o appliance preparando e checando o host.

## ADDED Requirements

### Requirement: O host expõe virtualização aninhada ao guest
O appliance SHALL configurar o KVM do host com virtualização aninhada habilitada
e SHALL apresentar a CPU à VM com a flag de virtualização (VT-x/vmx ou AMD-V/svm)
ativa, de modo que o convidado macOS enxergue a extensão.

#### Scenario: Flag visível dentro do macOS
- **WHEN** a VM está em execução com nested habilitado e o usuário consulta a
  CPU dentro do macOS (ex.: `sysctl machdep.cpu.features`)
- **THEN** a saída indica a presença de VMX (VT-x) — ou SVM no caso AMD

#### Scenario: Módulo KVM do host habilitado
- **WHEN** o appliance está pronto para rodar VMs
- **THEN** o módulo KVM do host está carregado com `nested=1` (habilitado pelo
  appliance quando necessário e possível)

### Requirement: O appliance checa a prontidão de virtualização aninhada
A checagem de prontidão SHALL verificar a disponibilidade de nested no host e
SHALL avisar o usuário quando não estiver disponível (BIOS/driver não permitem),
distinguindo essa limitação da ausência básica de virtualização (KVM).

#### Scenario: Nested indisponível
- **WHEN** o host não permite nested (flag ausente no módulo KVM e impossível
  habilitar)
- **THEN** a prontidão avisa que a VM do macOS não terá suporte a virtualização
  aninhada, com a razão

#### Scenario: Nested habilitado e verificado
- **WHEN** o host permite nested e o appliance o habilitou
- **THEN** a prontidão aprova e o guest enxerga a extensão
