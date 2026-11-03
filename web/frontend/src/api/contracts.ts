export interface SessionView {
  authenticated: boolean;
  username: string | null;
  expires_at: string | null;
  bootstrap_required: boolean;
  must_change_password?: boolean;
  default_credentials_available?: boolean;
  tls_fingerprint: string | null;
  reims_update_status?: 'queued' | 'unavailable' | null;
  reims_update_job_id?: string | null;
}

export interface WebActivationView {
  appliance_id: string;
  activation_code: string;
  status: 'not_submitted' | 'pending' | 'approved' | 'rejected';
  updated_at: string;
  portal_url: string;
  inventory_changed: boolean;
}

export interface WebActivationSubmitResponse {
  status: 'pending' | 'approved' | 'rejected';
  updated_at: string;
}

export interface PasswordChangeRequest {
  current_password: string;
  new_password: string;
}

export interface AuditEvent {
  created_at: string;
  actor: string;
  action: string;
  target: string;
  outcome: number;
  request_id: string;
  operation_id: string;
}

export interface AuditListResponse {
  items: AuditEvent[];
  limit: number;
}

export interface HostOverview {
  hostname: string;
  architecture: string;
  cpu_model: string;
  logical_cpus: number;
  memory_total_bytes: number;
  memory_available_bytes: number;
  uptime_seconds: number;
  addresses: string[];
  web_port: number;
  tls_fingerprint: string | null;
}

export interface HealthView {
  status: 'ok' | 'degraded' | 'unavailable';
  version: string;
  agent_connected: boolean;
  checked_at: string;
}

export interface RuntimeCapability {
  id: string;
  label: string;
  supported: boolean;
  available: boolean;
  requires_stopped_vm: boolean;
  description: string | null;
  limitation: string | null;
}

export interface AdditionalDiskConfig {
  media_id: string | null;
  path: string;
  format: 'qcow2' | 'raw';
  controller: 'ahci' | 'nvme';
  order: number;
}

export interface AdditionalDiskDraft {
  media_id: string;
  controller: 'ahci' | 'nvme';
  order: number;
}

export interface CpuTopology {
  sockets: number;
  cores: number;
  threads: number;
}

export interface CapabilitiesResponse {
  runtime_fingerprint: string;
  generated_at: string;
  items: RuntimeCapability[];
  runtime: {
    status: 'available' | 'partial' | 'missing';
    reims_version: string | null;
    reims_release_tag: string | null;
    reims_schema_version: number | null;
    reims_schema_features: string[];
    qemu_version: string | null;
    qemu_options: string[];
    qemu_command_options: string[];
    qemu_devices: string[];
    qemu_machine_types: string[];
    qemu_cpu_models: string[];
    qemu_audio_backends: string[];
    qemu_device_categories: Record<string, string[]>;
    reims_set_options: string[];
    reims_create_options: string[];
    binary_fingerprint: string;
  } | null;
}

export interface TelemetrySample {
  name: string;
  value: number | string | null;
  timestamp: string;
  unit: string;
  source: string;
  quality: string;
  availability: 'available' | 'unavailable';
  labels: Record<string, string>;
}

export interface TelemetryResponse {
  samples: TelemetrySample[];
}

export interface TelemetrySettingsView {
  history_retention_hours: 1 | 6 | 12 | 24;
  history_persistence_enabled: boolean;
}

export interface ReimsRelease {
  tag: string;
  name: string;
  published_at: string | null;
  asset_size_bytes: number;
  prerelease: boolean;
}

export interface ReimsReleaseList {
  repository: string;
  current_release: string | null;
  active_release: string | null;
  items: ReimsRelease[];
}

export interface ControlReleaseList {
  repository: string;
  package_format: 'arch' | 'deb' | 'rpm';
  current_release: string;
  items: Array<{
    tag: string;
    name: string;
    published_at: string | null;
    asset_size_bytes: number;
    prerelease: boolean;
  }>;
}

export interface TelemetryHistoryPoint {
  timestamp: string;
  resolution_seconds: 10 | 60;
  average: number | null;
  minimum: number | null;
  maximum: number | null;
  sample_count: number;
  unavailable_count: number;
  availability: 'available' | 'partial' | 'unavailable';
  unit: string;
  source: string;
  labels: Record<string, string>;
}

export interface TelemetryHistory {
  name: string;
  resolution_seconds: 10 | 60;
  points: TelemetryHistoryPoint[];
  vm_id: string | null;
}

export interface GpuDevice {
  pci_bdf: string;
  vendor_id: string;
  device_id: string;
  name: string;
  kind: string;
  render_node: string | null;
  vulkan_uuid: string | null;
  driver_file: string | null;
  reims_device_uuid: string | null;
  reims_pci_address: string | null;
  reims_shares_driver: boolean | null;
  vulkan_available: boolean;
  used_by_host: boolean | null;
  assigned_vm_id: string | null;
  selection_supported: boolean;
  limitation: string | null;
}

export interface GpuInventoryResponse {
  items: GpuDevice[];
  stale: boolean;
  message: string | null;
}

export interface HostUsbDevice {
  device_id: string;
  label: string;
  device_count: number;
  auto_attach_supported: boolean;
  limitation: string | null;
}

export interface HostUsbInventoryResponse {
  items: HostUsbDevice[];
  stale: boolean;
  message: string | null;
}

export interface StorageItem {
  id: string;
  name: string;
  location: 'managed' | 'external';
  availability: 'available' | 'missing' | 'external';
  virtual_size_bytes: number | null;
  allocated_bytes: number | null;
  references: string[];
}

export interface StorageInventory {
  items: StorageItem[];
  free_bytes: number | null;
  managed_volume: string;
}

export interface MediaConvertRequest {
  output_format: 'raw' | 'qcow2';
  idempotency_key: string;
}

export interface VmBackupItem {
  backup_id: string;
  created_at: string;
  size_bytes: number;
  file_count: number;
  metadata_valid: boolean;
  integrity_note: string;
}

export interface VmBackupListResponse {
  vm_id: string;
  items: VmBackupItem[];
}

export interface PrivateTransferView {
  id: string;
  filename: string;
  size_bytes: number;
  format: 'iso' | 'qcow2' | 'dmg';
}

export interface PrivateTransferListResponse {
  items: PrivateTransferView[];
  max_file_bytes: number;
  max_total_bytes: number;
  min_free_bytes: number;
}

export interface VmConfig {
  id: string;
  name: string;
  macos_version: string;
  vcpus: number;
  cpu_model: string;
  cpu_topology?: CpuTopology | null;
  ram_gb: number;
  disk_size_gb: number;
  disk_path: string;
  additional_disks: AdditionalDiskConfig[];
  auto_start: boolean;
  gpu_mode: string;
  vfio_pci_id: string | null;
  selected_gpu: string | null;
  selected_gpu_name: string | null;
  selected_gpu_uuid: string | null;
  selected_gpu_device_uuid: string | null;
  selected_gpu_pci_address: string | null;
  selected_gpu_driver_file: string | null;
  audio_device: string;
  audio_enabled: boolean;
  audio_backend: string;
  network_enabled: boolean;
  network_mac: string | null;
  network_ssh_port: number | null;
  fullscreen: boolean;
  display_resolution: string;
  display_count: number;
  display_hidpi: boolean;
  display_refresh_hz: number | null;
  opencore_show_picker: boolean;
  created_at: string;
  last_booted_at: string | null;
  smbios: { model: string; serial_number: string; board_serial: string; smuuid: string; rom: string };
  install_reboots: number;
  install_evidence: string;
  usb_auto_attach: string[];
  qemu_extra_args: string[];
  extra_config: Record<string, unknown>;
  power_sync_mode: 'vm-only' | 'kiosk';
  install_phase: 'pending' | 'installing' | 'installed';
  revision: string;
}

export interface VmConfigEnvelope {
  config: VmConfig;
  revision: string;
  runtime_config_differs: boolean;
  pending_restart_fields: string[];
  projection_status: 'available' | 'unknown';
  config_differences: Array<{ field: string; saved_value: string; last_projection_value: string }>;
}

export type JobState = 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelling' | 'cancelled' | 'interrupted';

export interface JobView {
  id: string;
  kind: string;
  state: JobState;
  stage: string;
  progress_percent: number | null;
  created_at: string;
  updated_at: string;
  result: Record<string, unknown>;
  error: { code?: string; message?: string } | null;
  cancellable: boolean;
  retryable: boolean;
  checkpoints?: JobCheckpoint[];
  logs?: JobLog[];
}

export interface JobSummary {
  id: string;
  kind: string;
  state: JobState;
  stage: string;
  progress_percent: number | null;
  target_vm_id: string | null;
  created_at: string;
  updated_at: string;
  cancellable: boolean;
  retryable: boolean;
}

export interface JobCheckpoint {
  name: string;
  payload: Record<string, unknown>;
  completed: boolean;
  updated_at: string;
}

export interface JobLog {
  id: number;
  created_at: string;
  level: 'debug' | 'info' | 'warning' | 'error';
  message: string;
}

export interface JobListResponse {
  items: JobSummary[];
  limit: number;
  offset: number;
  total: number;
}

export interface JobAccepted {
  job_id: string;
  state: JobState;
  events_url: string;
}

export interface VmProvisionRequest {
  idempotency_key: string;
  id?: string | null;
  name: string;
  macos_version: string;
  vcpus: number;
  cpu_model: string;
  cpu_topology?: CpuTopology | null;
  audio_device: 'usb-audio' | 'virtio-sound';
  audio_backend: string;
  audio_enabled: boolean;
  network_enabled: boolean;
  network_mac: string | null;
  network_ssh_port?: number;
  ram_gb: number;
  disk_size_gb: number;
  auto_start: boolean;
  fullscreen: boolean;
  display_resolution: string;
  display_count?: number;
  display_hidpi: boolean;
  display_refresh_hz: number | null;
  selected_gpu_bdf: string | null;
  selected_gpu_uuid: string | null;
  selected_gpu_device_uuid?: string | null;
  installer_policy: 'none' | 'cached' | 'download';
  opencore_show_picker?: boolean | null;
  usb_auto_attach: string[];
  qemu_extra_args: string[];
}

export interface VmListResponse {
  items: VmConfigEnvelope[];
  revision: string;
}

export interface ReimsImportCandidate {
  id: string;
  name: string;
  directory: string;
  disk_path: string;
  vcpus: number;
  ram_gb: number;
  disk_size_gb: number;
  mac: string | null;
  display_resolution: string;
  display_hidpi: boolean;
  importable: boolean;
  runtime_state: 'running' | 'stopped' | 'unknown';
  runtime_pid: number | null;
  limitation: string | null;
}

export interface ReimsImportCandidateList { items: ReimsImportCandidate[] }

export interface VmDeleteResponse {
  id: string;
  deleted: true;
  revision: string;
}

export interface VmRuntime {
  id: string;
  state: 'stopped' | 'starting' | 'running' | 'stopping' | 'degraded' | 'unknown';
  pid: number | null;
  process_started_at: string | null;
  qmp_available: boolean;
  install_phase: 'pending' | 'installing' | 'installed';
  supervisor: {
    unit: string;
    load_state: string;
    active_state: string;
    sub_state: string;
    main_pid: number | null;
    main_process_started_at: string | null;
    result: string;
  } | null;
  gpu: {
    configured_bdf: string | null;
    configured_uuid: string | null;
    configured_device_uuid: string | null;
    configured_pci_address: string | null;
    configured_driver_file: string | null;
    effective_bdf: string | null;
    effective_uuid: string | null;
    verification: 'confirmed' | 'unavailable' | 'mismatch' | 'ambiguous';
    message: string | null;
  } | null;
  observed_at: string;
  message: string | null;
}

export interface VmRuntimeList {
  items: VmRuntime[];
  observed_at: string;
}

export interface VmSerialConsole {
  vm_id: string;
  available: boolean;
  content: string;
  updated_at: string | null;
  truncated: boolean;
  message: string | null;
}

export interface ApiProblem {
  code?: string;
  message?: string;
  request_id?: string;
  details?: Record<string, unknown>;
}
