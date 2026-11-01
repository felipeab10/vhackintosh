"""HTTP API authorization and dependency-boundary tests."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from web.api.app import ActivationCode, SQLiteAuditStore, SQLiteAuthStore, _HostTelemetryStream, create_app
from core.web_rpc import RpcRemoteError
from core.telemetry import MetricSample
from core.web_agent import WebAgentService
from web.api.agent import RpcAgentClient
from web.api.contracts import (
    HostOverview,
    HostUsbInventory,
    CapabilitiesResponse,
    Capability,
    InstallPhase,
    JobAccepted,
    Operation,
    SmbiosConfig,
    VmRuntimeList,
    VmActionRequest,
    VmConfig,
    VmConfigEnvelope,
    VmConfigCreate,
    VmConfigUpdate,
    VmOpenCorePickerRequest,
    VmSmbiosDiagnosticRequest,
    VmDeleteResponse,
    VmListResponse,
    ReimsReleaseList,
    ReimsReleaseUpdateRequest,
    ReimsImportCandidateList,
    ReimsImportRequest,
)


def origin() -> dict[str, str]:
    return {"Origin": "http://testserver"}


class FakeAgent:
    def __init__(self):
        self.retention_hours = 24

    def is_connected(self) -> bool:
        return True

    def get_telemetry_settings(self):
        return {"history_retention_hours": self.retention_hours, "history_persistence_enabled": True}

    def set_telemetry_settings(self, history_retention_hours: int):
        self.retention_hours = history_retention_hours
        return self.get_telemetry_settings()

    def list_reims_releases(self):
        return ReimsReleaseList(
            repository="felipeab10/reims-vgpu", current_release="v1.0.0", active_release="v0.9.0",
            items=[{"tag": "v1.1.0", "name": "v1.1.0", "published_at": "2026-10-01T00:00:00Z", "asset_size_bytes": 1024}],
        )

    def update_reims_release(self, request: ReimsReleaseUpdateRequest):
        self.last_reims_release_request = request
        return JobAccepted(job_id="release-job", events_url="/api/v1/jobs/release-job")

    def host_overview(self) -> HostOverview:
        return HostOverview(
            hostname="appliance",
            architecture="x86_64",
            cpu_model="Test CPU",
            logical_cpus=8,
            memory_total_bytes=16 * 1024**3,
            memory_available_bytes=12 * 1024**3,
            uptime_seconds=60,
        )

    def runtime_capabilities(self) -> CapabilitiesResponse:
        return CapabilitiesResponse(
            runtime_fingerprint="a" * 64,
            generated_at=datetime.now(timezone.utc),
            items=[Capability(
                id="gpu.exact_selection", label="Seleção exata de GPU", supported=True,
                available=False, limitation="Sonda Vulkan indisponível.",
            )],
        )

    def list_vms(self) -> VmListResponse:
        return VmListResponse(items=[self.get_vm_config("vm-1")], revision="a" * 64)

    def list_reims_import_candidates(self) -> ReimsImportCandidateList:
        return ReimsImportCandidateList(items=[])

    def import_reims_vm(self, request: ReimsImportRequest, expected_revision: str) -> VmConfigEnvelope:
        self.last_import = (request, expected_revision)
        return self.get_vm_config("vm-1")

    def get_vm_config(self, vm_id: str) -> VmConfigEnvelope | None:
        if vm_id != "vm-1":
            return None
        config = VmConfig(
            id=vm_id,
            name="macOS",
            macos_version="tahoe",
            vcpus=4,
            ram_gb=8,
            disk_size_gb=64,
            smbios=SmbiosConfig(model="iMac20,1", serial_number="SECRET-SERIAL", board_serial="", smuuid="", rom=""),
            audio_device="usb-audio",
            fullscreen=True,
            opencore_show_picker=False,
            created_at="2026-10-01T00:00:00Z",
            install_phase=InstallPhase.PENDING,
            install_reboots=0,
            install_evidence="none",
            revision="vm-revision",
        )
        return VmConfigEnvelope(config=config, revision="b" * 64)

    def create_vm_config(self, config: VmConfigCreate, expected_revision: str) -> VmConfigEnvelope:
        self.last_create = (config, expected_revision)
        item = self.get_vm_config("vm-1")
        return item.model_copy(update={"revision": "c" * 64, "config": item.config.model_copy(update={"id": config.id})})

    def update_vm_config(self, vm_id: str, config: VmConfigUpdate, expected_revision: str) -> VmConfigEnvelope:
        self.last_update = (vm_id, config, expected_revision)
        item = self.get_vm_config("vm-1")
        return item.model_copy(update={
            "revision": "d" * 64,
            "config": item.config.model_copy(update={"name": config.name, "id": vm_id}),
        })

    def patch_vm_config(self, vm_id: str, fields: dict, expected_revision: str) -> VmConfigEnvelope:
        if getattr(self, "reject_vm_patch", False):
            raise RpcRemoteError("vm_running", "Pare a VM antes de alterar a configuração.")
        self.last_patch = (vm_id, fields, expected_revision)
        item = self.get_vm_config(vm_id)
        patched = VmConfig.model_validate({**item.config.model_dump(mode="json"), **fields})
        return VmConfigEnvelope.model_validate({"revision": "e" * 64, "config": patched})

    def update_vm_opencore_picker(self, vm_id: str, request: VmOpenCorePickerRequest, expected_revision: str) -> JobAccepted:
        self.last_picker_update = (vm_id, request, expected_revision)
        return JobAccepted(job_id="picker-job", events_url="/api/v1/jobs/picker-job")

    def diagnose_vm_smbios(self, vm_id: str, request: VmSmbiosDiagnosticRequest, expected_revision: str) -> JobAccepted:
        self.last_smbios_diagnostic = (vm_id, request, expected_revision)
        return JobAccepted(job_id="smbios-job", events_url="/api/v1/jobs/smbios-job")

    def delete_vm_config(self, vm_id: str, expected_revision: str, delete_disk: bool = False) -> VmDeleteResponse:
        self.last_delete = (vm_id, expected_revision, delete_disk)
        if delete_disk and getattr(self, "reject_disk_delete", False):
            raise RpcRemoteError("disk_delete_forbidden", "O disco compartilhado foi preservado.")
        return VmDeleteResponse(id=vm_id, deleted=True, revision="c" * 64)

    def vm_action(self, vm_id: str, action: VmActionRequest) -> JobAccepted:
        if getattr(self, "reject_action", False):
            raise RpcRemoteError("capability_unavailable", "unsupported")
        return JobAccepted(job_id=f"job-{vm_id}", events_url=f"/api/v1/jobs/job-{vm_id}/events")

    def provision_vm(self, request):
        self.last_provision = request
        return JobAccepted(job_id="provision-job", events_url="/api/v1/jobs/provision-job")

    def get_job(self, job_id):
        if job_id != "provision-job":
            return None
        from web.api.contracts import JobDetail

        return JobDetail(
            id=job_id, kind="vm.provision", state="running", stage="disk_opencore",
            progress_percent=50, created_at="2026-10-01T00:00:00Z", updated_at="2026-10-01T00:00:01Z",
            cancellable=True,
        )

    def list_jobs(self, limit=25, offset=0, states=None):
        from web.api.contracts import JobListResponse

        item = JobListResponse(items=[], limit=limit, offset=offset, total=0)
        self.last_job_list = (limit, offset, states)
        return item

    def cancel_job(self, job_id):
        return self.get_job(job_id)

    def retry_job(self, job_id):
        if job_id != "provision-job":
            return None
        return JobAccepted(job_id=job_id, events_url=f"/api/v1/jobs/{job_id}/events")

    def host_gpus(self):
        return []

    def host_usb_inventory(self):
        return HostUsbInventory(items=[])

    def storage_inventory(self):
        return {"items": [], "free_bytes": 2048, "managed_volume": "Volume das VMs gerenciadas"}

    def import_media_transfer(self, transfer_id):
        return JobAccepted(job_id=f"media-{transfer_id}", events_url=f"/api/v1/jobs/media-{transfer_id}")

    def convert_managed_media(self, media_id, request):
        self.last_media_conversion = (media_id, request)
        return JobAccepted(job_id=f"convert-{media_id}", events_url=f"/api/v1/jobs/convert-{media_id}")

    def download_apple_recovery(self, request):
        self.last_apple_download = request
        return JobAccepted(job_id="apple-recovery-job", events_url="/api/v1/jobs/apple-recovery-job")

    def create_vm_backup(self, vm_id, request):
        self.last_backup = (vm_id, request)
        return JobAccepted(job_id=f"backup-{vm_id}", events_url=f"/api/v1/jobs/backup-{vm_id}")

    def verify_vm_backup(self, vm_id, backup_id, request):
        self.last_backup_verification = (vm_id, backup_id, request)
        return JobAccepted(job_id=f"verify-{vm_id}", events_url=f"/api/v1/jobs/verify-{vm_id}")

    def restore_vm_backup(self, vm_id, backup_id, request):
        self.last_backup_restore = (vm_id, backup_id, request)
        return JobAccepted(job_id=f"restore-{vm_id}", events_url=f"/api/v1/jobs/restore-{vm_id}")

    def clone_vm(self, vm_id, request):
        self.last_clone = (vm_id, request)
        return JobAccepted(job_id=f"clone-{vm_id}", events_url=f"/api/v1/jobs/clone-{vm_id}")

    def list_vm_backups(self, vm_id):
        return {"vm_id": vm_id, "items": []}

    def host_metrics(self):
        return {"samples": []}

    def host_metrics_history(self, name, labels, resolution_seconds, since, until, limit):
        self.last_history_request = (name, labels, resolution_seconds, since, until, limit)
        return {"name": name, "resolution_seconds": resolution_seconds, "points": []}

    def get_vm_runtime(self, vm_id: str):
        return {"id": vm_id, "state": "stopped"}

    def get_vm_serial_console(self, vm_id: str):
        return {"vm_id": vm_id, "available": False, "content": "", "truncated": False, "message": "Sem saída serial."}

    def list_vm_runtimes(self):
        return VmRuntimeList.model_validate({
            "items": [{
                "id": "vm-1", "state": "stopped", "qmp_available": False,
                "install_phase": "pending", "observed_at": datetime.now(timezone.utc),
            }],
            "observed_at": datetime.now(timezone.utc),
        })

    def get_vm_metrics(self, vm_id: str):
        self.vm_metric_calls = getattr(self, "vm_metric_calls", 0) + 1
        return {"vm_id": vm_id, "samples": []}

    def get_vm_metrics_history(self, vm_id, name, labels, resolution_seconds, since, until, limit):
        if self.get_vm_config(vm_id) is None:
            return None
        return {"vm_id": vm_id, "name": name, "resolution_seconds": resolution_seconds, "points": []}


class InProcessAgentRpc:
    """Exercise the same allowlisted RPC handlers without opening a socket."""

    def __init__(self, handlers):
        self.handlers = handlers

    def call(self, method, payload=None):
        return self.handlers[method](payload or {})


class WebApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "auth.sqlite"
        self.activation = ActivationCode("activation-code-that-is-long")
        app = create_app(
            agent=FakeAgent(),
            auth_store=SQLiteAuthStore(self.db),
            activation_code=self.activation,
            cookie_secure=False,
            web_port=9443,
            tls_fingerprint="A" * 64,
        )
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()
        self.temp.cleanup()

    def test_per_vm_runtime_route_publishes_the_typed_response_contract(self):
        operation = self.client.app.openapi()["paths"]["/api/v1/vms/{vm_id}/runtime"]["get"]
        response_schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
        self.assertEqual(response_schema["$ref"], "#/components/schemas/VmRuntime")

    def test_per_vm_serial_route_is_authenticated_and_typed(self):
        operation = self.client.app.openapi()["paths"]["/api/v1/vms/{vm_id}/serial"]["get"]
        response_schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
        self.assertEqual(response_schema["$ref"], "#/components/schemas/VmSerialConsole")
        self.assertEqual(self.client.get("/api/v1/vms/vm-1/serial").status_code, 401)
        self.bootstrap()
        response = self.client.get("/api/v1/vms/vm-1/serial")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["available"])
        self.assertEqual(response.json()["vm_id"], "vm-1")

    def test_openapi_documents_the_runtime_api_error_envelope(self):
        paths = self.client.app.openapi()["paths"]
        operations = [
            (path, method, operation)
            for path, path_item in paths.items()
            if path.startswith("/api/v1/")
            for method, operation in path_item.items()
            if method in {"get", "post", "put", "patch", "delete", "options", "head"}
        ]
        self.assertTrue(operations)
        for path, method, operation in operations:
            with self.subTest(path=path, method=method):
                responses = operation["responses"]
                self.assertEqual(
                    responses["500"]["content"]["application/json"]["schema"],
                    {"$ref": "#/components/schemas/ApiError"},
                )
                if "422" in responses:
                    self.assertEqual(
                        responses["422"]["content"]["application/json"]["schema"],
                        {"$ref": "#/components/schemas/ApiError"},
                    )
                for status_code in ("200", "201", "202"):
                    success = responses.get(status_code, {})
                    json_body = success.get("content", {}).get("application/json")
                    if json_body is not None:
                        self.assertIn("schema", json_body)

    def bootstrap(self) -> None:
        response = self.client.post(
            "/api/v1/session/bootstrap",
            headers=origin(),
            json={"code": "activation-code-that-is-long", "username": "admin", "password": "correct horse battery"},
        )
        self.assertEqual(response.status_code, 200, response.text)

    def test_activation_is_one_time_and_creates_protected_session(self) -> None:
        self.bootstrap()
        self.assertIn("vh_session", self.client.cookies)
        self.assertIn("vh_csrf", self.client.cookies)
        self.assertEqual(self.client.get("/api/v1/host").status_code, 200)
        second = self.client.post(
            "/api/v1/session/bootstrap",
            headers=origin(),
            json={"code": "activation-code-that-is-long", "username": "second", "password": "a different long password"},
        )
        self.assertEqual(second.status_code, 409)

    def test_storage_inventory_requires_session_and_returns_typed_data(self) -> None:
        self.assertEqual(self.client.get("/api/v1/storage").status_code, 401)
        self.bootstrap()
        response = self.client.get("/api/v1/storage")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "items": [], "free_bytes": 2048, "managed_volume": "Volume das VMs gerenciadas",
        })

    def test_media_import_is_authenticated_csrf_protected_and_audited(self) -> None:
        transfer_id = "a" * 32
        self.assertEqual(self.client.post(f"/api/v1/transfers/{transfer_id}/import").status_code, 401)
        self.bootstrap()
        csrf_headers = {**origin(), "X-CSRF-Token": self.client.cookies.get("vh_csrf")}
        self.assertEqual(self.client.post(f"/api/v1/transfers/{transfer_id}/import").status_code, 403)
        response = self.client.post(f"/api/v1/transfers/{transfer_id}/import", headers=csrf_headers)
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["job_id"], f"media-{transfer_id}")
        event = self.client.app.state.audit_store.recent(1)[0]
        self.assertEqual((event["action"], event["outcome"]), ("transfer.import", 202))
        malformed = self.client.post("/api/v1/transfers/../import", headers=csrf_headers)
        self.assertIn(malformed.status_code, (404, 422))

    def test_media_conversion_is_authenticated_csrf_protected_and_audited(self) -> None:
        media_id = "b" * 32
        payload = {"output_format": "qcow2", "idempotency_key": "12345678-1234-4234-8234-123456789abc"}
        self.assertEqual(self.client.post(f"/api/v1/storage/{media_id}/convert", json=payload).status_code, 401)
        self.bootstrap()
        headers = {**origin(), "X-CSRF-Token": self.client.cookies.get("vh_csrf")}
        self.assertEqual(self.client.post(f"/api/v1/storage/{media_id}/convert", headers=origin(), json=payload).status_code, 403)
        response = self.client.post(f"/api/v1/storage/{media_id}/convert", headers=headers, json=payload)
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["job_id"], f"convert-{media_id}")
        self.assertEqual(self.client.app.state.agent.last_media_conversion[0], media_id)
        event = self.client.app.state.audit_store.recent(1)[0]
        self.assertEqual((event["action"], event["outcome"]), ("storage.media.convert", 202))
        invalid = self.client.post(f"/api/v1/storage/{media_id}/convert", headers=headers, json={**payload, "output_format": "iso"})
        self.assertEqual(invalid.status_code, 422)

    def test_apple_recovery_download_is_authenticated_csrf_protected_and_audited(self) -> None:
        payload = {"macos_version": "sequoia", "idempotency_key": "12345678-1234-4234-8234-123456789abc"}
        self.assertEqual(self.client.post("/api/v1/storage/apple-recovery", json=payload).status_code, 401)
        self.bootstrap()
        headers = {**origin(), "X-CSRF-Token": self.client.cookies.get("vh_csrf")}
        self.assertEqual(self.client.post("/api/v1/storage/apple-recovery", headers=origin(), json=payload).status_code, 403)
        response = self.client.post("/api/v1/storage/apple-recovery", headers=headers, json=payload)
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["job_id"], "apple-recovery-job")
        self.assertEqual(self.client.app.state.agent.last_apple_download.macos_version, "sequoia")
        event = self.client.app.state.audit_store.recent(1)[0]
        self.assertEqual((event["action"], event["outcome"]), ("storage.apple.download", 202))
        invalid = self.client.post("/api/v1/storage/apple-recovery", headers=headers, json={**payload, "macos_version": "unknown"})
        self.assertEqual(invalid.status_code, 422)

    def test_vm_backup_is_authenticated_csrf_protected_and_audited(self) -> None:
        self.assertEqual(self.client.post("/api/v1/vms/vm-1/backups").status_code, 401)
        self.bootstrap()
        headers = {**origin(), "X-CSRF-Token": self.client.cookies.get("vh_csrf")}
        payload = {"idempotency_key": "12345678-1234-4234-8234-123456789abc"}
        self.assertEqual(self.client.post("/api/v1/vms/vm-1/backups", headers=origin(), json=payload).status_code, 403)
        response = self.client.post("/api/v1/vms/vm-1/backups", headers=headers, json=payload)
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["job_id"], "backup-vm-1")
        event = self.client.app.state.audit_store.recent(1)[0]
        self.assertEqual((event["action"], event["outcome"]), ("vm.backup", 202))
        invalid = self.client.post("/api/v1/vms/bad%2Fid/backups", headers=headers, json=payload)
        self.assertIn(invalid.status_code, (404, 422))

    def test_vm_backup_inventory_requires_session(self) -> None:
        self.assertEqual(self.client.get("/api/v1/vms/vm-1/backups").status_code, 401)
        self.bootstrap()
        response = self.client.get("/api/v1/vms/vm-1/backups")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"vm_id": "vm-1", "items": []})

    def test_backup_verification_is_authenticated_csrf_protected_and_audited(self) -> None:
        backup_id = "a" * 32 + ".vhb"
        path = f"/api/v1/vms/vm-1/backups/{backup_id}/verify"
        payload = {"idempotency_key": "12345678-1234-4234-8234-123456789abc"}
        self.assertEqual(self.client.post(path, json=payload).status_code, 401)
        self.bootstrap()
        headers = {**origin(), "X-CSRF-Token": self.client.cookies.get("vh_csrf")}
        self.assertEqual(self.client.post(path, headers=origin(), json=payload).status_code, 403)
        response = self.client.post(path, headers=headers, json=payload)
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["job_id"], "verify-vm-1")
        self.assertEqual(self.client.app.state.audit_store.recent(1)[0]["action"], "vm.backup.verify")
        invalid = self.client.post("/api/v1/vms/vm-1/backups/not-a-backup/verify", headers=headers, json=payload)
        self.assertEqual(invalid.status_code, 422)

    def test_backup_restore_is_authenticated_csrf_protected_and_audited(self) -> None:
        backup_id = "b" * 32 + ".vhb"
        path = f"/api/v1/vms/vm-1/backups/{backup_id}/restore"
        payload = {"idempotency_key": "12345678-1234-4234-8234-123456789abc", "confirmation": f"vm-1:{backup_id}"}
        self.assertEqual(self.client.post(path, json=payload).status_code, 401)
        self.bootstrap()
        headers = {**origin(), "X-CSRF-Token": self.client.cookies.get("vh_csrf")}
        self.assertEqual(self.client.post(path, headers=origin(), json=payload).status_code, 403)
        response = self.client.post(path, headers=headers, json=payload)
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["job_id"], "restore-vm-1")
        self.assertEqual(self.client.app.state.audit_store.recent(1)[0]["action"], "vm.backup.restore")

    def test_vm_clone_is_authenticated_csrf_protected_and_audited(self) -> None:
        path = "/api/v1/vms/vm-1/clone"
        payload = {
            "idempotency_key": "12345678-1234-4234-8234-123456789abc",
            "clone_id": "vm-1-copy", "name": "VM copy", "confirmation": "vm-1:vm-1-copy",
        }
        self.assertEqual(self.client.post(path, json=payload).status_code, 401)
        self.bootstrap()
        headers = {**origin(), "X-CSRF-Token": self.client.cookies.get("vh_csrf")}
        self.assertEqual(self.client.post(path, headers=origin(), json=payload).status_code, 403)
        response = self.client.post(path, headers=headers, json=payload)
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["job_id"], "clone-vm-1")
        self.assertEqual(self.client.app.state.audit_store.recent(1)[0]["action"], "vm.clone")

    def test_failed_bootstrap_and_login_attempts_are_audited_without_credentials(self) -> None:
        failed_bootstrap = self.client.post(
            "/api/v1/session/bootstrap",
            headers=origin(),
            json={"code": "wrong-activation-code", "username": "admin", "password": "private-password-value"},
        )
        self.assertEqual(failed_bootstrap.status_code, 401)
        bootstrap_event = self.client.app.state.audit_store.recent(1)[0]
        self.assertEqual((bootstrap_event["action"], bootstrap_event["outcome"]), ("session.bootstrap", 401))
        self.assertNotIn("wrong-activation-code", str(bootstrap_event))
        self.assertNotIn("private-password-value", str(bootstrap_event))

        self.bootstrap()
        failed_login = self.client.post(
            "/api/v1/session/login",
            headers=origin(),
            json={"username": "admin", "password": "private-password-value"},
        )
        self.assertEqual(failed_login.status_code, 401)
        login_event = self.client.app.state.audit_store.recent(1)[0]
        self.assertEqual((login_event["action"], login_event["outcome"]), ("session.login", 401))
        self.assertNotIn("private-password-value", str(login_event))

    def test_csrf_and_origin_are_required_for_logout(self) -> None:
        self.bootstrap()
        self.assertEqual(self.client.post("/api/v1/session/logout").status_code, 403)
        csrf = self.client.cookies.get("vh_csrf")
        response = self.client.post("/api/v1/session/logout", headers={**origin(), "X-CSRF-Token": csrf})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.client.get("/api/v1/host").status_code, 401)

    def test_password_change_rotates_credentials_and_revokes_other_sessions(self) -> None:
        self.bootstrap()
        other = TestClient(self.client.app)
        try:
            login = other.post(
                "/api/v1/session/login", headers=origin(),
                json={"username": "admin", "password": "correct horse battery"},
            )
            self.assertEqual(login.status_code, 200, login.text)
            csrf_headers = {**origin(), "X-CSRF-Token": self.client.cookies.get("vh_csrf")}
            response = self.client.post(
                "/api/v1/session/password", headers=csrf_headers,
                json={"current_password": "correct horse battery", "new_password": "new secure password"},
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["username"], "admin")
            self.assertEqual(self.client.get("/api/v1/host").status_code, 200)
            self.assertEqual(other.get("/api/v1/host").status_code, 401)

            old_login = TestClient(self.client.app)
            new_login = TestClient(self.client.app)
            try:
                self.assertEqual(old_login.post(
                    "/api/v1/session/login", headers=origin(),
                    json={"username": "admin", "password": "correct horse battery"},
                ).status_code, 401)
                self.assertEqual(new_login.post(
                    "/api/v1/session/login", headers=origin(),
                    json={"username": "admin", "password": "new secure password"},
                ).status_code, 200)
            finally:
                old_login.close()
                new_login.close()
            audit = next(
                event for event in self.client.app.state.audit_store.recent(10)
                if event["action"] == "session.password_change"
            )
            self.assertEqual((audit["action"], audit["outcome"]), ("session.password_change", 200))
            self.assertNotIn("new secure password", str(audit))
        finally:
            other.close()

    def test_password_change_requires_csrf_and_validates_current_password(self) -> None:
        self.bootstrap()
        payload = {"current_password": "wrong password", "new_password": "new secure password"}
        self.assertEqual(self.client.post("/api/v1/session/password", json=payload).status_code, 403)
        csrf_headers = {**origin(), "X-CSRF-Token": self.client.cookies.get("vh_csrf")}
        wrong = self.client.post("/api/v1/session/password", headers=csrf_headers, json=payload)
        self.assertEqual(wrong.status_code, 401)
        self.assertEqual(wrong.json()["code"], "invalid_current_password")
        unchanged = self.client.post(
            "/api/v1/session/password", headers=csrf_headers,
            json={"current_password": "correct horse battery", "new_password": "correct horse battery"},
        )
        self.assertEqual(unchanged.status_code, 422)
        self.assertEqual(self.client.post("/api/v1/session/login", headers=origin(),
            json={"username": "admin", "password": "correct horse battery"}).status_code, 200)

    def test_telemetry_retention_settings_require_auth_csrf_and_are_audited(self) -> None:
        self.assertEqual(self.client.get("/api/v1/settings/telemetry").status_code, 401)
        self.bootstrap()
        current = self.client.get("/api/v1/settings/telemetry")
        self.assertEqual(current.status_code, 200, current.text)
        self.assertEqual(current.json()["history_retention_hours"], 24)
        self.assertTrue(current.json()["history_persistence_enabled"])
        payload = {"history_retention_hours": 6}
        self.assertEqual(self.client.put("/api/v1/settings/telemetry", headers=origin(), json=payload).status_code, 403)
        headers = {**origin(), "X-CSRF-Token": self.client.cookies.get("vh_csrf")}
        updated = self.client.put("/api/v1/settings/telemetry", headers=headers, json=payload)
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(updated.json()["history_retention_hours"], 6)
        invalid = self.client.put("/api/v1/settings/telemetry", headers=headers, json={"history_retention_hours": 7})
        self.assertEqual(invalid.status_code, 422)
        audit = next(event for event in self.client.app.state.audit_store.recent(10)
                     if event["action"] == "telemetry.retention.set")
        self.assertEqual(audit["outcome"], 200)

    def test_host_alerts_require_auth_and_acknowledgement_is_csrf_protected_audited(self) -> None:
        self.assertEqual(self.client.get("/api/v1/host/alerts").status_code, 401)
        self.bootstrap()
        agent = self.client.app.state.agent
        agent.host_metrics = lambda: {"samples": [{
            "name": "host.cpu_percent", "value": 95, "timestamp": datetime.now(timezone.utc).isoformat(),
            "unit": "%", "source": "procfs", "availability": "available", "labels": {"path": "/secret"},
        }]}
        self.assertEqual(self.client.get("/api/v1/host/metrics").status_code, 200)
        alert_response = self.client.get("/api/v1/host/alerts")
        self.assertEqual(alert_response.status_code, 200, alert_response.text)
        alert = alert_response.json()["items"][0]
        self.assertEqual(alert["code"], "cpu.high")
        self.assertNotIn("secret", str(alert))
        endpoint = f"/api/v1/host/alerts/{alert['id']}/acknowledge"
        self.assertEqual(self.client.post(endpoint, headers=origin()).status_code, 403)
        headers = {**origin(), "X-CSRF-Token": self.client.cookies.get("vh_csrf")}
        self.assertEqual(self.client.post(endpoint, headers=headers).status_code, 204)
        self.assertIsNotNone(self.client.get("/api/v1/host/alerts").json()["items"][0]["acknowledged_at"])
        event = self.client.app.state.audit_store.recent(1)[0]
        self.assertEqual((event["action"], event["outcome"]), ("host.alert.acknowledge", 204))

    def test_agent_backed_host_and_vm_reads_work(self) -> None:
        self.bootstrap()
        self.assertEqual(self.client.get("/api/v1/host").json()["hostname"], "appliance")
        host = self.client.get("/api/v1/host").json()
        self.assertEqual(host["web_port"], 9443)
        self.assertEqual(host["tls_fingerprint"], "A" * 64)
        self.assertEqual(self.client.get("/api/v1/vms").json()["items"][0]["config"]["id"], "vm-1")
        self.assertEqual(self.client.get("/api/v1/vms/runtime").json()["items"][0]["state"], "stopped")
        self.assertEqual(self.client.get("/api/v1/vms/vm-1/config").status_code, 200)
        self.assertEqual(self.client.get("/api/v1/vms/absent/config").status_code, 404)

    def test_runtime_capabilities_require_auth_and_return_typed_unavailability_reason(self) -> None:
        self.assertEqual(self.client.get("/api/v1/capabilities").status_code, 401)
        self.bootstrap()
        response = self.client.get("/api/v1/capabilities")
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["runtime_fingerprint"], "a" * 64)
        self.assertEqual(payload["items"][0]["id"], "gpu.exact_selection")
        self.assertFalse(payload["items"][0]["available"])
        self.assertEqual(payload["items"][0]["limitation"], "Sonda Vulkan indisponível.")

    def test_vm_delete_requires_csrf_confirmation_and_catalog_revision(self) -> None:
        self.bootstrap()
        csrf_headers = {**origin(), "X-CSRF-Token": self.client.cookies.get("vh_csrf")}
        unauthenticated_mutation = self.client.request(
            "DELETE", "/api/v1/vms/vm-1", headers=origin(), json={"confirmation": "vm-1"},
        )
        self.assertEqual(unauthenticated_mutation.status_code, 403)
        bad_confirmation = self.client.request(
            "DELETE", "/api/v1/vms/vm-1", headers=csrf_headers, json={"confirmation": "wrong"},
        )
        self.assertEqual(bad_confirmation.status_code, 422)
        missing_revision = self.client.request(
            "DELETE", "/api/v1/vms/vm-1", headers=csrf_headers, json={"confirmation": "vm-1"},
        )
        self.assertEqual(missing_revision.status_code, 428)
        deleted = self.client.request(
            "DELETE", "/api/v1/vms/vm-1",
            headers={**csrf_headers, "If-Match": '"' + "a" * 64 + '"'},
            json={"confirmation": "vm-1"},
        )
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertEqual(deleted.json(), {"id": "vm-1", "deleted": True, "revision": "c" * 64})
        self.assertEqual(self.client.app.state.agent.last_delete, ("vm-1", '"' + "a" * 64 + '"', False))

    def test_shared_disk_delete_conflict_is_reported_as_safe_http_conflict(self) -> None:
        self.bootstrap()

        def reject_shared_disk(_vm_id: str, _revision: str, _delete_disk: bool = False):
            raise RpcRemoteError("disk_shared", "O disco também está associado à VM Test; ele foi preservado.")

        self.client.app.state.agent.delete_vm_config = reject_shared_disk
        response = self.client.request(
            "DELETE", "/api/v1/vms/vm-1",
            headers={
                **origin(),
                "X-CSRF-Token": self.client.cookies.get("vh_csrf"),
                "If-Match": '"' + "a" * 64 + '"',
            },
            json={"confirmation": "vm-1", "delete_disk": True},
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "disk_shared")
        self.assertIn("preservado", response.json()["message"])

    def test_vm_delete_surfaces_a_refused_disk_delete_reason(self) -> None:
        self.bootstrap()
        self.client.app.state.agent.reject_disk_delete = True
        headers = {
            **origin(), "X-CSRF-Token": self.client.cookies.get("vh_csrf"),
            "If-Match": '"' + "a" * 64 + '"',
        }
        response = self.client.request(
            "DELETE", "/api/v1/vms/vm-1", headers=headers,
            json={"confirmation": "vm-1", "delete_disk": True},
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], "disk_delete_forbidden")
        self.assertEqual(response.json()["message"], "O disco compartilhado foi preservado.")

    def test_private_transfers_require_session_and_csrf_and_stream_verified_content(self) -> None:
        self.assertEqual(self.client.get("/api/v1/transfers").status_code, 401)
        self.assertEqual(
            self.client.post(
                "/api/v1/transfers",
                headers={"Content-Type": "application/octet-stream", "X-Transfer-Filename": "recovery.iso", "X-Transfer-Format": "iso"},
                content=b"private bytes",
            ).status_code,
            401,
        )
        self.bootstrap()
        payload = bytearray(32775)
        payload[16 * 2048 : 16 * 2048 + 7] = b"\x01CD001\x01"
        upload_headers = {
            **origin(),
            "X-CSRF-Token": self.client.cookies.get("vh_csrf"),
            "Content-Type": "application/octet-stream",
            "X-Transfer-Filename": "recovery.iso",
            "X-Transfer-Format": "iso",
        }
        denied = self.client.post("/api/v1/transfers", headers={**origin(), "Content-Type": "application/octet-stream"}, content=bytes(payload))
        self.assertEqual(denied.status_code, 403)
        uploaded = self.client.post("/api/v1/transfers", headers=upload_headers, content=bytes(payload))
        self.assertEqual(uploaded.status_code, 201, uploaded.text)
        item = uploaded.json()
        self.assertEqual((item["filename"], item["format"], item["size_bytes"]), ("recovery.iso", "iso", len(payload)))
        self.assertEqual(self.client.get("/api/v1/transfers").json()["items"], [item])
        with TestClient(self.client.app) as anonymous:
            self.assertEqual(anonymous.get(f"/api/v1/transfers/{item['id']}/content").status_code, 401)
        downloaded = self.client.get(f"/api/v1/transfers/{item['id']}/content")
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(downloaded.content, bytes(payload))
        self.assertEqual(downloaded.headers["content-disposition"], 'attachment; filename="recovery.iso"')
        self.assertEqual(self.client.get("/api/v1/transfers/../content").status_code, 404)
        csrf = self.client.cookies.get("vh_csrf")
        self.assertEqual(self.client.delete(f"/api/v1/transfers/{item['id']}").status_code, 403)
        deleted = self.client.delete(
            f"/api/v1/transfers/{item['id']}", headers={**origin(), "X-CSRF-Token": csrf}
        )
        self.assertEqual(deleted.status_code, 204)
        self.assertEqual(self.client.get(f"/api/v1/transfers/{item['id']}/content").status_code, 404)

    def test_session_exposes_the_local_tls_fingerprint(self) -> None:
        response = self.client.get("/api/v1/session")
        self.assertTrue(response.json()["bootstrap_required"])
        self.assertEqual(response.json()["tls_fingerprint"], "A" * 64)

    def test_agent_backed_metrics_and_host_device_inventory_routes(self) -> None:
        self.bootstrap()
        self.assertEqual(self.client.get("/api/v1/host/metrics").json(), {"samples": []})
        self.assertEqual(self.client.get("/api/v1/host/gpus").json(), {"items": [], "stale": False, "message": None})
        self.assertEqual(self.client.get("/api/v1/host/usb").json(), {"items": [], "stale": False, "message": None})
        self.assertEqual(self.client.get("/api/v1/vms/vm-1/metrics").json(), {"vm_id": "vm-1", "samples": [], "message": None})

    def test_host_metrics_stream_requires_session_and_emits_unbuffered_snapshot(self) -> None:
        self.assertEqual(self.client.get("/api/v1/host/metrics/stream").status_code, 401)
        self.bootstrap()
        with self.client.stream("GET", "/api/v1/host/metrics/stream") as response:
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["content-type"], "text/event-stream; charset=utf-8")
            self.assertEqual(response.headers["x-accel-buffering"], "no")
            import json
            event = next(line for line in response.iter_lines() if line.startswith("data: "))
            payload = json.loads(event[6:])
            self.assertEqual(payload["sequence"], 0)
            self.assertTrue(payload["epoch"])
            self.assertEqual(payload["data"], {"samples": []})

    def test_vm_metrics_stream_requires_session_valid_vm_and_emits_snapshot(self) -> None:
        self.assertEqual(self.client.get("/api/v1/vms/vm-1/metrics/stream").status_code, 401)
        self.bootstrap()
        self.assertEqual(self.client.get("/api/v1/vms/missing/metrics/stream").status_code, 404)
        with self.client.stream("GET", "/api/v1/vms/vm-1/metrics/stream") as response:
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["content-type"], "text/event-stream; charset=utf-8")
            import json
            event = next(line for line in response.iter_lines() if line.startswith("data: "))
            payload = json.loads(event[6:])
            self.assertEqual(payload["sequence"], 0)
            self.assertTrue(payload["epoch"])
            self.assertEqual(payload["data"], {"vm_id": "vm-1", "samples": []})
        self.assertEqual(self.client.app.state.agent.vm_metric_calls, 1)
        self.assertEqual(self.client.app.state.vm_telemetry_streams, {})

    def test_jobs_list_requires_session_and_validates_bounded_filters(self) -> None:
        self.assertEqual(self.client.get("/api/v1/jobs").status_code, 401)
        self.bootstrap()
        listed = self.client.get("/api/v1/jobs?limit=10&offset=5&state=running&state=queued")
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(listed.json(), {"items": [], "limit": 10, "offset": 5, "total": 0})
        self.assertEqual(self.client.app.state.agent.last_job_list, (10, 5, ["running", "queued"]))
        self.assertEqual(self.client.get("/api/v1/jobs?limit=101").status_code, 422)
        self.assertEqual(self.client.get("/api/v1/jobs?state=unknown").status_code, 422)

    def test_install_phase_confirmation_is_target_bound_versioned_and_audited(self) -> None:
        path = "/api/v1/vms/vm-1/install/confirm"
        payload = {"confirmation": "vm-1"}
        self.assertEqual(self.client.post(path, json=payload).status_code, 401)
        self.bootstrap()
        headers = {**origin(), "X-CSRF-Token": self.client.cookies.get("vh_csrf")}
        self.assertEqual(self.client.post(path, headers=headers, json=payload).status_code, 428)
        bad_target = self.client.post(path, headers={**headers, "If-Match": '"' + "b" * 64 + '"'}, json={"confirmation": "other"})
        self.assertEqual(bad_target.status_code, 422)
        self.assertFalse(hasattr(self.client.app.state.agent, "last_patch"))

        current = self.client.get("/api/v1/vms/vm-1/config")
        confirmed = self.client.post(
            path,
            headers={**headers, "If-Match": current.headers["etag"]},
            json=payload,
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        self.assertEqual(confirmed.json()["config"]["install_phase"], "installed")
        self.assertEqual(confirmed.json()["config"]["install_evidence"], "complete")
        self.assertEqual(confirmed.headers["etag"], f'"{"e" * 64}"')
        self.assertEqual(self.client.app.state.agent.last_patch, (
            "vm-1", {"install_phase": "installed", "install_evidence": "complete"}, current.headers["etag"],
        ))
        audit = next(event for event in self.client.app.state.audit_store.recent(10)
                     if event["action"] == "vm.install.confirm")
        self.assertEqual((audit["target"], audit["outcome"]), ("vm-1", 200))

    def test_install_phase_confirmation_refuses_a_running_vm(self) -> None:
        self.bootstrap()
        self.client.app.state.agent.reject_vm_patch = True
        current = self.client.get("/api/v1/vms/vm-1/config")
        response = self.client.post(
            "/api/v1/vms/vm-1/install/confirm",
            headers={
                **origin(),
                "X-CSRF-Token": self.client.cookies.get("vh_csrf"),
                "If-Match": current.headers["etag"],
            },
            json={"confirmation": "vm-1"},
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "vm_running")

    def test_opencore_picker_update_requires_target_revision_and_returns_audited_job(self) -> None:
        path = "/api/v1/vms/vm-1/opencore/picker"
        payload = {"boot_selection": True, "confirmation": "vm-1", "idempotency_key": "00000000-0000-4000-8000-000000000001"}
        self.assertEqual(self.client.post(path, json=payload).status_code, 401)
        self.bootstrap()
        headers = {**origin(), "X-CSRF-Token": self.client.cookies.get("vh_csrf")}
        self.assertEqual(self.client.post(path, headers=headers, json=payload).status_code, 428)
        invalid = self.client.post(path, headers={**headers, "If-Match": '"' + "b" * 64 + '"'}, json={**payload, "confirmation": "wrong"})
        self.assertEqual(invalid.status_code, 422)
        self.assertFalse(hasattr(self.client.app.state.agent, "last_picker_update"))
        current = self.client.get("/api/v1/vms/vm-1/config")
        accepted = self.client.post(path, headers={**headers, "If-Match": current.headers["etag"]}, json=payload)
        self.assertEqual(accepted.status_code, 202, accepted.text)
        self.assertEqual(accepted.json()["job_id"], "picker-job")
        vm_id, request, revision = self.client.app.state.agent.last_picker_update
        self.assertEqual(vm_id, "vm-1")
        self.assertTrue(request.boot_selection)
        self.assertEqual(revision, current.headers["etag"])
        audit = next(event for event in self.client.app.state.audit_store.recent(10)
                     if event["action"] == "vm.picker.update")
        self.assertEqual((audit["target"], audit["outcome"]), ("vm-1", 202))

    def test_smbios_diagnostics_requires_session_csrf_and_catalog_revision(self) -> None:
        path = "/api/v1/vms/vm-1/smbios/diagnostics"
        payload = {"idempotency_key": "00000000-0000-4000-8000-000000000002"}
        self.assertEqual(self.client.post(path, json=payload).status_code, 401)
        self.bootstrap()
        headers = {**origin(), "X-CSRF-Token": self.client.cookies.get("vh_csrf")}
        self.assertEqual(self.client.post(path, headers=headers, json=payload).status_code, 428)
        current = self.client.get("/api/v1/vms/vm-1/config")
        accepted = self.client.post(path, headers={**headers, "If-Match": current.headers["etag"]}, json=payload)
        self.assertEqual(accepted.status_code, 202, accepted.text)
        self.assertEqual(accepted.json()["job_id"], "smbios-job")
        vm_id, request, revision = self.client.app.state.agent.last_smbios_diagnostic
        self.assertEqual(vm_id, "vm-1")
        self.assertEqual(request.idempotency_key, payload["idempotency_key"])
        self.assertEqual(revision, current.headers["etag"])
        audit = next(event for event in self.client.app.state.audit_store.recent(10)
                     if event["action"] == "vm.smbios.check")
        self.assertEqual((audit["target"], audit["outcome"]), ("vm-1", 202))

    def test_catalog_etags_and_create_update_require_if_match_and_csrf(self) -> None:
        self.bootstrap()
        catalog = self.client.get("/api/v1/vms")
        self.assertEqual(catalog.headers["etag"], f'"{"a" * 64}"')
        get_config = self.client.get("/api/v1/vms/vm-1/config")
        self.assertEqual(get_config.headers["etag"], f'"{"b" * 64}"')
        headers = {**origin(), "X-CSRF-Token": self.client.cookies.get("vh_csrf")}
        body = {
            "id": "new-vm", "name": "New macOS", "macos_version": "tahoe", "vcpus": 4,
            "ram_gb": 8, "disk_size_gb": 64,
        }
        missing = self.client.post("/api/v1/vms", headers=headers, json=body)
        self.assertEqual(missing.status_code, 428)
        created = self.client.post("/api/v1/vms", headers={**headers, "If-Match": catalog.headers["etag"]}, json=body)
        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(created.headers["etag"], f'"{"c" * 64}"')
        self.assertEqual(self.client.app.state.agent.last_create[1], catalog.headers["etag"])

        update_body = {
            "name": "Renamed", "macos_version": "tahoe", "vcpus": 4, "ram_gb": 8,
            "disk_size_gb": 64,
        }
        updated = self.client.put(
            "/api/v1/vms/vm-1/config",
            headers={**headers, "If-Match": get_config.headers["etag"]},
            json=update_body,
        )
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(updated.headers["etag"], f'"{"d" * 64}"')

    def test_reims_vm_import_preview_and_commit_are_authenticated_and_revisioned(self) -> None:
        self.assertEqual(self.client.get("/api/v1/vms/import-candidates").status_code, 401)
        self.bootstrap()
        preview = self.client.get("/api/v1/vms/import-candidates")
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertEqual(preview.json(), {"items": []})
        payload = {"id": "legacy-vm", "macos_version": "tahoe"}
        self.assertEqual(self.client.post("/api/v1/vms/import", json=payload, headers=origin()).status_code, 403)
        headers = {**origin(), "X-CSRF-Token": self.client.cookies.get("vh_csrf")}
        self.assertEqual(self.client.post("/api/v1/vms/import", json=payload, headers=headers).status_code, 428)
        imported = self.client.post(
            "/api/v1/vms/import", json=payload,
            headers={**headers, "If-Match": f'"{"a" * 64}"'},
        )
        self.assertEqual(imported.status_code, 201, imported.text)
        self.assertEqual(imported.headers["etag"], f'"{"b" * 64}"')
        self.assertEqual(self.client.app.state.agent.last_import[0].id, "legacy-vm")
        self.assertEqual(self.client.app.state.agent.last_import[1], f'"{"a" * 64}"')

    def test_reims_release_settings_are_authenticated_csrf_protected_and_audited(self):
        self.assertEqual(self.client.get("/api/v1/settings/reims/releases").status_code, 401)
        self.bootstrap()
        response = self.client.get("/api/v1/settings/reims/releases")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["repository"], "felipeab10/reims-vgpu")
        self.assertEqual(response.json()["current_release"], "v1.0.0")
        self.assertEqual(response.json()["active_release"], "v0.9.0")
        payload = {"idempotency_key": "12345678-1234-4234-8234-123456789abc", "tag": "v1.1.0"}
        self.assertEqual(self.client.post("/api/v1/settings/reims/releases", json=payload).status_code, 403)
        headers = {**origin(), "X-CSRF-Token": self.client.cookies.get("vh_csrf")}
        updated = self.client.post("/api/v1/settings/reims/releases", headers=headers, json=payload)
        self.assertEqual(updated.status_code, 202, updated.text)
        self.assertEqual(updated.json()["job_id"], "release-job")
        self.assertEqual(self.client.app.state.agent.last_reims_release_request.tag, "v1.1.0")
        event = self.client.app.state.audit_store.recent(1)[0]
        self.assertEqual(event["action"], "reims.release.update")
        self.assertEqual(event["outcome"], 202)
        self.assertEqual(event["operation_id"], "release-job")

    def test_vm_provisioning_requires_explicit_policy_and_returns_async_job(self):
        self.bootstrap()
        headers = {**origin(), "X-CSRF-Token": self.client.cookies.get("vh_csrf")}
        base = {
            "idempotency_key": "provision-key-0001", "id": "macos-vm", "name": "macOS VM",
            "macos_version": "tahoe", "vcpus": 4, "ram_gb": 8, "disk_size_gb": 64,
        }
        missing_policy = self.client.post("/api/v1/vms/provision", headers=headers, json=base)
        self.assertEqual(missing_policy.status_code, 422)
        arbitrary_path = self.client.post(
            "/api/v1/vms/provision", headers=headers,
            json={**base, "installer_policy": "cached", "installer_path": "/etc/shadow"},
        )
        self.assertEqual(arbitrary_path.status_code, 422)
        invalid_hidpi = self.client.post(
            "/api/v1/vms/provision", headers=headers,
            json={**base, "installer_policy": "none", "display_resolution": "1280x720", "display_hidpi": True},
        )
        self.assertEqual(invalid_hidpi.status_code, 422)
        accepted = self.client.post(
            "/api/v1/vms/provision", headers=headers,
            json={**base, "installer_policy": "none", "display_resolution": "2560x1440", "display_hidpi": True, "display_refresh_hz": 144},
        )
        self.assertEqual(accepted.status_code, 202, accepted.text)
        self.assertEqual(accepted.json()["job_id"], "provision-job")
        self.assertEqual(accepted.headers["x-request-id"], self.client.app.state.audit_store.recent(1)[0]["request_id"])
        audit_entry = self.client.app.state.audit_store.recent(1)[0]
        self.assertEqual(audit_entry["actor"], "admin")
        self.assertEqual(audit_entry["target"], "/api/v1/vms/provision")
        self.assertEqual(audit_entry["outcome"], 202)
        self.assertEqual(audit_entry["operation_id"], "provision-job")
        self.assertNotIn("activation-code-that-is-long", str(audit_entry))
        self.assertNotIn("correct horse battery", str(audit_entry))
        self.assertEqual(self.client.app.state.agent.last_provision.installer_policy, "none")
        self.assertEqual(self.client.app.state.agent.last_provision.display_resolution, "2560x1440")
        self.assertTrue(self.client.app.state.agent.last_provision.display_hidpi)
        self.assertEqual(self.client.app.state.agent.last_provision.display_refresh_hz, 144)
        job = self.client.get("/api/v1/jobs/provision-job")
        self.assertEqual(job.status_code, 200, job.text)
        self.assertEqual(job.json()["stage"], "disk_opencore")
        cancelled = self.client.post("/api/v1/jobs/provision-job/cancel", headers=headers)
        self.assertEqual(cancelled.status_code, 200, cancelled.text)

    def test_unexpected_provisioning_failure_returns_traceable_safe_problem(self):
        self.bootstrap()
        headers = {**origin(), "X-CSRF-Token": self.client.cookies.get("vh_csrf")}
        self.client.app.state.agent.provision_vm = lambda _request: (_ for _ in ()).throw(
            RuntimeError("private host path /root/private/qemu-output")
        )
        from fastapi.testclient import TestClient

        with self.assertLogs("web.api.app", level="ERROR") as captured:
            with TestClient(self.client.app, raise_server_exceptions=False) as client:
                client.cookies.update(self.client.cookies)
                response = client.post("/api/v1/vms/provision", headers=headers, json={
                    "idempotency_key": "provision-unexpected-1",
                    "id": "macos-vm",
                    "name": "macOS VM",
                    "macos_version": "tahoe",
                    "vcpus": 4,
                    "ram_gb": 8,
                    "disk_size_gb": 64,
                    "installer_policy": "none",
                })
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.headers["content-type"].split(";")[0], "application/json")
        problem = response.json()
        self.assertEqual(problem["code"], "internal_error")
        self.assertEqual(problem["request_id"], response.headers["x-request-id"])
        self.assertIn(problem["request_id"], problem["message"])
        self.assertNotIn("/root/private", response.text)
        self.assertIn(problem["request_id"], captured.output[0])
        self.assertIn("exception_type=RuntimeError", captured.output[0])
        self.assertNotIn("/root/private", captured.output[0])
        self.assertNotIn("private host path", captured.output[0])
        event = self.client.app.state.audit_store.recent(1)[0]
        self.assertEqual(event["outcome"], 500)
        self.assertEqual(event["request_id"], problem["request_id"])

    def test_audit_history_is_authenticated_and_excludes_secrets(self):
        self.assertEqual(self.client.get("/api/v1/audit").status_code, 401)
        self.bootstrap()
        headers = {**origin(), "X-CSRF-Token": self.client.cookies.get("vh_csrf")}
        payload = {
            "idempotency_key": "audit-job-key-0001", "id": "audit-vm", "name": "Audit VM",
            "macos_version": "tahoe", "vcpus": 2, "ram_gb": 4,
            "installer_policy": "none",
        }
        accepted = self.client.post("/api/v1/vms/provision", headers=headers, json=payload)
        self.assertEqual(accepted.status_code, 202)
        response = self.client.get("/api/v1/audit?limit=20")
        self.assertEqual(response.status_code, 200, response.text)
        entries = response.json()["items"]
        provision = next(item for item in entries if item["target"] == "/api/v1/vms/provision")
        self.assertEqual(provision["actor"], "admin")
        self.assertEqual(provision["action"], "vm.provision")
        self.assertEqual(provision["outcome"], 202)
        self.assertEqual(provision["operation_id"], "provision-job")
        self.assertEqual(provision["request_id"], accepted.headers["x-request-id"])
        self.assertNotIn("activation-code-that-is-long", response.text)
        self.assertNotIn("correct horse battery", response.text)

    def test_retry_requires_session_and_csrf_and_returns_existing_job_id(self):
        self.assertEqual(self.client.post("/api/v1/jobs/provision-job/retry").status_code, 401)
        self.bootstrap()
        self.assertEqual(self.client.post("/api/v1/jobs/provision-job/retry", headers=origin()).status_code, 403)
        headers = {**origin(), "X-CSRF-Token": self.client.cookies.get("vh_csrf")}
        accepted = self.client.post("/api/v1/jobs/provision-job/retry", headers=headers)
        self.assertEqual(accepted.status_code, 202, accepted.text)
        self.assertEqual(accepted.json(), {
            "job_id": "provision-job", "state": "queued",
            "events_url": "/api/v1/jobs/provision-job/events",
        })
        missing = self.client.post("/api/v1/jobs/absent/retry", headers=headers)
        self.assertEqual(missing.status_code, 404)


class HostTelemetryFanoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_five_clients_share_host_and_two_vm_collection_cycles(self) -> None:
        calls = {"host": 0, "vm-1": 0, "vm-2": 0}

        def collector(key):
            async def collect():
                calls[key] += 1
                return {"samples": [{"source": key, "value": calls[key]}]}
            return collect

        valid = lambda _token: asyncio.sleep(0, result=True)
        hubs = {key: _HostTelemetryStream(collector(key), valid, interval=0.05) for key in calls}
        subscriptions = {
            "host": [await hubs["host"].subscribe(f"session-{index}") for index in range(5)],
            "vm-1": [await hubs["vm-1"].subscribe(f"session-{index}") for index in range(5)],
            "vm-2": [await hubs["vm-2"].subscribe(f"session-{index}") for index in range(5)],
        }
        snapshots = {
            key: [await asyncio.wait_for(queue.get(), timeout=1) for _subscriber, queue in pairs]
            for key, pairs in subscriptions.items()
        }
        self.assertEqual(calls, {"host": 1, "vm-1": 1, "vm-2": 1})
        for key, group in snapshots.items():
            self.assertEqual({item["data"]["samples"][0]["source"] for item in group}, {key})
            self.assertEqual({item["sequence"] for item in group}, {0})
        for key, pairs in subscriptions.items():
            for subscriber_id, _queue in pairs:
                await hubs[key].unsubscribe(subscriber_id)

    async def test_five_subscribers_share_one_collection_per_tick(self) -> None:
        calls = 0

        async def collect():
            nonlocal calls
            calls += 1
            return {"samples": [{"value": calls}]}

        hub = _HostTelemetryStream(collect, lambda _token: asyncio.sleep(0, result=True), interval=0.03)
        subscribers = [await hub.subscribe(f"session-{index}") for index in range(5)]
        first = [await asyncio.wait_for(queue.get(), timeout=1) for _, queue in subscribers]
        self.assertEqual(calls, 1)
        self.assertEqual({item["sequence"] for item in first}, {0})
        await asyncio.sleep(0.04)
        second = [await asyncio.wait_for(queue.get(), timeout=1) for _, queue in subscribers]
        self.assertEqual(calls, 2)
        self.assertEqual({item["sequence"] for item in second}, {1})
        self.assertEqual({item["epoch"] for item in first + second}, {hub.epoch})
        for subscriber_id, _queue in subscribers:
            await hub.unsubscribe(subscriber_id)

    async def test_slow_subscriber_queue_discards_oldest_samples(self) -> None:
        async def collect():
            return {"samples": []}

        hub = _HostTelemetryStream(collect, lambda _token: asyncio.sleep(0, result=True), interval=0.01, queue_size=2)
        subscriber_id, queue = await hub.subscribe("slow")
        first = await asyncio.wait_for(queue.get(), timeout=1)
        await asyncio.sleep(0.065)
        self.assertEqual(queue.qsize(), 2)
        retained = [queue.get_nowait(), queue.get_nowait()]
        self.assertEqual([item["sequence"] for item in retained], sorted(item["sequence"] for item in retained))
        self.assertGreater(retained[-1]["sequence"], 1)
        self.assertGreater(retained[0]["sequence"], first["sequence"] + 1)
        await hub.unsubscribe(subscriber_id)

    async def test_revoked_session_is_removed_and_epoch_changes_on_reconnect(self) -> None:
        valid = True

        async def collect():
            return {"samples": []}

        async def check(_token):
            return valid

        hub = _HostTelemetryStream(collect, check, interval=0.01)
        subscriber_id, queue = await hub.subscribe("revoked")
        first = await asyncio.wait_for(queue.get(), timeout=1)
        valid = False
        for _ in range(20):
            if subscriber_id not in hub.subscribers:
                break
            await asyncio.sleep(0.01)
        self.assertNotIn(subscriber_id, hub.subscribers)
        await hub.unsubscribe(subscriber_id)

        next_hub = _HostTelemetryStream(collect, lambda _token: asyncio.sleep(0, result=True), interval=0.01)
        next_id, next_queue = await next_hub.subscribe("reconnected")
        reconnected = await asyncio.wait_for(next_queue.get(), timeout=1)
        self.assertNotEqual(first["epoch"], reconnected["epoch"])
        self.assertEqual(reconnected["sequence"], 0)
        await next_hub.unsubscribe(next_id)


class WebApiRemainingTests(WebApiTests):
    def test_deferred_auth_store_allows_csrf_protected_vm_provisioning(self) -> None:
        deferred_app = create_app(
            agent=FakeAgent(),
            auth_db_path=Path(self.temp.name) / "deferred-auth.sqlite",
            audit_db_path=Path(self.temp.name) / "deferred-audit.sqlite",
            activation_code=ActivationCode("activation-code-that-is-long"),
            cookie_secure=False,
        )
        with TestClient(deferred_app) as client:
            bootstrap = client.post(
                "/api/v1/session/bootstrap", headers=origin(),
                json={"code": "activation-code-that-is-long", "username": "admin", "password": "correct horse battery"},
            )
            self.assertEqual(bootstrap.status_code, 200, bootstrap.text)
            headers = {**origin(), "X-CSRF-Token": client.cookies.get("vh_csrf")}
            accepted = client.post("/api/v1/vms/provision", headers=headers, json={
                "idempotency_key": "deferred-provision-key-1",
                "id": "deferred-vm", "name": "Deferred VM", "macos_version": "tahoe",
                "vcpus": 4, "ram_gb": 8, "disk_size_gb": 64, "installer_policy": "none",
            })
            self.assertEqual(accepted.status_code, 202, accepted.text)
            self.assertEqual(accepted.json()["job_id"], "provision-job")

    def test_api_recreation_keeps_access_to_agent_persisted_history(self) -> None:
        agent_service = WebAgentService(
            Path(self.temp.name) / "vms.json",
            telemetry_history_path=Path(self.temp.name) / "telemetry.sqlite",
            telemetry_history_enabled=True,
        )
        agent_service.telemetry_history.record([
            MetricSample("host.cpu_percent", 42, datetime.now(timezone.utc), "%", "test")
        ])
        rpc_client = RpcAgentClient(InProcessAgentRpc(agent_service.handlers()))
        self.client.close()
        first_api = create_app(
            agent=rpc_client, auth_store=SQLiteAuthStore(self.db),
            activation_code=ActivationCode("activation-code-that-is-long"), cookie_secure=False,
        )
        with TestClient(first_api) as first_client:
            first_client.post(
                "/api/v1/session/bootstrap", headers=origin(),
                json={"code": "activation-code-that-is-long", "username": "admin", "password": "correct horse battery"},
            )
            first_history = first_client.get("/api/v1/host/metrics/history?name=host.cpu_percent")
            self.assertEqual(first_history.status_code, 200, first_history.text)
            self.assertEqual(first_history.json()["points"][0]["average"], 42)
        second_api = create_app(
            agent=rpc_client, auth_store=SQLiteAuthStore(self.db),
            activation_code=ActivationCode(), cookie_secure=False,
        )
        with TestClient(second_api) as restarted_client:
            login = restarted_client.post(
                "/api/v1/session/login", headers=origin(),
                json={"username": "admin", "password": "correct horse battery"},
            )
            self.assertEqual(login.status_code, 200, login.text)
            preserved = restarted_client.get("/api/v1/host/metrics/history?name=host.cpu_percent")
            self.assertEqual(preserved.status_code, 200, preserved.text)
            self.assertEqual(preserved.json()["points"][0]["average"], 42)
        agent_service.telemetry_history.close()

    def test_activation_failures_are_rate_limited_and_security_headers_are_set(self) -> None:
        bad_host = self.client.post(
            "/api/v1/session/bootstrap", headers={**origin(), "Host": "attacker.example"},
            json={"code": "wrong-code", "username": "admin", "password": "correct horse battery"},
        )
        self.assertEqual(bad_host.status_code, 403)
        self.assertEqual(bad_host.json()["code"], "origin_rejected")
        for _ in range(5):
            response = self.client.post(
                "/api/v1/session/bootstrap", headers=origin(),
                json={"code": "wrong-code", "username": "admin", "password": "correct horse battery"},
            )
            self.assertEqual(response.status_code, 401)
        blocked = self.client.post(
            "/api/v1/session/bootstrap", headers=origin(),
            json={"code": "activation-code-that-is-long", "username": "admin", "password": "correct horse battery"},
        )
        self.assertEqual(blocked.status_code, 429)
        self.assertEqual(blocked.headers.get("retry-after"), "900")
        self.assertIn("frame-ancestors 'none'", blocked.headers["content-security-policy"])
        self.assertEqual(blocked.headers["x-content-type-options"], "nosniff")
        self.assertEqual(blocked.headers["cache-control"], "no-store")

    def test_tls_session_cookies_are_secure_http_only_and_strict(self) -> None:
        secure_app = create_app(
            agent=FakeAgent(), auth_store=SQLiteAuthStore(Path(self.temp.name) / "secure.sqlite"),
            activation_code=ActivationCode("secure-activation-code-long"), cookie_secure=True,
        )
        with TestClient(secure_app) as client:
            response = client.post(
                "/api/v1/session/bootstrap", headers=origin(),
                json={"code": "secure-activation-code-long", "username": "admin", "password": "correct horse battery"},
            )
            self.assertEqual(response.status_code, 200, response.text)
            cookies = response.headers.get_list("set-cookie")
            self.assertEqual(len(cookies), 2)
            self.assertTrue(all("Secure" in value and "SameSite=strict" in value for value in cookies))
            self.assertTrue(any("vh_session=" in value and "HttpOnly" in value for value in cookies))

    def test_host_metrics_history_is_authenticated_validated_and_filtered(self) -> None:
        unauthenticated = self.client.get("/api/v1/host/metrics/history?name=host.cpu_percent")
        self.assertEqual(unauthenticated.status_code, 401)
        self.bootstrap()
        response = self.client.get(
            '/api/v1/host/metrics/history?name=host.cpu_percent&resolution_seconds=60&labels=%7B%22cpu%22%3A%22all%22%7D'
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["resolution_seconds"], 60)
        self.assertEqual(self.client.app.state.agent.last_history_request[:3], ("host.cpu_percent", {"cpu": "all"}, 60))
        invalid = self.client.get("/api/v1/host/metrics/history?name=host.cpu_percent&resolution_seconds=11")
        self.assertEqual(invalid.status_code, 422)

    def test_vm_metrics_history_returns_not_found_for_missing_vm_and_rejects_bad_labels(self) -> None:
        self.bootstrap()
        missing = self.client.get("/api/v1/vms/unknown/metrics/history?name=vm.cpu_percent")
        self.assertEqual(missing.status_code, 404)
        invalid = self.client.get(
            "/api/v1/vms/vm-1/metrics/history?name=vm.cpu_percent&labels=%7B%22%22%3A%22x%22%7D"
        )
        self.assertEqual(invalid.status_code, 422)

    def test_mutation_rejects_invalid_if_match_before_agent_call(self) -> None:
        self.bootstrap()
        body = {
            "id": "new-vm", "name": "New macOS", "macos_version": "tahoe", "vcpus": 4,
            "ram_gb": 8, "disk_size_gb": 64,
        }
        response = self.client.post(
            "/api/v1/vms",
            headers={**origin(), "X-CSRF-Token": self.client.cookies.get("vh_csrf"), "If-Match": "*"},
            json=body,
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "invalid_if_match")

    def test_mutating_vm_action_requires_csrf_then_delegates(self) -> None:
        self.bootstrap()
        payload = {"operation": Operation.START.value, "idempotency_key": "an-idempotency-key"}
        denied = self.client.post("/api/v1/vms/vm-1/actions", json=payload, headers=origin())
        self.assertEqual(denied.status_code, 403)
        response = self.client.post(
            "/api/v1/vms/vm-1/actions",
            json=payload,
            headers={**origin(), "X-CSRF-Token": self.client.cookies.get("vh_csrf")},
        )
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["job_id"], "job-vm-1")

    def test_forced_stop_requires_confirmation_matching_target(self) -> None:
        self.bootstrap()
        headers = {**origin(), "X-CSRF-Token": self.client.cookies.get("vh_csrf")}
        missing = self.client.post(
            "/api/v1/vms/vm-1/actions",
            json={"operation": Operation.FORCE_STOP.value, "idempotency_key": "forced-stop-key-1"},
            headers=headers,
        )
        self.assertEqual(missing.status_code, 422)
        self.assertEqual(missing.json()["code"], "confirmation_required")
        mismatch = self.client.post(
            "/api/v1/vms/vm-1/actions",
            json={"operation": Operation.FORCE_STOP.value, "confirmation": "another-vm", "idempotency_key": "forced-stop-key-2"},
            headers=headers,
        )
        self.assertEqual(mismatch.status_code, 422)
        self.assertEqual(mismatch.json()["code"], "confirmation_required")
        accepted = self.client.post(
            "/api/v1/vms/vm-1/actions",
            json={"operation": Operation.FORCE_STOP.value, "confirmation": "vm-1", "idempotency_key": "forced-stop-key-3"},
            headers=headers,
        )
        self.assertEqual(accepted.status_code, 202, accepted.text)
        self.assertEqual(accepted.json()["job_id"], "job-vm-1")
        event = self.client.app.state.audit_store.recent(1)[0]
        self.assertEqual(event["target"], '/api/v1/vms/{vm_id}/actions {"vm_id": "vm-1"}')
        self.assertEqual(event["action"], "vm.force_stop")
        self.assertEqual(event["outcome"], 202)

    def test_unavailable_agent_capability_is_a_structured_503(self) -> None:
        self.bootstrap()
        self.client.app.state.agent.reject_action = True
        response = self.client.post(
            "/api/v1/vms/vm-1/actions",
            json={"operation": Operation.START.value, "idempotency_key": "start-action-key-1"},
            headers={**origin(), "X-CSRF-Token": self.client.cookies.get("vh_csrf")},
        )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["code"], "capability_unavailable")

    def test_agent_unavailable_returns_structured_503_after_auth(self) -> None:
        self.client.close()
        app = create_app(
            agent=None,
            auth_store=SQLiteAuthStore(self.db),
            activation_code=ActivationCode("activation-code-that-is-long"),
            cookie_secure=False,
        )
        self.client = TestClient(app)
        self.bootstrap()
        response = self.client.get("/api/v1/vms")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["code"], "agent_unavailable")

    def test_login_rejects_cross_origin_and_bad_credentials(self) -> None:
        self.bootstrap()
        self.client.post("/api/v1/session/logout", headers={**origin(), "X-CSRF-Token": self.client.cookies.get("vh_csrf")})
        invalid_origin = self.client.post(
            "/api/v1/session/login",
            headers={"Origin": "https://evil.example"},
            json={"username": "admin", "password": "correct horse battery"},
        )
        self.assertEqual(invalid_origin.status_code, 403)
        invalid_login = self.client.post(
            "/api/v1/session/login", headers=origin(), json={"username": "admin", "password": "wrong password"}
        )
        self.assertEqual(invalid_login.status_code, 401)
        self.assertEqual(invalid_login.json()["code"], "invalid_credentials")

    def test_validation_errors_do_not_echo_credentials(self) -> None:
        secret = "UNIQUE_PASSWORD_SENTINEL"
        response = self.client.post(
            "/api/v1/session/bootstrap",
            headers=origin(),
            json={"code": "activation-code-that-is-long", "username": "", "password": secret},
        )
        self.assertEqual(response.status_code, 422)
        self.assertNotIn(secret, response.text)


if __name__ == "__main__":
    unittest.main()
