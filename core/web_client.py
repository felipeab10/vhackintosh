"""Small CLI/TUI client for the appliance's single local management agent."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping

from core.web_rpc import RpcError, UnixRpcClient
from core.config import VMConfig


DEFAULT_AGENT_SOCKET = "/run/vhackintosh-agent/agent.sock"
AGENT_SYSTEMD_UNITS = (
    Path("/etc/systemd/system/vhackintosh-agent.service"),
    Path("/usr/lib/systemd/system/vhackintosh-agent.service"),
)


class WebAgentUnavailable(RuntimeError):
    """The appliance is configured for the agent, but it cannot be reached."""

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


def agent_is_required() -> bool:
    """Whether this install is appliance-managed rather than a dev checkout."""
    configured = os.environ.get("VHACKINTOSH_AGENT_REQUIRED", "").strip().lower()
    return configured in {"1", "true", "yes"} or any(path.is_file() for path in AGENT_SYSTEMD_UNITS)


def agent_socket_path() -> str:
    return os.environ.get("VHACKINTOSH_AGENT_SOCKET", DEFAULT_AGENT_SOCKET)


def call_agent(method: str, payload: Mapping[str, Any] | None = None) -> Any:
    """Call the existing agent and turn transport failures into actionable text."""
    try:
        return UnixRpcClient(agent_socket_path(), timeout=3.0).call(method, payload)
    except RpcError as exc:
        code = getattr(exc, "code", None)
        if code == "capability_unavailable":
            raise WebAgentUnavailable(
                "O agente está ativo, mas o supervisor compartilhado de VM ainda não está disponível. "
                "A operação foi recusada; nenhuma VM foi iniciada ou parada.",
                code=code,
            ) from exc
        raise WebAgentUnavailable(
            f"O serviço de gerenciamento local está indisponível ({exc}). "
            "Verifique o serviço vhackintosh-agent e tente novamente.",
            code=code,
        ) from exc


def read_agent_catalog() -> tuple[list[VMConfig], str]:
    """Read the appliance VM catalog and its CAS revision from its owner."""
    result = call_agent("vms.list")
    if not isinstance(result, dict) or not isinstance(result.get("revision"), str):
        raise WebAgentUnavailable("O agente retornou um catálogo de VMs inválido.")
    items = result.get("items")
    if not isinstance(items, list):
        raise WebAgentUnavailable("O agente retornou uma lista de VMs inválida.")

    vms: list[VMConfig] = []
    try:
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("config"), dict):
                raise ValueError("registro sem configuração")
            config = dict(item["config"])
            # Revision belongs to the envelope, not VMConfig's extensible fields.
            config.pop("revision", None)
            if not isinstance(config.get("id"), str) or not isinstance(config.get("name"), str):
                raise ValueError("identificador ou nome ausente/inválido")
            vm = VMConfig.from_dict(config)
            if not vm.id.strip() or not vm.name.strip():
                raise ValueError("identificador ou nome vazio")
            vms.append(vm)
    except (TypeError, ValueError, KeyError) as exc:
        raise WebAgentUnavailable(f"O agente retornou uma configuração de VM inválida ({exc}).") from exc
    return vms, result["revision"]


def list_agent_vms() -> list[VMConfig]:
    """Read the appliance VM catalog from its owner, never a stale local cache."""
    return read_agent_catalog()[0]
