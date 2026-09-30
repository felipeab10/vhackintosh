# Tarefa T04: Core Builder com Progresso Oculto

## Objetivo
Ocultar saídas de compilação verbosas de terminal e exibir barras de progresso modernas, ricas e animadas ao compilar `reims-vgpu`, UEFI GOP ROM e QEMU.

## Itens a Desenvolver
- [ ] Wrapper de compilação em Python com `rich.progress`:
  - Captura e redirecionamento de logs para arquivo de log em background (`~/.config/vhackintosh/logs/build.log`).
  - Análise em tempo real do progresso de compilação (crates compiladas pelo `cargo`, alvos do `ninja`/QEMU).
  - Exibição de spinner elegante, percentual e tempo decorrido.
- [ ] Compilação de componentes:
  - `reims-vgpu-gop.rom` (UEFI GOP ROM).
  - `reims-vgpu` staticlib (Rust).
  - `qemu-system-x86_64` (C/Ninja).
