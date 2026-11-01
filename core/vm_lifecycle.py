"""Serviços sem interface para o ciclo de lançamento de uma VM.

O módulo mantém regras reutilizáveis fora da TUI. O start efetivo continua a
pertencer ao `reims-ctl`; este serviço não monta argv de QEMU nem abre uma
segunda sessão QMP.
"""

from __future__ import annotations

import datetime
import os
import shutil
import tomllib
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from core.config import CONFIG_DIR, ExclusiveVMLock, VMConfig, VMManagerStore
from core.disk import DiskProvisioner
from core.gpu import GPUChecker
from core import reims_ctl
from core.install_phase import (
    DiskProbe,
    InstallEvidence,
    InstallPhaseResolver,
    InstallProbe,
    LaunchPolicy,
    classify_evidence,
)


class LaunchRejected(RuntimeError):
    """Expected preflight failure returned as a structured VM outcome."""

    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


class LaunchSessionFailed(RuntimeError):
    """The Reims start command exited unsuccessfully after being launched."""

    def __init__(self, exit_code: int):
        super().__init__(f"reims-ctl start exited with status {exit_code}")
        self.exit_code = exit_code


@dataclass(frozen=True)
class LaunchResult:
    """Structured outcome of a synchronous VM launch lifecycle."""

    vm_id: str
    state: str
    phase: str
    reset_count: int = 0
    last_reason: Optional[str] = None
    error_code: Optional[str] = None
    message: Optional[str] = None
    details: dict[str, Any] | None = None


@dataclass
class LaunchHooks:
    """Adapters for host/UI side effects; lifecycle ordering stays centralized."""

    find_reims_dir: Callable[[], Optional[Path]]
    vulkan_readiness: Callable[[], tuple[bool, str]]
    ensure_release: Callable[[Optional[str], Optional[str]], bool]
    recover_storage: Callable[[VMConfig], tuple[Optional[Path], str]]
    decide_runtime: Callable[[Optional[str], str, Optional[Path]], tuple[bool, str]]
    apply_graphics_environment: Callable[[dict[str, str]], Any]
    on_usb_ready: Callable[[VMConfig], None]
    apply_gpu_environment: Callable[[VMConfig, dict[str, str]], None]
    write_vm_config: Callable[..., Any]
    ensure_registered: Callable[[VMConfig], None]
    ctl_binary: Callable[[], Optional[str]]
    session_argv: Callable[[list[str], dict[str, str]], tuple[list[str], bool]]
    monitor_factory: Callable[[VMConfig], Any]
    session_factory: Callable[[list[str], dict[str, str], VMConfig, Callable[[VMConfig], None]], Any]
    apply_power_policy: Callable[[VMConfig, LaunchPolicy, Any], None]
    post_phase_update: Callable[[VMConfig, LaunchPolicy], None]
    event: Callable[[str, dict[str, Any]], None]
    write_vm_projection: Optional[Callable[..., Any]] = None


def _default_usb_auto_attach(vm: VMConfig) -> None:
    """Best-effort attachment of the VM's persisted USB selection after QMP is ready."""
    wanted = getattr(vm, "usb_auto_attach", []) or []
    if not wanted:
        return
    from core import passthrough

    try:
        sock = reims_ctl.qmp_socket(vm)
        devices: dict[str, list[Any]] = {}
        for device in passthrough.list_usb_devices():
            device_id = f"{device.vendor_id}:{device.product_id}".lower()
            devices.setdefault(device_id, []).append(device)
    except Exception:
        return
    for key in wanted:
        matches = devices.get(str(key).lower(), [])
        if len(matches) != 1:
            if len(matches) > 1:
                print(f"  aviso: auto-anexo USB ignorado ({key}); há dispositivos duplicados com o mesmo VID:PID")
            continue
        dev = matches[0]
        try:
            passthrough.attach(sock, dev)
            print(f"  usb: {dev.short_label} anexado à VM")
        except Exception as exc:
            # Matching the TUI contract: USB auto-attach is best effort.
            print(f"  aviso: auto-anexo USB falhou ({exc}); a VM segue normalmente")
            continue


def _default_power_policy(vm: VMConfig, policy: LaunchPolicy, monitor: Any, store: VMManagerStore) -> None:
    """Persist observed resets while keeping host power isolated by default."""
    resets = int(getattr(monitor, "reset_count", 0) or 0)
    if resets:
        vm.install_reboots = int(getattr(vm, "install_reboots", 0)) + resets
        store.update_vm(vm)


def _default_post_phase(vm: VMConfig, policy: LaunchPolicy, store: VMManagerStore) -> None:
    """Persist evidence but require a local explicit confirmation to install phase."""
    if policy.phase.value == "installed":
        return
    assessment = assess_post_install(vm)
    vm.install_evidence = assessment.evidence.value
    # No non-interactive caller may automatically enable host power sync.
    vm.install_phase = "installing"
    store.update_vm(vm)


def _ensure_reims_release(reims_dir: Optional[str], reservation_vm_id: str | None = None) -> bool:
    """Install/validate the prebuilt runtime required by the single launcher."""
    from core.reims_build import evaluate, install_prebuilt, qemu_binary, qemu_data_dir

    if reims_dir:
        selected = Path(reims_dir).expanduser()
        installed_runtime = qemu_data_dir()
        try:
            is_installed_runtime = selected.resolve() == installed_runtime.resolve()
        except OSError:
            is_installed_runtime = False
        if is_installed_runtime:
            from core.reims_ctl import reims_ctl_bin

            binary = qemu_binary()
            return bool(reims_ctl_bin() and binary.is_file() and os.access(binary, os.X_OK))

    state = evaluate(explicit=reims_dir)
    if state.reims_dir is None or not install_prebuilt(
        state.reims_dir, reservation_vm_id=reservation_vm_id
    ):
        return False
    state = evaluate(explicit=str(state.reims_dir))
    return bool(state.binary_path and state.binary_path.exists())


def default_launch_hooks(store: VMManagerStore) -> LaunchHooks:
    """Construct appliance adapters safe for a non-interactive caller."""
    from core.reims_build import find_launch_runtime_dir
    from core.power import QMPPowerMonitor
    from core.qmp.relay import qmp_control_socket

    return LaunchHooks(
        find_reims_dir=find_launch_runtime_dir,
        vulkan_readiness=GPUChecker.check_vulkan_host_readiness,
        ensure_release=_ensure_reims_release,
        recover_storage=recover_reimsctl_vm_storage,
        decide_runtime=decide_reimsctl,
        apply_graphics_environment=reims_ctl.apply_test_graphics_environment,
        on_usb_ready=_default_usb_auto_attach,
        apply_gpu_environment=lambda _vm, _env: None,
        write_vm_config=reims_ctl.write_vm_toml,
        ensure_registered=reims_ctl.ensure_registered,
        ctl_binary=reims_ctl.reims_ctl_bin,
        session_argv=reims_ctl.session_argv,
        monitor_factory=lambda vm: QMPPowerMonitor(
            sock_path=str(reims_ctl.qmp_socket(vm)),
            control_socket_path=str(qmp_control_socket(vm.id)),
        ),
        session_factory=lambda argv, env, vm, on_usb_ready: reims_ctl.ReimsCtlSession(
            argv,
            env=env,
            on_ready=lambda: on_usb_ready(vm),
            # A systemd VM unit owns the whole QEMU cgroup. Its supervisor must
            # outlive the guest; returning after the interactive 10-minute
            # ceiling would make systemd kill the still-running QEMU process.
            wait_timeout_sec=None if env.get("VHACKINTOSH_VM_SUPERVISOR") == "1" else reims_ctl._ESPERA_MAXIMA_S,
        ),
        apply_power_policy=lambda vm, policy, monitor: _default_power_policy(vm, policy, monitor, store),
        post_phase_update=lambda vm, policy: _default_post_phase(vm, policy, store),
        event=lambda _name, _payload: None,
        write_vm_projection=reims_ctl.write_vm_configuration,
    )


def decide_reimsctl(
    reims_dir: Optional[str],
    macos_version: str,
    custom_dir: Optional[Path],
    *,
    create_rail_dir: bool = True,
) -> tuple[bool, str]:
    """
    Decide se a VM pode subir pelo reims-ctl (o único motor), ou a recusa de
    iniciar a VM — não há mais um motor de reserva para onde cair.

    Devolve `(pode_subir, motivo)` — o `motivo` existe porque um "não"
    silencioso é indistinguível de um defeito, e esta decisão já custou o
    salvamento de aceleração do appliance uma vez: a ausência de algo
    essencial passava sem ninguém perceber.

    Três condições, cada uma legítima:
      1. o `reims-ctl` instalado junto da release — sem ele não há quem suba
         a VM nem registre o QMP;
      2. o QEMU do reims-vgpu instalado (`qemu/current`) — sem o motor
         acelerado o convidado não sobe (o appliance não tem motor de reserva);
      3. o layout da VM (macos.qcow2 + OpenCore.qcow2 + OVMF_VARS.fd, vindos
         de `custom_dir`) mais o OVMF_CODE.fd, que é copiado do template aqui.

    `create_rail_dir` permanece só para compatibilidade de assinatura (o
    antigo rail criava um diretório; agora nada é escrito aqui).
    """
    del create_rail_dir  # nenhum efeito colateral nesta decisão
    if not reims_dir:
        return False, "sem diretório do reims-vgpu"
    if not reims_ctl.reims_ctl_bin():
        return False, "reims-ctl não encontrado; instale uma release do reims-vgpu"
    from core.reims_build import qemu_binary

    if not qemu_binary().exists():
        return False, f"QEMU do reims-vgpu não instalado (esperado em {qemu_binary()})"

    if not (
        custom_dir
        and (custom_dir / "macos.qcow2").exists()
        and (custom_dir / "OpenCore.qcow2").exists()
        and (custom_dir / "OVMF_VARS.fd").exists()
    ):
        return False, f"layout persistente incompleto em {custom_dir}"

    # O reims-ctl exige o código do OVMF ao lado das variáveis, no diretório da
    # VM. VMs provisionadas antes desta integração só têm o par de variáveis;
    # completamos do template (idempotente).
    if not (custom_dir / "OVMF_CODE.fd").exists():
        if not DiskProvisioner.ensure_ovmf_code(custom_dir):
            return False, f"OVMF_CODE.fd ausente e sem template para copiar (em {custom_dir})"

    return True, f"reims-ctl ({macos_version})"


def recover_reimsctl_vm_storage(
    vm: VMConfig,
    *,
    config_dir: Path = CONFIG_DIR,
) -> tuple[Optional[Path], str]:
    """Adota com segurança o disco de uma VM existente do reims-ctl.

    Catálogos antigos do vHackintosh podem apontar para o diretório removido
    `~/reims-vgpu/vm/disks/...`, enquanto o disco foi mantido no diretório que
    o reims-ctl registrou. Só recuperamos uma VM parada, com o mesmo ID, disco
    e VARS válidos. Disco e OpenCore nativos ficam preservados por symlink no
    layout do vHackintosh. Isso mantém a mesma EFI que o `reims-ctl start`
    usava antes da migração (incluindo configurações e identidade já testadas).
    Só gera um OpenCore do template quando o registro nativo não tem uma EFI.
    """
    record = reims_ctl.vm_record(vm.id)
    if not record:
        return None, f"não encontrei uma VM reims-ctl registrada com ID {vm.id}"
    if record.get("state") != "stopped" or record.get("pid") is not None:
        return None, f"a VM reims-ctl {vm.id} precisa estar parada para recuperar o armazenamento"

    source_dir = Path(str(record.get("dir", ""))).expanduser()
    if not source_dir.is_dir():
        return None, f"o diretório registrado pelo reims-ctl não existe: {source_dir}"

    config_path = source_dir / "vm.toml"
    try:
        with config_path.open("rb") as fh:
            source_config = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return None, f"não consegui ler {config_path}: {exc}"

    disk_name = source_config.get("disk", "disk.qcow2")
    q35 = source_config.get("q35") or {}
    opencore_name = q35.get("opencore", "opencore.qcow2")
    vars_name = q35.get("firmware_vars", "OVMF_VARS.fd")
    source_disk = source_dir / disk_name
    source_opencore = source_dir / opencore_name
    source_vars = source_dir / vars_name
    if not source_disk.is_file() or source_disk.stat().st_size == 0:
        return None, f"o disco da VM registrada não existe ou está vazio: {source_disk}"
    if not source_vars.is_file():
        return None, f"as variáveis UEFI da VM registrada não existem: {source_vars}"

    target_dir = config_dir / "vms" / vm.id
    target_disk = target_dir / "macos.qcow2"
    target_opencore = target_dir / "OpenCore.qcow2"
    target_vars = target_dir / "OVMF_VARS.fd"
    target_dir.mkdir(parents=True, exist_ok=True)
    if target_disk.exists() or target_disk.is_symlink():
        try:
            same_disk = target_disk.resolve(strict=True) == source_disk.resolve(strict=True)
        except OSError:
            same_disk = False
        if not same_disk:
            return None, f"já existe outro disco no destino e não será sobrescrito: {target_disk}"
    else:
        target_disk.symlink_to(source_disk.resolve())

    # O launcher nativo pode manter uma EFI diferente do template distribuído
    # pelo vHackintosh. Preserve exatamente a imagem apontada pelo vm.toml que
    # já iniciou esta VM; a capitalização do nome é só o contrato do TOML novo.
    native_opencore = source_opencore.is_file() and source_opencore.stat().st_size > 0
    if native_opencore:
        if target_opencore.exists() or target_opencore.is_symlink():
            try:
                same_opencore = (
                    target_opencore.resolve(strict=True)
                    == source_opencore.resolve(strict=True)
                )
            except OSError:
                same_opencore = False
            if not same_opencore:
                return None, (
                    "já existe outro OpenCore no destino e ele não será substituído: "
                    f"{target_opencore}"
                )
        else:
            target_opencore.symlink_to(source_opencore.resolve())

    if not target_vars.exists():
        shutil.copy2(source_vars, target_vars)
    # write_vm_toml preserva a GPU e o MAC do TOML anterior. Levar só esse
    # arquivo para o novo diretório mantém a seleção Intel/NVIDIA que a VM já
    # usava, sem reaproveitar seus caminhos de EFI ou ajustes antigos.
    target_config = target_dir / "vm.toml"
    if not target_config.exists():
        shutil.copy2(config_path, target_config)
    source_code = source_dir / q35.get("firmware_code", "OVMF_CODE.fd")
    target_code = target_dir / "OVMF_CODE.fd"
    if not target_code.exists() and source_code.is_file():
        shutil.copy2(source_code, target_code)
    if not target_code.exists() and not DiskProvisioner.ensure_ovmf_code(target_dir):
        return None, f"OVMF_CODE.fd ausente e sem template para copiar (em {target_dir})"

    if native_opencore:
        vm.disk_path = str(target_disk)
    else:
        try:
            DiskProvisioner.provision(vm)
        except Exception as exc:  # noqa: BLE001 — devolve o motivo para a TUI
            return None, f"falha ao preparar OpenCore/UEFI no destino {target_dir}: {exc}"

    required = (target_disk, target_dir / "OpenCore.qcow2", target_vars)
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        return None, "a recuperação terminou sem estes arquivos: " + ", ".join(missing)
    if native_opencore:
        return target_dir, "disco e OpenCore nativos do reims-ctl preservados"
    return target_dir, "disco recuperado do reims-ctl; OpenCore criado pelo template vHackintosh"


@dataclass(frozen=True)
class InstallLaunchPreparation:
    """Resultado determinístico da preparação de uma partida da VM."""

    probe: DiskProbe
    policy: LaunchPolicy
    evidence: InstallEvidence
    installer_image: Optional[str]
    booted_at: str


def prepare_install_launch(
    vm,
    *,
    force_installation: bool = False,
    custom_dir: Optional[Path] = None,
    config_dir: Path = CONFIG_DIR,
    home: Optional[Path] = None,
    probe: Optional[DiskProbe] = None,
    now: Optional[datetime.datetime] = None,
) -> InstallLaunchPreparation:
    """Resolve política, evidência, horário e mídia sem persistir nem iniciar.

    A sonda é apenas leitura. Quando a política anexa o instalador, a ordem de
    busca permanece idêntica à sequência histórica da TUI.
    """
    if probe is None:
        probe = InstallProbe.probe(vm.disk_path) if vm.disk_path else DiskProbe()

    policy = InstallPhaseResolver.build_policy(
        vm, force_installation=force_installation, probe=probe
    )
    installer_image = None
    if policy.attach_installer:
        home = home or Path.home()
        candidates = (
            custom_dir / "BaseSystem.img" if custom_dir else None,
            config_dir / "images" / vm.macos_version / "BaseSystem.img",
            home / ".config" / "vhackintosh" / "images" / vm.macos_version / "BaseSystem.img",
        )
        installer_image = next(
            (str(candidate) for candidate in candidates if candidate and candidate.exists()),
            None,
        )

    timestamp = (now or datetime.datetime.now()).isoformat(timespec="seconds")
    return InstallLaunchPreparation(
        probe=probe,
        policy=policy,
        evidence=classify_evidence(probe),
        installer_image=installer_image,
        booted_at=timestamp,
    )


def record_install_launch(vm, preparation: InstallLaunchPreparation) -> None:
    """Atualiza o objeto VM com os mesmos metadados gravados antes do start."""
    vm.install_phase = preparation.policy.phase.value
    vm.install_evidence = preparation.evidence.value
    vm.last_booted_at = preparation.booted_at


@dataclass(frozen=True)
class PostInstallAssessment:
    """Evidência pós-boot; a confirmação da fase instalada cabe à UI."""

    probe: DiskProbe
    evidence: InstallEvidence
    looks_complete: bool


def assess_post_install(vm, probe: Optional[DiskProbe] = None) -> PostInstallAssessment:
    """Reavalia o disco sem promover automaticamente `INSTALLING` a `INSTALLED`."""
    if probe is None:
        probe = InstallProbe.probe(vm.disk_path) if vm.disk_path else DiskProbe()
    return PostInstallAssessment(
        probe=probe,
        evidence=classify_evidence(probe),
        looks_complete=probe.looks_like_complete_install,
    )


def run_rail_session(
    launch: Callable[[], object],
    make_monitor: Callable[[], object],
    is_installing: bool,
    *,
    max_relaunches: int = 30,
    settle_sec: float = 0.6,
    on_relaunch: Optional[Callable[[int], None]] = None,
) -> tuple[int, Optional[str]]:
    """Executa boots do reims-ctl, relançando após reboot durante instalação.

    O QEMU recebe `-action reboot=shutdown` no `vm.toml`, então o reboot do
    macOS encerra o QEMU em vez de reiniciá-lo. Isso evita o DeviceReset que
    pode congelar o host, mas exige que esta sessão relance o processo para a
    instalação atravessar os estágios do instalador.

    `launch()` devolve um adaptador com `.wait()`; `make_monitor()` cria um
    monitor QMP novo. Um monitor por boot é obrigatório: o loop do monitor
    encerra quando o socket QMP cai junto com o QEMU, então reaproveitá-lo
    deixaria os próximos reboots sem observação. O evento final é drenado antes
    de parar o monitor para não perder o reboot que antecede a saída do QEMU.

    Reboots são somados entre boots e devolvidos com o motivo mais recente para
    que a camada de energia possa aplicar a política à sessão completa. Um boot
    que termina sem reboot encerra o laço; exceder `max_relaunches` notifica com
    `-1` e para, preservando o limite histórico.
    """
    total_resets = 0
    last_reason: Optional[str] = None
    relaunches = 0

    while True:
        monitor = make_monitor()
        monitor.start()
        try:
            exit_code = launch().wait()
        finally:
            time.sleep(settle_sec)
            wait_for_disconnect = getattr(monitor, "wait_for_disconnect", None)
            if callable(wait_for_disconnect):
                wait_for_disconnect(timeout_sec=2.0)
            monitor.stop()

        total_resets += monitor.reset_count
        if monitor.last_reason:
            last_reason = monitor.last_reason

        # During macOS installation a guest reset can close the current Reims
        # start command while QMP reports the reboot. Preserve that supported
        # relaunch path. Otherwise a non-zero command result is a failed start,
        # even if Reims has already spawned a process; the caller will inspect
        # current runtime state before releasing admission.
        if (
            isinstance(exit_code, int)
            and exit_code != 0
            and not (is_installing and monitor.reset_count > 0)
        ):
            raise LaunchSessionFailed(exit_code)

        if not is_installing or monitor.reset_count == 0:
            break

        relaunches += 1
        if relaunches > max_relaunches:
            if on_relaunch:
                on_relaunch(-1)
            break
        if on_relaunch:
            on_relaunch(relaunches)

    return total_resets, last_reason


class VMLifecycleService:
    """One shared, synchronous start pipeline for TUI and appliance workers.

    The service owns ordering, reservations, Reims registration, GPU selection,
    QMP observation, and installation policy. Presentation and explicitly
    environment-specific side effects are supplied through `LaunchHooks`.
    QEMU argv remains owned exclusively by `reims-ctl`.
    """

    def __init__(
        self,
        store: VMManagerStore,
        *,
        lock: ExclusiveVMLock | None = None,
        lock_factory: Callable[[], ExclusiveVMLock] | None = None,
        hooks: LaunchHooks | None = None,
    ) -> None:
        self.store = store
        # A lifecycle service can be long-lived (the TUI) while distinct VMs
        # start concurrently. Each attempt needs its own reservation handle;
        # the filesystem lock still provides cross-process atomic admission.
        self.lock = lock
        self.lock_factory = lock_factory or ((lambda: lock) if lock is not None else ExclusiveVMLock)
        self.hooks = hooks or default_launch_hooks(store)

    def start(
        self,
        vm: VMConfig,
        *,
        force_installation: bool = False,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> LaunchResult:
        """Run the shared start path and return a structured terminal outcome."""
        emit = on_event or self.hooks.event
        phase = str(getattr(vm, "install_phase", "pending"))
        lock = self.lock_factory()
        if not lock.acquire(
            vm.name,
            ram_gb=vm.ram_gb,
            vm_id=vm.id,
            disk_path=vm.disk_path,
            disk_paths=[disk.path for disk in (vm.additional_disks or [])],
            usb_devices=vm.usb_auto_attach,
            network_ports=[vm.network_ssh_port] if vm.network_ssh_port else [],
        ):
            details = dict(lock.last_error or {})
            requested_gb = details.get("requested_gb")
            available_gb = details.get("available_gb")
            if isinstance(requested_gb, (int, float)) and isinstance(available_gb, (int, float)):
                error_code = "insufficient_host_memory"
                message = (
                    f"Memória insuficiente para iniciar esta VM: ela solicita {requested_gb:g} GiB, "
                    f"mas há somente {available_gb:g} GiB disponíveis para novas VMs depois da margem "
                    "de segurança do host. Feche outras VMs ou reduza a memória configurada."
                )
            elif details.get("reason") == "storage_full" and details.get("resource") == "vm_reservation":
                error_code = "reservation_storage_full"
                message = (
                    "Não foi possível criar a reserva de recursos da VM porque o armazenamento "
                    "temporário do host está sem espaço livre. Libere espaço e tente novamente."
                )
            elif details.get("reason") == "resource_conflict":
                error_code = "resource_conflict"
                resources = ", ".join(str(item) for item in details.get("resources", [])) or "hardware"
                message = f"Outro processo já reservou recursos incompatíveis desta VM ({resources})."
            elif details.get("reason") == "network_port_in_use":
                error_code = "network_port_in_use"
                ports = ", ".join(str(port) for port in details.get("network_ports", []))
                message = f"A porta local {ports} já está ocupada no host e não pode receber o encaminhamento SSH desta VM. Escolha outra porta."
            else:
                error_code = "admission_failed"
                message = "A VM não foi admitida pela reserva de recursos."
            return LaunchResult(
                vm_id=vm.id,
                state="rejected",
                phase=phase,
                error_code=error_code,
                message=message,
                details=details,
            )

        try:
            emit("starting", {"vm_id": vm.id, "name": vm.name, "macos_version": vm.macos_version})
            found_reims = self.hooks.find_reims_dir()
            reims_dir = str(found_reims) if found_reims else None

            vulkan_ok, vulkan_message = self.hooks.vulkan_readiness()
            if not vulkan_ok:
                return self._reject(vm, phase, "vulkan_blocked", vulkan_message, interaction="return")

            if reims_dir and not self.hooks.ensure_release(reims_dir, vm.id):
                return self._reject(vm, phase, "release_unavailable", "Reims/QEMU não está pronto.", interaction="continue")

            custom_dir: Optional[Path] = None
            recovery_error = ""
            configured_dir = Path(vm.disk_path).expanduser().parent if vm.disk_path else None
            if (
                vm.disk_path
                and Path(vm.disk_path).expanduser().is_file()
                and configured_dir
                and (configured_dir / "OpenCore.qcow2").is_file()
            ):
                custom_dir = configured_dir
            else:
                custom_dir, recovery_error = self.hooks.recover_storage(vm)
                if custom_dir:
                    vm.disk_path = str(custom_dir / "macos.qcow2")
                    self.store.update_vm(vm)
                    emit("storage_recovered", {"vm_id": vm.id, "message": recovery_error})
                elif configured_dir and configured_dir.exists():
                    custom_dir = configured_dir

            preparation = prepare_install_launch(
                vm,
                force_installation=force_installation,
                custom_dir=custom_dir,
                config_dir=CONFIG_DIR,
                home=Path.home(),
            )
            policy = preparation.policy
            phase = policy.phase.value
            record_install_launch(vm, preparation)
            self.store.update_vm(vm)
            emit("install_policy", {
                "vm_id": vm.id,
                "phase": phase,
                "reason": policy.reason,
                "is_installing": policy.is_installing,
            })

            available, runtime_reason = self.hooks.decide_runtime(
                reims_dir, vm.macos_version, custom_dir
            )
            if not available:
                if recovery_error and "layout persistente incompleto" in runtime_reason:
                    runtime_reason = f"{runtime_reason}; {recovery_error}"
                return self._reject(vm, phase, "runtime_unavailable", runtime_reason, interaction="continue")

            env_vars = os.environ.copy()
            self.hooks.apply_graphics_environment(env_vars)
            self.hooks.apply_gpu_environment(vm, env_vars)
            installer_path = (
                preparation.installer_image
                if preparation.installer_image and policy.attach_installer
                else None
            )
            try:
                if self.hooks.write_vm_projection:
                    self.hooks.write_vm_projection(vm, installer=installer_path)
                else:
                    # Compatibility for injected hooks used by integrations and tests.
                    self.hooks.write_vm_config(vm, installer=installer_path)
                    self.hooks.ensure_registered(vm)
            except reims_ctl.GPUSelectionError as exc:
                return self._reject(vm, phase, "gpu_selection_failed", str(exc), interaction="continue")

            ctl_bin = self.hooks.ctl_binary()
            if not ctl_bin:
                return self._reject(vm, phase, "ctl_missing", "reims-ctl não encontrado.", interaction="continue")

            base_argv = [ctl_bin, "start", vm.id]
            try:
                start_argv, no_graphical_session = self.hooks.session_argv(base_argv, env_vars)
            except RuntimeError as exc:
                return self._reject(
                    vm,
                    phase,
                    "display_session_unavailable",
                    str(exc),
                    interaction="continue",
                )
            if no_graphical_session:
                emit("no_graphical_session", {"vm_id": vm.id})

            def relaunch_notice(step: int) -> None:
                emit("relaunching", {"vm_id": vm.id, "step": step})

            try:
                reset_count, last_reason = run_rail_session(
                    launch=lambda: self.hooks.session_factory(
                        start_argv, env_vars, vm, self.hooks.on_usb_ready
                    ),
                    make_monitor=lambda: self.hooks.monitor_factory(vm),
                    is_installing=policy.is_installing,
                    on_relaunch=relaunch_notice,
                )
            except LaunchSessionFailed as exc:
                # A non-zero `reims-ctl start` may still have forked QEMU. Never
                # report it as a successful lifecycle: preserve the process as
                # a duplicate-start guard when Reims still sees it, and let
                # the owning systemd unit reconcile its KillMode=control-group
                # when this supervisor returns failure.
                running, runtime_known = ExclusiveVMLock._running_reims_vms()
                process_still_running = vm.id in running if runtime_known else None
                details = {
                    "interaction": "continue",
                    "exit_code": exc.exit_code,
                    "runtime_state_known": runtime_known,
                    "process_still_running": process_still_running,
                }
                emit("launch_failed", {"vm_id": vm.id, **details})
                return LaunchResult(
                    vm_id=vm.id,
                    state="rejected",
                    phase=phase,
                    error_code="launch_failed",
                    message="O comando de inicialização do Reims terminou com erro.",
                    details=details,
                )
            from types import SimpleNamespace

            session = SimpleNamespace(
                reset_count=reset_count,
                last_reason=last_reason,
                shutdown_count=0,
            )
            self.hooks.apply_power_policy(vm, policy, session)
            self.hooks.post_phase_update(vm, policy)
            emit("finished", {"vm_id": vm.id, "reset_count": reset_count, "last_reason": last_reason})
            return LaunchResult(
                vm_id=vm.id,
                state="completed",
                phase=phase,
                reset_count=reset_count,
                last_reason=last_reason,
            )
        finally:
            lock.release()

    @staticmethod
    def _reject(
        vm: VMConfig,
        phase: str,
        code: str,
        message: str,
        *,
        interaction: str,
    ) -> LaunchResult:
        return LaunchResult(
            vm_id=vm.id,
            state="rejected",
            phase=phase,
            error_code=code,
            message=message,
            details={"interaction": interaction},
        )
