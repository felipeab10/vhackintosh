import { lazy, Suspense, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { AlertTriangle, ArrowLeft, Check, Cpu, Download, HardDrive, Layers3, LoaderCircle, Monitor, Save, Server, X } from 'lucide-react';
import { ApiError, api } from './api/client';
import type { AdditionalDiskDraft, CapabilitiesResponse, CpuTopology, GpuInventoryResponse, HostUsbInventoryResponse, JobAccepted, JobView, StorageInventory, VmConfig, VmConfigEnvelope, VmListResponse, VmProvisionRequest } from './api/contracts';
import { usePolling } from './api/usePolling';
import { FormField, ProgressBar } from './components';
import { translate as t } from './i18n';

interface VmWrite {
  name: string; macos_version: string; vcpus: number; cpu_model: string; cpu_topology?: CpuTopology | null; ram_gb: number; disk_size_gb: number;
  auto_start: boolean; audio_device: 'usb-audio' | 'virtio-sound'; audio_enabled: boolean; audio_backend: string; fullscreen: boolean; opencore_show_picker: boolean;
  display_resolution: string; display_count?: number; display_hidpi: boolean; display_refresh_hz: number | null;
  selected_gpu: string | null; selected_gpu_uuid: string | null;
  selected_gpu_device_uuid?: string | null;
  network_enabled: boolean; network_mac: string | null; network_ssh_port?: number; power_sync_mode: 'vm-only'; qemu_extra_args: string[];
  usb_auto_attach: string[];
  additional_disks: AdditionalDiskDraft[];
}

interface Draft {
  id: string;
  name: string;
  macos_version: string;
  vcpus: string;
  cpu_model: string;
  cpu_topology_enabled: boolean;
  cpu_topology_sockets: string;
  cpu_topology_cores: string;
  cpu_topology_threads: string;
  ram_gb: string;
  disk_size_gb: string;
  audio_device: string;
  audio_enabled: boolean;
  audio_backend: string;
  network_enabled: boolean;
  network_mac: string;
  network_ssh_port: string;
  auto_start: boolean;
  fullscreen: boolean;
  display_resolution: string;
  display_count: string;
  display_hidpi: boolean;
  display_refresh_hz: string;
  opencore_show_picker: boolean | null;
  installer_policy: VmProvisionRequest['installer_policy'];
  selected_gpu_bdf: string;
  selected_gpu_uuid: string | null;
  selected_gpu_device_uuid: string | null;
  qemu_extra_args: string[];
  usb_auto_attach: string[];
  additional_disks: AdditionalDiskDraft[];
}

const QemuArgumentsEditor = lazy(() => import('./QemuArgumentsEditor'));

const emptyDraft: Draft = {
  id: '', name: '', macos_version: 'sequoia', vcpus: '8', cpu_model: 'Skylake-Client-noTSX-IBRS', cpu_topology_enabled: false, cpu_topology_sockets: '1', cpu_topology_cores: '8', cpu_topology_threads: '1', ram_gb: '8', disk_size_gb: '64',
  audio_device: 'usb-audio', audio_enabled: true, audio_backend: 'auto', network_enabled: true, network_mac: '', network_ssh_port: '', auto_start: false, fullscreen: true, display_resolution: '1920x1080', display_count: '1', display_hidpi: false, display_refresh_hz: '', opencore_show_picker: null, installer_policy: 'none', selected_gpu_bdf: '', selected_gpu_uuid: null, selected_gpu_device_uuid: null, qemu_extra_args: [], usb_auto_attach: [], additional_disks: [],
};

function asDraft(config: VmConfig): Draft {
  return {
    id: config.id, name: config.name, macos_version: config.macos_version, vcpus: String(config.vcpus), cpu_model: config.cpu_model || 'Skylake-Client-noTSX-IBRS',
    cpu_topology_enabled: Boolean(config.cpu_topology), cpu_topology_sockets: String(config.cpu_topology?.sockets ?? 1), cpu_topology_cores: String(config.cpu_topology?.cores ?? config.vcpus), cpu_topology_threads: String(config.cpu_topology?.threads ?? 1), ram_gb: String(config.ram_gb),
    disk_size_gb: String(config.disk_size_gb), audio_device: config.audio_device, audio_enabled: config.audio_enabled !== false, audio_backend: config.audio_backend || 'auto',
    network_enabled: config.network_enabled !== false, network_mac: config.network_mac ?? '', network_ssh_port: config.network_ssh_port ? String(config.network_ssh_port) : '', auto_start: config.auto_start,
    fullscreen: config.fullscreen,
    // Preview instances and older agents may not yet expose the newer display
    // contract. Keep the editor usable while the backend is upgraded.
    display_resolution: typeof config.display_resolution === 'string' ? config.display_resolution : '1920x1080',
    display_count: String(config.display_count ?? 1),
    display_hidpi: config.display_hidpi === true,
    display_refresh_hz: config.display_refresh_hz == null ? '' : String(config.display_refresh_hz),
    opencore_show_picker: config.opencore_show_picker, installer_policy: 'none', selected_gpu_bdf: config.selected_gpu ?? '',
    selected_gpu_uuid: config.selected_gpu_uuid, qemu_extra_args: [...config.qemu_extra_args], usb_auto_attach: [...(config.usb_auto_attach ?? [])],
    selected_gpu_device_uuid: config.selected_gpu_device_uuid ?? null,
    additional_disks: (config.additional_disks ?? []).flatMap((disk, order) => disk.media_id ? [{ media_id: disk.media_id, controller: disk.controller, order }] : []),
  };
}

function makePayload(draft: Draft, audioDevice: 'usb-audio' | 'virtio-sound', topologyAvailable: boolean, sshForwardAvailable: boolean, displayCountAvailable: boolean): VmWrite {
  return {
    name: draft.name.trim(), macos_version: draft.macos_version,
    vcpus: Number(draft.vcpus), cpu_model: draft.cpu_model,
    ...(topologyAvailable ? { cpu_topology: draft.cpu_topology_enabled ? {
      sockets: Number(draft.cpu_topology_sockets), cores: Number(draft.cpu_topology_cores), threads: Number(draft.cpu_topology_threads),
    } : null } : {}),
    ram_gb: Number(draft.ram_gb), disk_size_gb: Number(draft.disk_size_gb),
    auto_start: draft.auto_start, audio_device: audioDevice, audio_enabled: draft.audio_enabled, audio_backend: draft.audio_backend,
    fullscreen: draft.fullscreen, display_resolution: draft.display_resolution || '1920x1080', display_hidpi: draft.display_hidpi === true,
    ...(displayCountAvailable ? { display_count: Number(draft.display_count) } : {}),
    display_refresh_hz: !draft.display_refresh_hz ? null : Number(draft.display_refresh_hz),
    opencore_show_picker: Boolean(draft.opencore_show_picker),
    selected_gpu: draft.selected_gpu_bdf || null, selected_gpu_uuid: draft.selected_gpu_uuid,
    selected_gpu_device_uuid: draft.selected_gpu_device_uuid,
    ...(sshForwardAvailable ? { network_ssh_port: draft.network_ssh_port.trim() ? Number(draft.network_ssh_port) : 0 } : {}),
    network_enabled: draft.network_enabled, network_mac: draft.network_mac.trim() || null, power_sync_mode: 'vm-only', qemu_extra_args: [...draft.qemu_extra_args], usb_auto_attach: [...draft.usb_auto_attach],
    additional_disks: draft.additional_disks.map((disk, order) => ({ ...disk, order })),
  };
}

function Field({ label, hint, error, children }: { label: string; hint?: string; error?: string; children: React.ReactNode }) {
  return <FormField label={label} hint={hint} error={error}>{children}</FormField>;
}

export default function VmEditor() {
  const { vmId } = useParams();
  const isEdit = Boolean(vmId);
  const navigate = useNavigate();
  const catalog = usePolling<VmListResponse>('/vms', 20000);
  const capabilities = usePolling<CapabilitiesResponse>('/capabilities', 30000);
  const gpuInventory = usePolling<GpuInventoryResponse>('/host/gpus', 30000);
  const usbInventory = usePolling<HostUsbInventoryResponse>('/host/usb', 30000);
  const storageInventory = usePolling<StorageInventory>(isEdit ? '/storage' : null, 30000);
  const original = useMemo(() => catalog.data?.items.find((item) => item.config.id === vmId), [catalog.data, vmId]);
  const initialized = useRef(false);
  const [draft, setDraft] = useState<Draft>(emptyDraft);
  const [revision, setRevision] = useState<string | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [job, setJob] = useState<JobView | null>(null);
  const [jobError, setJobError] = useState<string | null>(null);
  const [cancelling, setCancelling] = useState(false);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [qemuArgumentsValid, setQemuArgumentsValid] = useState(true);
  const virtioAudioCapability = capabilities.data?.items.find((item) => item.id === 'vm.audio.virtio_device');
  const bridgeCapability = capabilities.data?.items.find((item) => item.id === 'vm.network.bridge');
  const forwardingCapability = capabilities.data?.items.find((item) => item.id === 'vm.network.port_forwarding');
  const sshForwardAvailable = forwardingCapability?.available === true;
  const displayCapability = capabilities.data?.items.find((item) => item.id === 'vm.display.multiple');
  const displayCountAvailable = displayCapability?.available === true;
  const additionalDisksCapability = capabilities.data?.items.find((item) => item.id === 'vm.disks.multiple');
  const topologyCapability = capabilities.data?.items.find((item) => item.id === 'vm.cpu.topology');
  const gpuIdentityCapability = capabilities.data?.items.find((item) => item.id === 'gpu.identity');
  const gpuIdentityAvailable = gpuIdentityCapability?.available === true;
  const topologyAvailable = topologyCapability?.available === true;
  const additionalDisksEditable = isEdit && additionalDisksCapability?.available === true;
  const storageChoices = (storageInventory.data?.items ?? []).filter((item) => item.location === 'managed' && item.availability === 'available' && /\.(qcow2|raw|img)$/i.test(item.name) && item.references.every((reference) => reference === draft.id));
  const audioDevice: 'usb-audio' | 'virtio-sound' = virtioAudioCapability?.available
    ? 'virtio-sound'
    : draft.audio_device === 'virtio-sound' ? 'virtio-sound' : 'usb-audio';
  const audioRelease = capabilities.data?.runtime?.reims_release_tag;
  const audioDeviceLabel = virtioAudioCapability?.available
    ? t('VirtIO Sound · Reims {release} (automático)').replace('{release}', audioRelease ?? 'v1.4.0+')
    : draft.audio_device === 'virtio-sound'
      ? t('VirtIO Sound salvo · runtime não confirmado')
      : audioRelease
      ? t('USB Audio + HDA · Reims {release}').replace('{release}', audioRelease)
        : t('Modelo de áudio gerenciado pelo Reims');
  const audioDeviceHint = virtioAudioCapability?.available
    ? t('O Reims cria VirtIO Sound automaticamente no próximo boot. Em x86-64, HDA também fica disponível para o som do OpenCore; o backend do host é escolhido abaixo.')
    : t(virtioAudioCapability?.limitation ?? 'Consultando a release do Reims e os dispositivos do QEMU selecionados.');

  useEffect(() => {
    if (!job || ['succeeded', 'failed', 'cancelled', 'interrupted'].includes(job.state)) return;
    let active = true;
    let timer = 0;
    const refresh = async () => {
      try {
        const current = await api.request<JobView>(`/jobs/${encodeURIComponent(job.id)}`);
        if (active) { setJob(current); setJobError(null); }
      } catch (error) {
        if (active) setJobError(error instanceof Error ? t(error.message) : t('Não foi possível atualizar a tarefa.'));
      }
      if (active) timer = window.setTimeout(refresh, 1500);
    };
    timer = window.setTimeout(refresh, 250);
    return () => { active = false; window.clearTimeout(timer); };
  }, [job?.id, job?.state]);

  useEffect(() => {
    if (job?.state !== 'succeeded') return;
    const provisionedVmId = typeof job.result.vm_id === 'string' ? job.result.vm_id : draft.id;
    if (!provisionedVmId) return;
    const timer = window.setTimeout(() => navigate(`/vms/${encodeURIComponent(provisionedVmId)}/summary`, { replace: true }), 1600);
    return () => window.clearTimeout(timer);
  }, [job?.state, draft.id, navigate]);

  useEffect(() => {
    if (!original || initialized.current) return;
    initialized.current = true;
    setDraft(asDraft(original.config));
    setRevision(`"${original.revision || original.config.revision}"`);
  }, [original]);

  useEffect(() => {
    let active = true;
    if (isEdit && vmId) {
      void api.requestWithHeaders<VmConfigEnvelope>(`/vms/${encodeURIComponent(vmId)}/config`).then(({ data, headers }) => {
        if (active) setRevision(headers.get('ETag') ?? `"${data.revision}"`);
      }).catch(() => undefined);
    }
    return () => { active = false; };
  }, [isEdit, vmId]);

  const update = <K extends keyof Draft>(key: K, value: Draft[K]) => setDraft((current) => ({ ...current, [key]: value }));
  const selectGpu = (bdf: string) => {
    const selected = selectableGpus.find((gpu) => gpu.pci_bdf === bdf);
    setDraft((current) => ({
      ...current,
      selected_gpu_bdf: bdf,
      selected_gpu_uuid: selected?.vulkan_uuid ?? null,
      selected_gpu_device_uuid: selected?.reims_device_uuid ?? null,
    }));
  };
  const displayWidth = Number((draft.display_resolution || '1920x1080').split('x')[0]);
  const validate = () => {
    const errors: Record<string, string> = {};
    if (isEdit && !/^[a-z0-9][a-z0-9-]{0,62}$/.test(draft.id.trim())) errors.id = t('Use até 63 caracteres: letras minúsculas, números ou hífen.');
    if (draft.name.trim().length < 1 || draft.name.trim().length > 80) errors.name = t('Informe um nome de até 80 caracteres.');
    if (draft.network_mac.trim() && (!/^(?:[0-9a-f]{2}:){5}[0-9a-f]{2}$/i.test(draft.network_mac.trim()) || (Number.parseInt(draft.network_mac.slice(0, 2), 16) & 1) === 1)) errors.network_mac = t('Informe um MAC unicast com seis pares hexadecimais.');
    if (draft.network_ssh_port.trim()) {
      const port = Number(draft.network_ssh_port);
      if (!Number.isInteger(port) || port < 1 || port > 65535) errors.network_ssh_port = t('Use uma porta TCP entre 1 e 65535, ou deixe em branco para desativar.');
      else if (!draft.network_enabled) errors.network_ssh_port = t('Habilite a rede da VM para encaminhar a porta SSH.');
    }
    if (displayCountAvailable) {
      const count = Number(draft.display_count);
      if (!Number.isInteger(count) || count < 1 || count > 8) errors.display_count = t('Escolha de 1 a 8 displays.');
    }
    for (const [key, label, min, max] of [['vcpus', t('vCPU'), 1, 256], ['ram_gb', 'RAM', 1, 2048], ['disk_size_gb', t('Disco'), 16, 4096]] as const) {
      const value = Number(draft[key]);
      if (!Number.isInteger(value) || value < min || value > max) errors[key] = t('{label}: informe um número inteiro entre {min} e {max}.').replace('{label}', t(label)).replace('{min}', String(min)).replace('{max}', String(max));
    }
    if (topologyAvailable && draft.cpu_topology_enabled) {
      const factors = [draft.cpu_topology_sockets, draft.cpu_topology_cores, draft.cpu_topology_threads].map(Number);
      if (factors.some((value) => !Number.isInteger(value) || value < 1 || value > 256)) {
        errors.cpu_topology = t('Sockets, cores e threads devem ser inteiros entre 1 e 256.');
      } else if (factors.reduce((product, value) => product * value, 1) !== Number(draft.vcpus)) {
        errors.cpu_topology = t('Sockets × cores × threads deve ser igual à quantidade de vCPUs.');
      }
    }
    setFieldErrors(errors);
    return Object.keys(errors).length === 0;
  };

  const save = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setFormError(null);
    setSaved(false);
    if (!qemuArgumentsValid) { setFormError(t('Corrija o array JSON dos argumentos QEMU antes de continuar.')); return; }
    if (!validate()) return;
    const expectedEtag = revision;
    if (isEdit && !expectedEtag) { setFormError(t('Não foi possível obter a revisão ETag atual. Atualize o catálogo antes de salvar.')); return; }
    if (isEdit && original?.config.power_sync_mode !== 'vm-only') { setFormError(t('Esta VM possui uma política de energia antiga que o painel não pode preservar com segurança. A configuração permanece bloqueada.')); return; }
    setSaving(true);
    try {
      if (!isEdit) {
        const payload: VmProvisionRequest = {
          idempotency_key: crypto.randomUUID(), name: draft.name.trim(), macos_version: draft.macos_version,
          vcpus: Number(draft.vcpus), ram_gb: Number(draft.ram_gb), disk_size_gb: Number(draft.disk_size_gb),
          cpu_model: draft.cpu_model,
          ...(topologyAvailable ? { cpu_topology: draft.cpu_topology_enabled ? {
            sockets: Number(draft.cpu_topology_sockets), cores: Number(draft.cpu_topology_cores), threads: Number(draft.cpu_topology_threads),
          } : null } : {}),
          audio_device: audioDevice, audio_backend: draft.audio_backend, audio_enabled: draft.audio_enabled, network_enabled: draft.network_enabled,
          network_mac: draft.network_mac.trim() || null,
          ...(sshForwardAvailable ? { network_ssh_port: draft.network_ssh_port.trim() ? Number(draft.network_ssh_port) : 0 } : {}),
          auto_start: draft.auto_start, fullscreen: draft.fullscreen, display_resolution: draft.display_resolution || '1920x1080',
          ...(displayCountAvailable ? { display_count: Number(draft.display_count) } : {}),
          display_hidpi: draft.display_hidpi === true, display_refresh_hz: !draft.display_refresh_hz ? null : Number(draft.display_refresh_hz),
          selected_gpu_bdf: draft.selected_gpu_bdf || null,
          selected_gpu_uuid: gpuInventory.data?.items.find((gpu) => gpu.pci_bdf === draft.selected_gpu_bdf)?.vulkan_uuid ?? null,
          ...(gpuIdentityAvailable ? { selected_gpu_device_uuid: gpuInventory.data?.items.find((gpu) => gpu.pci_bdf === draft.selected_gpu_bdf)?.reims_device_uuid ?? null } : {}),
          qemu_extra_args: [...draft.qemu_extra_args],
          usb_auto_attach: [...draft.usb_auto_attach],
          installer_policy: draft.installer_policy,
          opencore_show_picker: draft.opencore_show_picker,
        };
        const accepted = await api.request<JobAccepted>('/vms/provision', { method: 'POST', body: JSON.stringify(payload) });
        setJob({ id: accepted.job_id, kind: 'vm_provision', state: accepted.state, stage: 'queued', progress_percent: 0,
          created_at: new Date().toISOString(), updated_at: new Date().toISOString(), result: {}, error: null, cancellable: false, retryable: false });
      } else {
        const payload = makePayload(draft, audioDevice, topologyAvailable, sshForwardAvailable, displayCountAvailable);
        const response = await api.requestWithHeaders<VmConfigEnvelope>(`/vms/${encodeURIComponent(draft.id)}/config`, {
          method: 'PUT', headers: { 'If-Match': expectedEtag! }, body: JSON.stringify(payload),
        });
        setRevision(response.headers.get('ETag') ?? `"${response.data.revision}"`);
        setSaved(true);
        window.setTimeout(() => navigate(`/vms/${encodeURIComponent(response.data.config.id)}/summary`, { replace: true }), 700);
      }
    } catch (error) {
      if (error instanceof ApiError) {
        setFieldErrors(Object.fromEntries(Object.entries(error.fieldErrors).map(([field, message]) => [field, t(message)])));
        setFormError(error.status === 409 || error.status === 412
          ? t('Esta configuração foi alterada em outro lugar. Recarregue a VM para revisar a versão mais recente antes de salvar.')
          : t(error.message));
      } else setFormError(error instanceof Error ? t(error.message) : t('Não foi possível salvar a VM.'));
    } finally { setSaving(false); }
  };

  const cancelProvisioning = async () => {
    if (!job?.cancellable || cancelling) return;
    setCancelling(true);
    setJobError(null);
    try { setJob(await api.request<JobView>(`/jobs/${encodeURIComponent(job.id)}/cancel`, { method: 'POST' })); }
    catch (error) { setJobError(error instanceof Error ? t(error.message) : t('Não foi possível cancelar a tarefa.')); }
    finally { setCancelling(false); }
  };

  if (isEdit && catalog.loading && !catalog.data) return <div className="vm-editor-page"><div className="data-state" role="status">{t('Carregando configuração da VM…')}</div></div>;
  if (isEdit && catalog.data && !original) return <div className="vm-editor-page"><button className="back-link" onClick={() => navigate('/vms')}><ArrowLeft size={16}/>{t('Voltar às VMs')}</button><div className="inline-message error-message" role="alert">{t('VM não encontrada no catálogo atual.')}</div></div>;

  const unavailableGpu = gpuInventory.data?.items.filter((gpu) => !gpu.selection_supported) ?? [];
  const cpuModels = [...new Set([draft.cpu_model, ...(capabilities.data?.runtime?.qemu_cpu_models ?? [])])].filter(Boolean).sort((a, b) => a.localeCompare(b));
  const audioBackends = [...new Set(['auto', draft.audio_backend, ...(capabilities.data?.runtime?.qemu_audio_backends ?? [])])].filter(Boolean);
  const selectableGpus = gpuInventory.data?.stale ? [] : gpuInventory.data?.items.filter((gpu) => gpu.selection_supported) ?? [];
  const legacyPowerMode = isEdit && original?.config.power_sync_mode !== 'vm-only';
  return <div className="vm-editor-page">
    <button className="back-link" onClick={() => navigate(isEdit ? `/vms/${encodeURIComponent(draft.id)}/summary` : '/vms')}><ArrowLeft size={16}/>{t('Voltar')} {isEdit ? t('à VM') : t('às máquinas virtuais')}</button>
    <header className="vm-editor-heading"><div><div className="eyebrow"><span className="eyebrow-line"/>{isEdit ? t('CONFIGURAÇÃO DA VM') : t('NOVO RECURSO')}</div><h1>{isEdit ? t('Editar {name}').replace('{name}', original?.config.name ?? t('máquina virtual')) : t('Criar máquina virtual')}</h1><p>{isEdit ? t('Revise os parâmetros persistidos para a próxima partida.') : t('Defina os recursos e acompanhe o provisionamento do disco e da identidade OpenCore.')}</p></div><span className="vm-editor-revision">{isEdit ? `REV ${revision ?? '—'}` : t('NOVO PROVISIONAMENTO')}</span></header>
    {formError && <div className="inline-message error-message" role="alert"><AlertTriangle size={16}/>{formError}{(Object.keys(fieldErrors).length > 0) && <button className="inline-retry" type="button" onClick={() => { void catalog.refresh(); }}>{t('Recarregar catálogo')}</button>}</div>}
    {saved && isEdit && <div className="inline-message success-message" role="status"><Check size={16}/>{t('Configuração salva. Abrindo a página da VM…')}</div>}
    <form className="vm-editor-form" onSubmit={save} noValidate>
      <section className="panel vm-editor-section"><div className="vm-editor-section-heading"><span className="vm-editor-section-icon"><Server size={18}/></span><div><h2>{t('Identificação')}</h2><p>{t('Nome e versão do sistema oferecido pelo vHackintosh.')}</p></div></div><div className="vm-editor-grid">
        <Field label={t('Identificador')} hint={isEdit ? t('Não pode ser alterado depois da criação.') : t('Gerado automaticamente ao criar a VM, com sufixo exclusivo.')} error={fieldErrors.id}><input readOnly autoComplete="off" value={draft.id} placeholder={isEdit ? undefined : t('Gerado automaticamente')}/></Field>
        <Field label={t('Nome da VM')} error={fieldErrors.name}><input required maxLength={80} value={draft.name} onChange={(e) => update('name', e.target.value)}/></Field>
        <Field label={t('Versão macOS')} hint={isEdit ? t('A versão instalada não pode ser alterada editando a VM.') : t('Versões atualmente oferecidas pelo assistente do vHackintosh.')}><select disabled={isEdit} value={draft.macos_version} onChange={(e) => update('macos_version', e.target.value)}><option value="sequoia">{t('macOS Sequoia 15 · estável')}</option><option value="tahoe">{t('macOS Tahoe 26 · experimental')}</option><option value="ventura">{t('macOS Ventura 13 · legado')}</option></select></Field>
      </div></section>

      <section className="panel vm-editor-section"><div className="vm-editor-section-heading"><span className="vm-editor-section-icon"><Cpu size={18}/></span><div><h2>{t('Recursos da máquina')}</h2><p>{t('Limites básicos para o hardware virtual.')}</p></div></div><div className="vm-editor-grid vm-editor-grid-three">
        {([['vcpus', t('vCPU'), 1, 256], ['ram_gb', t('Memória (GiB)'), 1, 2048], ['disk_size_gb', t('Disco (GiB)'), 16, 4096]] as const).map(([key, label, min, max]) => <Field key={key} label={label} error={fieldErrors[key]} hint={key === 'disk_size_gb' && original?.config.disk_path ? t('Há um caminho de disco registrado; o tamanho continua apenas informativo.') : undefined}><input type="number" inputMode="numeric" min={min} max={max} step="1" required disabled={key === 'disk_size_gb' && Boolean(original?.config.disk_path)} value={draft[key]} onChange={(e) => update(key, e.target.value)}/></Field>)}
        <Field label={t('Modelo de CPU')} hint={capabilities.data?.runtime?.status === 'available' ? t('Sugestões vêm do QEMU detectado; o servidor aceita apenas modelos presentes nesse inventário. Skylake sem TSX é o padrão recomendado para macOS. Vale no próximo boot.') : t('Inventário QEMU indisponível; o modelo atual será preservado até o runtime responder.')}><input list="qemu-cpu-models" maxLength={100} value={draft.cpu_model} onChange={(event) => update('cpu_model', event.target.value)} aria-label={t('Modelo de CPU')}/><datalist id="qemu-cpu-models">{cpuModels.map((model) => <option key={model} value={model}/>)}</datalist></Field>
        {topologyAvailable && <div className="vm-editor-topology">
          <label className="vm-toggle"><input type="checkbox" checked={draft.cpu_topology_enabled} onChange={(event) => update('cpu_topology_enabled', event.target.checked)}/><span><strong>{t('Personalizar topologia de CPU')}</strong><small>{t('O padrão do Reims continua sendo 1 socket × N cores × 1 thread.')}</small></span></label>
          {draft.cpu_topology_enabled && <div className="vm-editor-grid vm-editor-grid-three">
            {([['cpu_topology_sockets', t('Sockets')], ['cpu_topology_cores', t('Cores por socket')], ['cpu_topology_threads', t('Threads por core')]] as const).map(([key, label]) => <Field key={key} label={label} error={fieldErrors.cpu_topology}><input type="number" inputMode="numeric" min={1} max={256} step={1} value={draft[key]} onChange={(event) => update(key, event.target.value)}/></Field>)}
            <p className="vm-field-hint">{t('O produto deve corresponder às {count} vCPU(s). A configuração exige o suporte `cpu_topology` anunciado pelo Reims e vale no próximo boot.').replace('{count}', draft.vcpus || '—')}</p>
          </div>}
        </div>}
        {!topologyAvailable && isEdit && original?.config.cpu_topology && <p className="vm-field-hint">{t('A topologia salva ({sockets} × {cores} × {threads}) será preservada. O Reims atual não anuncia suporte para alterá-la.').replace('{sockets}', String(original.config.cpu_topology.sockets)).replace('{cores}', String(original.config.cpu_topology.cores)).replace('{threads}', String(original.config.cpu_topology.threads))}</p>}
        {isEdit && original?.config.disk_path && <Field label={t('Caminho do disco')} hint={t('Gerenciado pelo appliance; não pode ser alterado por este formulário.')}><input type="text" readOnly value={original.config.disk_path}/></Field>}
      </div></section>

      {isEdit && <section className="panel vm-editor-section"><div className="vm-editor-section-heading"><span className="vm-editor-section-icon"><HardDrive size={18}/></span><div><h2>{t('Discos adicionais')}</h2><p>{t('Anexe imagens existentes da biblioteca ao próximo boot, sem compartilhar disco gravável entre VMs.')}</p></div></div>
        {!additionalDisksEditable && <div className="vm-field-hint">{additionalDisksCapability?.limitation ?? t('Consultando suporte do Reims para discos adicionais.')} A criação da VM continua usando o disco principal configurado acima.</div>}
        {storageInventory.loading && !storageInventory.data && <div className="data-state" role="status">{t('Consultando imagens gerenciadas…')}</div>}
        {storageInventory.error && <div className="inline-message error-message" role="status"><AlertTriangle size={16}/>{t('Não foi possível consultar o armazenamento; nenhuma imagem nova pode ser selecionada.')}</div>}
        {draft.additional_disks.length > 0 && <div className="vm-additional-disks">{draft.additional_disks.map((disk, index) => {
          const selectedItem = storageInventory.data?.items.find((item) => item.id === disk.media_id);
          return <div className="vm-additional-disk" key={`${disk.media_id}-${index}`}>
            <Field label={t('Disco {index}').replace('{index}', String(index + 1))} hint={selectedItem ? t('{name} · imagem existente').replace('{name}', selectedItem.name) : t('Esta seleção não aparece no inventário atual; remova-a apenas se quiser desconectar o disco.')}>
              <select aria-label={t('Imagem do disco {index}').replace('{index}', String(index + 1))} disabled={!additionalDisksEditable || storageInventory.loading || !storageInventory.data} value={disk.media_id} onChange={(event) => update('additional_disks', draft.additional_disks.map((entry, entryIndex) => entryIndex === index ? { ...entry, media_id: event.target.value } : entry))}>
                {!selectedItem && <option value={disk.media_id}>{disk.media_id} · seleção salva</option>}
                {storageChoices.map((item) => <option key={item.id} value={item.id}>{item.name} · {item.virtual_size_bytes == null ? t('tamanho indisponível') : `${(item.virtual_size_bytes / (1024 ** 3)).toFixed(1)} GiB`}</option>)}
              </select>
            </Field>
            <Field label={t('Controlador')}>
              <select aria-label={`Controlador do disco ${index + 1}`} disabled={!additionalDisksEditable} value={disk.controller} onChange={(event) => update('additional_disks', draft.additional_disks.map((entry, entryIndex) => entryIndex === index ? { ...entry, controller: event.target.value as AdditionalDiskDraft['controller'] } : entry))}>
                <option value="ahci">AHCI</option><option value="nvme">NVMe</option>
              </select>
            </Field>
            {additionalDisksEditable && <button type="button" className="button button-secondary vm-additional-disk-remove" onClick={() => update('additional_disks', draft.additional_disks.filter((_, entryIndex) => entryIndex !== index))}>{t('Remover')}</button>}
          </div>;
        })}</div>}
        {additionalDisksEditable && <div className="vm-additional-disk-actions"><Field label={t('Imagem gerenciada')}>
          <select aria-label={t('Selecionar imagem para anexar')} value="" disabled={!storageInventory.data || storageInventory.loading || draft.additional_disks.length >= 15 || storageChoices.filter((item) => !draft.additional_disks.some((disk) => disk.media_id === item.id)).length === 0} onChange={(event) => { const mediaId = event.target.value; if (!mediaId) return; update('additional_disks', [...draft.additional_disks, { media_id: mediaId, controller: 'nvme', order: draft.additional_disks.length }]); }}>
            <option value="">{draft.additional_disks.length >= 15 ? t('Limite de 15 discos atingido') : storageInventory.loading ? t('Carregando biblioteca…') : t('Escolha um disco existente')}</option>
            {storageChoices.filter((item) => !draft.additional_disks.some((disk) => disk.media_id === item.id)).map((item) => <option key={item.id} value={item.id}>{item.name} · {item.virtual_size_bytes == null ? t('tamanho indisponível') : `${(item.virtual_size_bytes / (1024 ** 3)).toFixed(1)} GiB`}</option>)}
          </select>
        </Field><small className="vm-field-hint">{t('Somente QCOW2/RAW existentes no volume gerenciado. O Reims precisa anunciar suporte; discos compartilhados, caminhos externos e symlinks são recusados. A VM deve estar parada; a alteração vale no próximo boot.')}</small></div>}
        {additionalDisksEditable && storageInventory.data && storageChoices.length === 0 && <p className="vm-field-hint">{t('Nenhuma imagem gerenciada livre foi encontrada. Envie ou importe uma imagem na página Armazenamento.')}</p>}
      </section>}

      <section className="panel vm-editor-section"><div className="vm-editor-section-heading"><span className="vm-editor-section-icon"><Monitor size={18}/></span><div><h2>{t('Gráficos, áudio e tela')}</h2><p>{t('As opções bloqueadas dependem de suporte explícito do runtime.')}</p></div></div>
        <label className="vm-toggle"><input type="checkbox" checked={draft.audio_enabled} onChange={(event) => update('audio_enabled', event.target.checked)}/><span><strong>{t('Habilitar áudio')}</strong><small>{t('USB Audio + HDA do Reims. A alteração vale no próximo boot.')}</small></span></label>
        {selectableGpus.length === 0 && <div className="gpu-unavailable" role="note"><AlertTriangle size={17}/><div><strong>{t('Seleção de GPU Reims indisponível')}</strong><p>{gpuInventory.data?.stale ? `${t('{message} Atualize a leitura antes de escolher uma placa.').replace('{message}', t(gpuInventory.data.message ?? 'O inventário está desatualizado.'))}` : unavailableGpu.length ? t('{count} placa(s) detectada(s), mas o backend atual não confirma seleção exata por VM.').replace('{count}', String(unavailableGpu.length)) : t('O inventário ainda não confirma seleção exata de uma placa física pelo backend Reims.')} {t('A GPU atualmente salva será preservada na edição.')}</p>{original?.config.selected_gpu && <small>{t('Configurada: {gpu}').replace('{gpu}', original.config.selected_gpu_name ?? original.config.selected_gpu)}</small>}</div></div>}
        <div className="vm-editor-grid"><Field label={t('Modo de GPU')}><select disabled value={original?.config.gpu_mode ?? 'reims-vgpu'}><option value={original?.config.gpu_mode ?? 'reims-vgpu'}>{original?.config.gpu_mode ?? t('Reims vGPU')}</option></select><small className="vm-field-hint">{t('Modo exibido para compatibilidade. Alteração indisponível.')}</small></Field><Field label={t('Placa física')}><select disabled={selectableGpus.length === 0} value={draft.selected_gpu_bdf} onChange={(e) => selectGpu(e.target.value)}><option disabled={isEdit && Boolean(original?.config.selected_gpu)} value="">{selectableGpus.length ? t('Padrão do appliance') : t('Seleção exata indisponível')}</option>{draft.selected_gpu_bdf && !selectableGpus.some((gpu) => gpu.pci_bdf === draft.selected_gpu_bdf) && <option value={draft.selected_gpu_bdf}>{original?.config.selected_gpu_name ?? draft.selected_gpu_bdf} · {t('seleção atual')}</option>}{selectableGpus.map((gpu) => <option key={gpu.pci_bdf} value={gpu.pci_bdf}>{gpu.name} · {gpu.pci_bdf}{gpu.reims_device_uuid ? ` · Reims ${gpu.reims_device_uuid}` : ''}</option>)}</select><small className="vm-field-hint">{selectableGpus.length ? gpuIdentityAvailable ? t('Seleção identificada pelo UUID Reims e endereço PCI; a troca vale no próximo boot.') : t('A troca será aplicada no próximo boot após validação da associação BDF/UUID Vulkan.') : isEdit ? t('O inventário não oferece uma seleção exata validada; a configuração salva será preservada.') : t('O inventário não oferece uma seleção validada pelo runtime.')}</small></Field><Field label={t('Dispositivo de áudio')}><select disabled value={audioDevice}><option value={audioDevice}>{audioDeviceLabel}</option></select><small className="vm-field-hint">{audioDeviceHint}</small></Field><Field label={t('Backend de áudio do host')} hint={t('Aplicado pelo Reims no próximo boot. As opções vêm do QEMU selecionado.')}><select value={draft.audio_backend} disabled={!draft.audio_enabled || (!capabilities.data?.runtime?.qemu_audio_backends?.length && draft.audio_backend === 'auto')} onChange={(event) => update('audio_backend', event.target.value)}>{audioBackends.map((backend) => <option key={backend} value={backend}>{backend === 'auto' ? t('Automático · Reims escolhe o backend do host') : backend}</option>)}</select></Field></div>
        <div className="vm-editor-grid vm-editor-grid-three">
          <Field label={t('Resolução da tela')} hint={t('Aplicada ao próximo boot da VM.')}><select value={draft.display_resolution || '1920x1080'} onChange={(e) => { update('display_resolution', e.target.value); if (e.target.value === 'host') update('display_hidpi', false); }}><option value="host">{t('Usar modos do monitor host')}</option><option value="1280x720">1280 × 720</option><option value="1920x1080">1920 × 1080</option><option value="2560x1440">2560 × 1440</option><option value="3840x2160">3840 × 2160</option></select></Field>
          {displayCountAvailable && <Field label={t('Quantidade de displays')} error={fieldErrors.display_count} hint={t('O Reims abre uma janela local para cada saída; o macOS pode não habilitar todas em algumas versões.')}><select value={draft.display_count} onChange={(event) => update('display_count', event.target.value)}>{Array.from({ length: 8 }, (_, index) => index + 1).map((count) => <option key={count} value={count}>{count} {count === 1 ? 'display' : 'displays'}</option>)}</select></Field>}
          <Field label={t('Taxa de atualização')} hint={t('Vazio usa a taxa oferecida pelo monitor host.')}><input type="number" inputMode="numeric" min="1" max="1000" step="1" placeholder={t('Automática')} value={draft.display_refresh_hz ?? ''} onChange={(e) => update('display_refresh_hz', e.target.value)}/></Field>
          <label className="vm-toggle"><input type="checkbox" checked={draft.display_hidpi === true} disabled={(draft.display_resolution || '1920x1080') === 'host' || displayWidth < 1920} onChange={(e) => update('display_hidpi', e.target.checked)}/><span><strong>HiDPI</strong><small>{t('Escala a interface para metade da resolução em pixels. Exige largura mínima de 1920.')}</small></span></label>
        </div>
        {!displayCountAvailable && isEdit && (original?.config.display_count ?? 1) > 1 && <p className="vm-field-hint">{t('A VM tem {count} displays configurados no Reims; essa opção será preservada enquanto o runtime não confirmar sua edição.').replace('{count}', String(original?.config.display_count))}</p>}
        <div className="vm-editor-toggle-grid"><label className="vm-toggle"><input type="checkbox" checked={draft.fullscreen} onChange={(e) => update('fullscreen', e.target.checked)}/><span><strong>{t('Tela cheia sem bordas')}</strong><small>{t('Aplicada na próxima inicialização. Desative para abrir em janela.')}</small></span></label><label className="vm-toggle"><input type="checkbox" checked={isEdit ? Boolean(original?.config.opencore_show_picker) : (draft.opencore_show_picker ?? draft.installer_policy !== 'none')} disabled={isEdit} onChange={(e) => update('opencore_show_picker', e.target.checked)}/><span><strong>{t('Exibir seletor OpenCore')}</strong><small>{isEdit ? t('Somente leitura até o painel oferecer regeneração segura do OpenCore.') : t('Por padrão acompanha a mídia de instalação; você pode alterar essa opção antes de criar.')}</small></span></label></div>
      </section>

      <section className="panel vm-editor-section"><div className="vm-editor-section-heading"><span className="vm-editor-section-icon"><Server size={18}/></span><div><h2>{t('Rede')}</h2><p>{t('Opções estruturadas do schema atual do Reims.')}</p></div></div><div className="vm-editor-grid"><label className="vm-toggle"><input type="checkbox" checked={draft.network_enabled} onChange={(event) => update('network_enabled', event.target.checked)}/><span><strong>{t('Habilitar rede da VM')}</strong><small>{t('A configuração é aplicada no próximo boot.')}</small></span></label><Field label={t('Endereço MAC')} error={fieldErrors.network_mac} hint={t('Deixe em branco para preservar o endereço atual ou usar o padrão gerado pelo Reims.')}><input autoComplete="off" spellCheck={false} maxLength={17} placeholder={t('Automático')} value={draft.network_mac} onChange={(event) => update('network_mac', event.target.value.toLowerCase())}/></Field>{sshForwardAvailable && <Field label={t('Porta local para SSH')} error={fieldErrors.network_ssh_port} hint={t('Opcional. Reims encaminha 127.0.0.1:porta do host para a porta 22 da VM; não expõe SSH à rede LAN.')}><input type="number" inputMode="numeric" min={1} max={65535} step={1} placeholder={t('Desativado')} value={draft.network_ssh_port} disabled={!draft.network_enabled} onChange={(event) => update('network_ssh_port', event.target.value)}/></Field>}</div><p className="vm-form-note">{t('Modo disponível: user/NAT. A VM acessa a rede por NAT; o painel não cria bridge no host.')}</p>{bridgeCapability && <p className="vm-field-hint"><strong>{t(bridgeCapability.label)}:</strong> {t(bridgeCapability.limitation ?? bridgeCapability.description ?? 'Opção não disponível no runtime atual.')}</p>}{forwardingCapability && <p className="vm-field-hint"><strong>{t(forwardingCapability.label)}:</strong> {sshForwardAvailable ? t('Somente SSH para 127.0.0.1, com porta exclusiva por VM; a alteração vale no próximo boot.') : t(forwardingCapability.limitation ?? forwardingCapability.description ?? 'Opção não disponível no runtime atual.')}</p>}{!sshForwardAvailable && original?.config.network_ssh_port && <p className="vm-field-hint">{t('A porta SSH local {port} está salva e será preservada enquanto este runtime não confirmar capacidade de edição.').replace('{port}', String(original.config.network_ssh_port))}</p>}</section>

      <section className="panel vm-editor-section"><div className="vm-editor-section-heading"><span className="vm-editor-section-icon"><Layers3 size={18}/></span><div><h2>{t('Dispositivos USB')}</h2><p>{t('Escolha dispositivos únicos para anexar automaticamente no próximo boot.')}</p></div></div>
        {usbInventory.data?.stale && <div className="inline-message error-message" role="status"><AlertTriangle size={16}/>{t(usbInventory.data.message ?? 'Inventário USB indisponível.')}</div>}
        {usbInventory.error && <div className="inline-message error-message" role="status"><AlertTriangle size={16}/>{t('Não foi possível consultar os dispositivos USB. Atualize o painel para tentar novamente; novas seleções ficam indisponíveis.')}</div>}
        {!usbInventory.data && usbInventory.loading && <div className="data-state" role="status">{t('Consultando dispositivos USB do host…')}</div>}
        {draft.usb_auto_attach.filter((id) => !usbInventory.data?.items.some((item) => item.device_id === id)).map((id) => <label className="vm-toggle" key={`stale-${id}`}><input type="checkbox" checked onChange={() => update('usb_auto_attach', draft.usb_auto_attach.filter((value) => value !== id))}/><span><strong>{t('{id} · seleção salva').replace('{id}', id)}</strong><small>{t('O dispositivo não está visível agora. A seleção atual será preservada enquanto marcada.')}</small></span></label>)}
        {usbInventory.data?.items.map((item) => { const selected = draft.usb_auto_attach.includes(item.device_id); return <label className={`vm-toggle${item.auto_attach_supported ? '' : ' is-disabled'}`} key={item.device_id}><input type="checkbox" disabled={!item.auto_attach_supported || usbInventory.data?.stale || (!selected && draft.usb_auto_attach.length >= 32)} checked={selected} onChange={(event) => update('usb_auto_attach', event.target.checked ? [...new Set([...draft.usb_auto_attach, item.device_id])] : draft.usb_auto_attach.filter((id) => id !== item.device_id))}/><span><strong>{item.label} · {item.device_id}{item.device_count > 1 ? ` (${item.device_count} ${t('encontrados')})` : ''}</strong><small>{item.auto_attach_supported ? t('Anexar ao iniciar a VM. A alteração terá efeito no próximo boot.') : t(item.limitation ?? 'Este dispositivo não pode ser selecionado com segurança.')}</small></span></label>; })}
        {usbInventory.data && usbInventory.data.items.length === 0 && draft.usb_auto_attach.length === 0 && <p className="vm-field-hint">{t('Nenhum dispositivo USB anexável foi encontrado no host.')}</p>}
      </section>

      <details className="panel vm-editor-section vm-advanced" onToggle={(event) => setAdvancedOpen(event.currentTarget.open)}><summary><span className="vm-editor-section-icon"><Layers3 size={18}/></span><span><strong>{t('Identidade e opções avançadas')}</strong><small>{t('Identidade gerenciada e editor técnico QEMU')}</small></span></summary><div className="vm-editor-grid vm-editor-grid-three vm-editor-advanced-content">
        {original && <><Field label={t('Modelo SMBIOS')}><input readOnly value={original.config.smbios.model}/></Field><Field label={t('Número de série')}><input readOnly value={original.config.smbios.serial_number}/></Field><Field label={t('Número da placa')}><input readOnly value={original.config.smbios.board_serial}/></Field><Field label="SMUUID"><input readOnly value={original.config.smbios.smuuid}/></Field><Field label="ROM"><input readOnly value={original.config.smbios.rom}/></Field></>}
        {advancedOpen && <div className="qemu-editor-host"><Suspense fallback={<div className="chart-loading" role="status">{t('Carregando editor QEMU…')}</div>}><QemuArgumentsEditor value={draft.qemu_extra_args} onChange={(tokens) => update('qemu_extra_args', tokens)} onValidityChange={setQemuArgumentsValid}/></Suspense></div>}
        <p className="vm-form-note">{t('SMBIOS e dispositivos USB continuam sob controle do appliance. O editor não executa comandos nem envia argumentos arbitrários ao QEMU.')}</p>
      </div></details>

      <details className="panel vm-editor-section vm-advanced"><summary><span className="vm-editor-section-icon"><Cpu size={18}/></span><span><strong>{t('Inventário de capacidades QEMU/Reims')}</strong><small>{t('Versões, opções de linha de comando, dispositivos e limites estruturados')}</small></span></summary><div className="vm-editor-advanced-content"><p className="vm-form-note">{t('QEMU informa o que o binário contém; Reims informa as opções aceitas pelo comando de configuração. Um recurso listado não significa que o painel permita ativá-lo.')}</p><div className="vm-capability-catalog"><div><strong>{t('Versões detectadas')}</strong><span>Reims: {capabilities.data?.runtime?.reims_version ?? t('indisponível')} · QEMU: {capabilities.data?.runtime?.qemu_version ?? t('indisponível')}</span></div>{Object.entries(capabilities.data?.runtime?.qemu_device_categories ?? {}).map(([category, devices]) => <div key={category}><strong>{category === 'storage' ? t('Armazenamento') : category === 'network' ? t('Rede') : category === 'audio' ? t('Áudio') : category === 'usb' ? 'USB' : category === 'display' ? t('Vídeo') : category}</strong><span>{devices.length ? devices.join(', ') : t('Nenhum dispositivo detectado')}</span></div>)}<div><strong>{t('CPU / máquinas')}</strong><span>{capabilities.data?.runtime ? t('{cpus} modelos de CPU · {machines} máquinas').replace('{cpus}', String(capabilities.data.runtime.qemu_cpu_models.length)).replace('{machines}', String(capabilities.data.runtime.qemu_machine_types.length)) : t('Inventário indisponível')}</span></div><div><strong>{t('Todos os dispositivos QEMU')}</strong><span>{capabilities.data?.runtime?.qemu_devices.join(', ') || t('Indisponíveis')}</span></div><div><strong>{t('Todos os modelos de CPU')}</strong><span>{capabilities.data?.runtime?.qemu_cpu_models.join(', ') || t('Indisponíveis')}</span></div><div><strong>{t('Tipos de máquina')}</strong><span>{capabilities.data?.runtime?.qemu_machine_types.join(', ') || t('Indisponíveis')}</span></div><div><strong>{t('Backends de áudio')}</strong><span>{capabilities.data?.runtime?.qemu_audio_backends.join(', ') || t('Indisponíveis')}</span></div><div><strong>{t('Opções Reims (comando set)')}</strong><span>{capabilities.data?.runtime?.reims_set_options.join(', ') || t('Indisponíveis')}</span></div><div><strong>{t('Opções Reims (create)')}</strong><span>{capabilities.data?.runtime?.reims_create_options.join(', ') || t('Indisponíveis')}</span></div><div><strong>{t('Opções QEMU')}</strong><span>{capabilities.data?.runtime?.qemu_command_options.join(', ') || t('Indisponíveis')}</span></div></div>{(capabilities.data?.items ?? []).filter((item) => item.id.startsWith('vm.audio.') || item.id.startsWith('vm.disks.')).map((item) => <p className="vm-field-hint" key={item.id}><strong>{t(item.label)}:</strong> {t(item.supported ? item.description ?? 'Recurso disponível.' : item.limitation ?? item.description ?? 'Opção não disponível no runtime atual.')}</p>)}</div></details>

      {!isEdit && <section className="panel vm-editor-section"><div className="vm-editor-section-heading"><span className="vm-editor-section-icon"><Download size={18}/></span><div><h2>{t('Mídia de instalação')}</h2><p>{t('Escolha como o appliance deve tratar o instalador do macOS.')}</p></div></div><Field label={t('Origem do instalador')}><select value={draft.installer_policy} onChange={(e) => update('installer_policy', e.target.value as Draft['installer_policy'])}><option value="none">{t('Sem instalador · preparar a VM')}</option><option value="cached">{t('Usar instalador já armazenado')}</option><option value="download">{t('Baixar se necessário')}</option></select></Field><p className="vm-form-note">{t('Nenhum download começa automaticamente. “Baixar se necessário” só será executado após você selecionar essa opção e iniciar a criação.')}</p></section>}
      <section className="panel vm-editor-section"><div className="vm-editor-section-heading"><span className="vm-editor-section-icon"><HardDrive size={18}/></span><div><h2>{t('Execução e energia')}</h2><p>{t('Desligar a VM não afetará o host por padrão.')}</p></div></div><div className="vm-editor-toggle-grid"><label className="vm-toggle"><input type="checkbox" checked={draft.auto_start} onChange={(e) => update('auto_start', e.target.checked)}/><span><strong>{t('Iniciar com o appliance')}</strong><small>{t('Somente uma VM pode iniciar automaticamente. Ao ativar esta, a anterior deixa de iniciar.')}</small></span></label><Field label={t('Política de energia')}><select disabled value="vm-only"><option value="vm-only">{t('Somente esta VM · política disponível')}</option></select><small className="vm-field-hint">{t('Modo kiosk ainda não é gerenciado pelo painel.')}</small></Field></div></section>
      {legacyPowerMode && <p className="vm-pending-note" role="alert">Esta VM usa a política antiga {original?.config.power_sync_mode}; salvar está bloqueado para não alterar silenciosamente esse comportamento.</p>}
      <div className="vm-editor-actions"><button type="button" className="button button-secondary" onClick={() => navigate(isEdit ? `/vms/${encodeURIComponent(draft.id)}/summary` : '/vms')}>{t('Cancelar')}</button><button type="submit" className="button button-primary" disabled={saving || saved || Boolean(job) || legacyPowerMode || (isEdit && !original)}><Save size={17}/>{saving ? t('Enviando…') : isEdit ? t('Salvar configuração') : t('Criar VM')}</button></div>
      {!isEdit && job && <section className="panel vm-provision-job" aria-live="polite"><div className="vm-provision-job-head"><span className="vm-editor-section-icon">{['queued', 'running', 'cancelling'].includes(job.state) ? <LoaderCircle className="spin" size={18}/> : job.state === 'succeeded' ? <Check size={18}/> : <AlertTriangle size={18}/>}</span><div><h2>{job.state === 'succeeded' ? t('Provisionamento concluído') : job.state === 'failed' ? t('Provisionamento falhou') : job.state === 'cancelled' ? t('Provisionamento cancelado') : job.state === 'interrupted' ? t('Tarefa interrompida') : t('Provisionando máquina virtual')}</h2><p>{t(job.stage || 'Aguardando início')} · {job.progress_percent == null ? t('Progresso indisponível') : `${Math.round(job.progress_percent)}%`}</p></div>{job.cancellable && <button type="button" className="button button-secondary" onClick={cancelProvisioning} disabled={cancelling}><X size={16}/>{cancelling ? t('Cancelando…') : t('Cancelar tarefa')}</button>}</div><ProgressBar value={job.progress_percent} label={t('Progresso do provisionamento')} className="vm-job-progress"/>{job.state === 'succeeded' && <p className="vm-form-note">{t('A VM foi criada e será aberta em instantes. Ela permanece desligada.')}</p>}{job.error?.message && <div className="inline-message error-message" role="alert">{t(job.error.message)}</div>}{jobError && <div className="inline-message error-message" role="alert">{t(jobError)}</div>}</section>}
      {isEdit && <section className="panel vm-editor-section vm-config-diff" aria-live="polite"><div className="vm-editor-section-heading"><span className="vm-editor-section-icon"><Layers3 size={18}/></span><div><h2>{t('Salvo e aplicado ao Reims')}</h2><p>{t('Diferenças entre o catálogo do painel e o último vm.toml preparado para o próximo boot.')}</p></div></div>{original?.projection_status === 'unknown' ? <p className="vm-field-hint">{t('Ainda não há uma projeção local confiável para comparar.')}</p> : original?.config_differences.length ? <div className="vm-config-diff-list">{original.config_differences.map((difference) => <div key={difference.field}><strong>{({ vcpus: t('vCPU'), cpu_model: t('Modelo de CPU'), ram_gb: t('Memória'), disk_path: t('Arquivo do disco'), disk_format: t('Formato do disco'), disk_port: t('Porta do disco'), additional_disks: t('Discos adicionais'), audio_enabled: t('Áudio'), audio_backend: t('Backend de áudio'), network_enabled: t('Rede'), network_mac: t('Endereço MAC'), network_ssh_port: t('Porta local para SSH'), display_resolution: t('Resolução'), display_count: t('Quantidade de displays'), display_hidpi: t('HiDPI'), fullscreen: t('Tela cheia'), display_refresh_hz: t('Taxa de atualização'), qemu_extra_args: t('Opções QEMU') } as Record<string, string>)[difference.field] ?? difference.field.replaceAll('_', ' ')}</strong><span>{t('Salvo: {value}').replace('{value}', difference.saved_value)}</span><span>{t('Última projeção: {value}').replace('{value}', difference.last_projection_value)}</span></div>)}</div> : <p className="vm-field-hint">{t('Os campos estruturados comparáveis coincidem com a última projeção.')}</p>}<p className="vm-form-note">{t('“Aplicado” aqui significa registrado no arquivo do Reims. Isso não confirma os argumentos efetivos de um QEMU que já esteja em execução; as mudanças persistentes entram no próximo boot.')}</p></section>}
      {isEdit && <p className="vm-form-note">{t('Operações iniciar/parar permanecem no painel da VM. Esta tela só altera configuração persistente; recursos sem suporte no runtime não são ativados.')}</p>}
    </form>
  </div>;
}
