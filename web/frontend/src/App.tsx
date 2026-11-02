import { lazy, Suspense, useEffect, useMemo, useRef, useState } from 'react';
import { Navigate, NavLink, Outlet, Route, Routes, useLocation, useNavigate, useOutletContext, useParams } from 'react-router-dom';
import {
  Activity, ArrowDownToLine, ArrowLeft, ArrowRight, ChevronDown, Clock3, RefreshCw, Copy, Check, CheckCircle2, ClipboardList, Globe2, Upload, Trash2, Eye, EyeOff, KeyRound,
  Cpu, Database, Gauge, HardDrive, Layers3, LayoutDashboard, Menu,
  LogOut, Monitor, MoreHorizontal, Network, Plus, Power, Settings2, ShieldCheck, Thermometer, Wifi, X,
} from 'lucide-react';
import { ApiError, api } from './api/client';
import type { AuditListResponse, CapabilitiesResponse, GpuInventoryResponse, HealthView, HostOverview, JobAccepted, JobListResponse, JobView, PasswordChangeRequest, PrivateTransferListResponse, ReimsImportCandidateList, ReimsReleaseList, SessionView, StorageInventory, TelemetryResponse, TelemetrySample, TelemetrySettingsView, VmBackupListResponse, VmConfigEnvelope, VmListResponse, VmRuntime, VmRuntimeList, VmSerialConsole, WebActivationView, WebActivationSubmitResponse } from './api/contracts';
import { usePolling } from './api/usePolling';
import { useEventStream } from './api/useEventStream';
import VmDeleteAction from './VmDeleteAction';
import HostAlerts from './HostAlerts';
import type { PollingResult } from './api/usePolling';
import TelemetryHistoryChart from './TelemetryHistoryChart';
import { Card, ConfirmationDialog, NavigationTabs, ProgressBar, ResponsiveList, StatusBadge, ToastProvider, useToast } from './components';
import { LanguageSelector, translate, useLocale, type Locale } from './i18n';

let appLocale: Locale = 'pt-BR';
const bytesToGiB = (value: number) => value / (1024 ** 3);
const VmEditor = lazy(() => import('./VmEditor'));
const formatGiB = (value: number) => `${value.toLocaleString(appLocale, { maximumFractionDigits: 2 })} GiB`;

const navItems = [
  { label: 'Visão geral', to: '/', icon: LayoutDashboard },
  { label: 'Máquinas virtuais', to: '/vms', icon: Layers3 },
  { label: 'Armazenamento', to: '/storage', icon: HardDrive },
  { label: 'Tarefas', to: '/tasks', icon: Activity },
];

function Brand() {
  return <a className="brand" href="/" aria-label={translate("vHackintosh Control, início")}>
    <span className="brand-mark"><span /></span>
    <span className="brand-copy"><strong>{translate("vHackintosh")}</strong><small>{translate("CONTROL PLANE")}</small></span>
  </a>;
}

function Shell({ username, onLogout }: { username: string; onLogout: () => Promise<void> }) {
  const [drawer, setDrawer] = useState(false);
  const [mobileLayout, setMobileLayout] = useState(() => window.matchMedia('(max-width: 767px)').matches);
  const drawerRef = useRef<HTMLElement>(null);
  const menuButtonRef = useRef<HTMLButtonElement>(null);
  const [logoutError, setLogoutError] = useState<string | null>(null);
  const location = useLocation();
  const host = usePolling<HostOverview>('/host', 8000);
  const activeJobs = usePolling<JobListResponse>('/jobs?limit=1&offset=0&state=queued&state=running&state=cancelling', 10000);
  const logout = async () => {
    setLogoutError(null);
    try {
      await api.request<SessionView>('/session/logout', { method: 'POST' });
      await onLogout();
    } catch (error) {
      setLogoutError(error instanceof Error ? translate(error.message) : translate("Não foi possível encerrar a sessão."));
    }
  };
  useEffect(() => {
    if (!drawer) return;
    const drawerElement = drawerRef.current;
    const firstControl = drawerElement?.querySelector<HTMLElement>('a[href], button:not([disabled])');
    firstControl?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        setDrawer(false);
        return;
      }
      if (event.key !== 'Tab' || !drawerElement) return;
      const controls = [...drawerElement.querySelectorAll<HTMLElement>('a[href], button:not([disabled]), [tabindex]:not([tabindex="-1"])')]
        .filter((element) => !element.hasAttribute('hidden') && element.getAttribute('aria-hidden') !== 'true');
      if (!controls.length) { event.preventDefault(); return; }
      const first = controls[0];
      const last = controls[controls.length - 1];
      if (event.shiftKey && (document.activeElement === first || !drawerElement.contains(document.activeElement))) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && (document.activeElement === last || !drawerElement.contains(document.activeElement))) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('keydown', onKeyDown);
      menuButtonRef.current?.focus();
    };
  }, [drawer]);
  useEffect(() => {
    const media = window.matchMedia('(min-width: 768px)');
    const closeOnDesktop = (event: MediaQueryListEvent) => { if (event.matches) setDrawer(false); };
    media.addEventListener('change', closeOnDesktop);
    return () => media.removeEventListener('change', closeOnDesktop);
  }, []);
  useEffect(() => {
    const media = window.matchMedia('(max-width: 767px)');
    const updateMobileLayout = (event: MediaQueryListEvent) => setMobileLayout(event.matches);
    media.addEventListener('change', updateMobileLayout);
    return () => media.removeEventListener('change', updateMobileLayout);
  }, []);
  useEffect(() => { setDrawer(false); }, [location.pathname]);
  const title = location.pathname === '/' ? translate("Visão geral")
    : location.pathname === '/host' ? translate("Telemetria do host")
      : location.pathname.startsWith('/vms/') ? translate("Máquina virtual")
      : navItems.find((item) => item.to === location.pathname)?.label ?? 'Gerenciamento';
  return <div className="app-shell">
    <a className="skip-link" href="#main-content">{translate("Pular para o conteúdo")}</a>
    {drawer && <button type="button" className="drawer-backdrop" aria-label={translate("Fechar navegação")} onClick={() => setDrawer(false)} />}
    <aside ref={drawerRef} id="primary-navigation-drawer" className={`sidebar ${drawer ? 'sidebar-open' : ''}`} aria-label={translate("Navegação principal")} aria-modal={drawer || undefined} role={drawer ? 'dialog' : undefined} aria-hidden={!drawer && mobileLayout} inert={!drawer && mobileLayout}>
      <div className="sidebar-top"><Brand /><button type="button" className="icon-button close-drawer" aria-label={translate("Fechar menu")} onClick={() => setDrawer(false)}><X /></button></div>
      <div className="nav-caption">{translate("APPLIANCE")}</div>
      <nav className="primary-nav">
        {navItems.map(({ label, to, icon: Icon }) => <NavLink key={to} to={to} end={to === '/'} onClick={() => setDrawer(false)} className={({ isActive }) => `nav-link ${isActive ? 'active' : ''}`}>
          <Icon aria-hidden="true" /><span>{label}</span>{label === 'Tarefas' && <span className="nav-count" aria-label={`${activeJobs.data?.total ?? 0} tarefas ativas`}>{activeJobs.data?.total ?? '—'}</span>}
        </NavLink>)}
      </nav>
      <div className="nav-caption nav-caption-spaced">{translate("SISTEMA")}</div>
      <nav className="primary-nav">
        <NavLink to="/host" onClick={() => setDrawer(false)} className={({ isActive }) => `nav-link ${isActive ? 'active' : ''}`}><Gauge aria-hidden="true"/><span>{translate("Host")}</span></NavLink>
        <NavLink to="/gpu" onClick={() => setDrawer(false)} className={({ isActive }) => `nav-link ${isActive ? 'active' : ''}`}><Monitor aria-hidden="true"/><span>{translate("GPU")}</span></NavLink>
        <NavLink to="/settings" onClick={() => setDrawer(false)} className={({ isActive }) => `nav-link ${isActive ? 'active' : ''}`}><Settings2 aria-hidden="true"/><span>{translate("Configurações")}</span></NavLink>
      </nav>
      <div className="sidebar-bottom">
        <div className="connection-card"><span className={`status-dot ${host.data ? host.error ? 'status-stale' : 'status-live' : 'status-unknown'}`}/><div><strong>{translate("Conexão com host")}</strong><small>{host.data ? host.error ? translate("Dados anteriores") : host.data.hostname : translate("Indisponível")}</small></div><MoreHorizontal size={17}/></div>
        <div className="profile-row"><div className="profile-button"><span className="avatar">{translate("VH")}</span><span className="profile-copy"><strong>{username}</strong><small>{translate("Sessão autenticada")}</small></span><ChevronDown size={16}/></div><button className="icon-button logout-button" onClick={logout} aria-label={translate("Encerrar sessão")} title={translate("Encerrar sessão")}><LogOut size={17}/></button></div>{logoutError && <p className="logout-error" role="alert">{logoutError}</p>}
      </div>
    </aside>
    <section className="main-column" aria-hidden={drawer || undefined} inert={drawer}>
      <header className="topbar">
        <button ref={menuButtonRef} type="button" className="icon-button menu-button" aria-label={translate("Abrir menu")} aria-haspopup="dialog" aria-expanded={drawer} aria-controls="primary-navigation-drawer" onClick={() => setDrawer(true)}><Menu /></button>
        <div className="breadcrumbs"><span>{translate("Appliance")}</span><span className="crumb-separator">/</span><strong>{title}</strong></div>
        <div className="topbar-actions"><div className="host-pill"><span className={`status-dot ${host.data ? host.error ? 'status-stale' : 'status-live' : 'status-unknown'}`}/>{host.data ? host.error ? translate("Host com dados antigos") : host.data.hostname : host.loading ? translate("Consultando host") : translate("Host indisponível")}</div><LanguageSelector/><span className="top-avatar" aria-label={`Perfil ${username}`}>{translate("VH")}</span></div>
      </header>
      <main id="main-content" tabIndex={-1} className="page-content"><Outlet context={{ username, host }} /></main>
      <footer className="app-footer"><span>{translate("vHackintosh Control")}</span><span>{translate("Gerenciamento local do appliance")}</span></footer>
    </section>
    <nav className="mobile-nav" aria-label={translate("Navegação rápida")} aria-hidden={drawer || undefined} inert={drawer}>{navItems.slice(0, 4).map(({ label, to, icon: Icon }) => <NavLink key={to} to={to} end={to === '/'} aria-label={label} className={({ isActive }) => `mobile-nav-link ${isActive ? 'active' : ''}`}><Icon aria-hidden="true"/><span>{label === 'Máquinas virtuais' ? 'VMs' : label === 'Visão geral' ? translate("Início") : label}</span></NavLink>)}</nav>
  </div>;
}

function PageHeading({ eyebrow, title, description, actions }: { eyebrow: string; title: string; description: string; actions?: React.ReactNode }) {
  return <div className="page-heading"><div><div className="eyebrow"><span className="eyebrow-line"/>{translate(eyebrow)}</div><h1>{translate(title)}</h1><p>{translate(description)}</p></div>{actions && <div className="heading-actions">{actions}</div>}</div>;
}

function EmptyState({ icon: Icon, title, body, action }: { icon: typeof Layers3; title: string; body: string; action?: React.ReactNode }) {
  return <div className="empty-state"><div className="empty-icon"><Icon size={23}/></div><h3>{title}</h3><p>{body}</p>{action}</div>;
}

type HostPolling = PollingResult<HostOverview>;
interface ShellContext { username: string; host: HostPolling }

function formatBytes(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return translate("Indisponível");
  const units = ['B', 'KiB', 'MiB', translate("GiB"), 'TiB'];
  let amount = value;
  let unit = 0;
  while (amount >= 1024 && unit < units.length - 1) { amount /= 1024; unit += 1; }
  return `${amount.toLocaleString(appLocale, { maximumFractionDigits: amount >= 100 ? 0 : 1 })} ${units[unit]}`;
}

function sampleFor(samples: TelemetrySample[] | undefined, name: string): TelemetrySample | undefined {
  return samples?.filter((sample) => sample.name === name).sort((a, b) => Date.parse(b.timestamp) - Date.parse(a.timestamp))[0];
}

function formatSample(sample: TelemetrySample | undefined, display?: (value: number) => string): string {
  if (!sample || sample.availability !== 'available' || sample.value == null) return translate("Indisponível");
  if (typeof sample.value === 'number') return display ? display(sample.value) : `${sample.value.toLocaleString(appLocale, { maximumFractionDigits: 1 })} ${sample.unit}`;
  return `${sample.value} ${sample.unit}`.trim();
}

function DataState({ data, error, loading, updatedAt, staleAfter = 15000 }: { data: unknown; error: Error | null; loading: boolean; updatedAt: number | null; staleAfter?: number }) {
  const age = updatedAt == null ? null : Date.now() - updatedAt;
  const stale = Boolean(data) && (Boolean(error) || (age != null && age > staleAfter));
  if (!data) return <div className={`data-state ${error ? 'data-error' : ''}`} role="status"><span className={`status-dot ${error ? 'status-down' : 'status-unknown'}`}/>{error ? translate(error.message) : loading ? translate("Consultando serviço…") : translate("Sem dados disponíveis")}</div>;
  if (stale) return <div className="data-state data-stale" role="status"><span className="status-dot status-stale"/>{translate("Mostrando última resposta")}{' '}{age == null ? '' : `${translate('· atualizada há')} ${Math.max(1, Math.floor(age / 1000))} s`}{error ? ` · ${translate(error.message)}` : ''}</div>;
  return null;
}

function formatTimestamp(value: string | null | undefined): string {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString(appLocale, { dateStyle: 'short', timeStyle: 'medium' });
}

function MetricCard({ label, icon: Icon, value, helper }: { label: string; icon: typeof Cpu; value: string; helper: string }) {
  return <Card as="article" className="metric-card"><div className="metric-head"><span>{translate(label)}</span><Icon size={18}/></div><strong className="metric-value metric-value-data">{value}</strong><span className="metric-foot">{translate(helper)}</span></Card>;
}

const runtimeLabels: Record<VmRuntime['state'], string> = { stopped: 'Parada', starting: 'Iniciando', running: 'Em execução', stopping: 'Parando', degraded: 'Degradada', unknown: 'Desconhecido' };

function VmCard({ config, runtime }: { config: VmListResponse['items'][number]['config']; runtime?: VmRuntime }) {
  return <NavLink className="vm-row" to={`/vms/${encodeURIComponent(config.id)}/summary`}><span className="vm-row-icon"><Monitor size={18}/></span><span className="vm-row-main"><strong>{config.name}</strong><small>{config.macos_version} · {config.vcpus}{' '}{translate("vCPU ·")}{' '}{config.ram_gb}{' '}{translate("GiB RAM")}</small></span><span className={`vm-state state-${runtime?.state ?? 'unknown'}`}>{runtime ? translate(runtimeLabels[runtime.state]) : translate("Estado indisponível")}</span><ArrowRight size={16}/></NavLink>;
}

function WebActivationPanel() {
  const activation = usePolling<WebActivationView>('/activation', 30000);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [copied, setCopied] = useState(false);
  const statusSyncInFlight = useRef(false);
  const status = activation.data?.status ?? 'not_submitted';

  useEffect(() => {
    if (status !== 'pending') return;
    let cancelled = false;
    const syncStatus = async () => {
      if (cancelled || document.visibilityState !== 'visible' || statusSyncInFlight.current) return;
      statusSyncInFlight.current = true;
      try {
        await api.request<WebActivationSubmitResponse>('/activation/sync', { method: 'POST', body: '{}' });
        if (!cancelled) await activation.refresh();
      } catch {
        // Keep the pending status visible and retry on the next interval.
      } finally {
        statusSyncInFlight.current = false;
      }
    };
    void syncStatus();
    const timer = window.setInterval(() => void syncStatus(), 10_000);
    const onVisibilityChange = () => { if (document.visibilityState === 'visible') void syncStatus(); };
    document.addEventListener('visibilitychange', onVisibilityChange);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
      document.removeEventListener('visibilitychange', onVisibilityChange);
    };
  }, [status, activation.refresh]);

  const sendRequest = async (sync = false) => {
    if (sync && statusSyncInFlight.current) return;
    if (sync) statusSyncInFlight.current = true;
    setBusy(true);
    setError('');
    try {
      await api.request<WebActivationSubmitResponse>(sync ? '/activation/sync' : '/activation/submit', { method: 'POST', body: '{}' });
      await activation.refresh();
    } catch (reason) {
      setError(reason instanceof Error ? translate(reason.message) : translate('Não foi possível enviar o pedido de ativação.'));
    } finally {
      if (sync) statusSyncInFlight.current = false;
      setBusy(false);
    }
  };
  const copyCode = async () => {
    if (!activation.data) return;
    try {
      await navigator.clipboard.writeText(activation.data.activation_code);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1800);
    } catch {
      setError(translate('Não foi possível copiar. Verifique a permissão do navegador.'));
    }
  };
  const statusLabel = status === 'approved' ? translate('Ativado')
    : status === 'pending' ? translate('Aguardando análise')
      : status === 'rejected' ? translate('Não aprovado') : translate('Não solicitado');
  return <section className="panel web-activation-panel" aria-labelledby="web-activation-title">
    <div className="panel-heading"><div><span className="section-kicker">{translate('LICENÇA DO APPLIANCE')}</span><h2 id="web-activation-title">{translate('Ativação do vHackintosh')}</h2></div><ShieldCheck size={19}/></div>
    {status !== 'approved' && <p>{translate('Entre no painel primeiro. Este código identifica o appliance; ao solicitar, serão enviados apenas dados básicos de hardware para análise no site.')}</p>}
    {activation.error && <p className="inline-message error-message" role="alert">{translate(activation.error.message)}</p>}
    {error && <p className="inline-message error-message" role="alert">{error}</p>}
    {activation.data && <>
      {status === 'approved' ? <>
        <p className="web-activation-approved"><CheckCircle2 size={17}/>{translate('Este appliance está ativado.')}</p>
        <details className="web-activation-details">
          <summary>{translate('Ver detalhes da ativação')}</summary>
          <div className="web-activation-code"><code>{activation.data.activation_code}</code><button type="button" className="button button-secondary" onClick={() => void copyCode()} aria-label={translate('Copiar código de ativação')}>{copied ? <Check size={16}/> : <Copy size={16}/>}<span>{copied ? translate('Copiado') : translate('Copiar código')}</span></button></div>
          <div className="web-activation-status"><span className="vm-state state-running">{statusLabel}</span><small>{translate('Atualizado em')} {formatTimestamp(activation.data.updated_at)}</small></div>
          <p className="web-activation-privacy">{translate('A ativação é opcional. O inventário inclui modelo do CPU, memória total, modelos das GPUs e repositório/release do Reims vGPU; o pedido também usa o código e um identificador técnico para associar o appliance. Não enviamos telemetria de uso; o inventário não contém MAC, números de série ou UUIDs.')}</p>
          {activation.data.inventory_changed && <p className="inline-message">{translate('O hardware do host mudou desde o último envio. Atualize o inventário para refletir CPU, memória, GPUs e Reims atuais.')}</p>}
          <a className="text-link" href={activation.data.portal_url} target="_blank" rel="noreferrer">{translate('Abrir site vHackintosh')}<ArrowRight size={15}/></a>
        </details>
      </> : <>
        <div className="web-activation-code"><code>{activation.data.activation_code}</code><button type="button" className="button button-secondary" onClick={() => void copyCode()} aria-label={translate('Copiar código de ativação')}>{copied ? <Check size={16}/> : <Copy size={16}/>}<span>{copied ? translate('Copiado') : translate('Copiar código')}</span></button></div>
        <div className="web-activation-status"><span className={`vm-state state-${status === 'rejected' ? 'degraded' : 'unknown'}`}>{statusLabel}</span><small>{translate('Atualizado em')} {formatTimestamp(activation.data.updated_at)}</small></div>
        {status === 'pending' && <p className="activation-auto-sync" role="status">{translate('Verificando aprovação automaticamente a cada 10 segundos enquanto este painel estiver aberto.')}</p>}
        <p className="web-activation-privacy">{translate('A ativação é opcional. O inventário inclui modelo do CPU, memória total, modelos das GPUs e repositório/release do Reims vGPU; o pedido também usa o código e um identificador técnico para associar o appliance. Não enviamos telemetria de uso; o inventário não contém MAC, números de série ou UUIDs.')}</p>
        {activation.data.inventory_changed && <p className="inline-message">{translate('O hardware do host mudou desde o último envio. Atualize o inventário para refletir CPU, memória, GPUs e Reims atuais.')}</p>}
        <div className="button-row">
          {(status === 'not_submitted' || status === 'rejected') && <button className="button button-primary" type="button" disabled={busy || !activation.data} onClick={() => void sendRequest()}>{busy ? translate('Enviando…') : translate('Solicitar ativação')}</button>}
          {activation.data.inventory_changed && status !== 'not_submitted' && <button className="button button-secondary" type="button" disabled={busy} onClick={() => void sendRequest()}>{busy ? translate('Enviando…') : translate('Atualizar inventário')}</button>}
          {status === 'pending' && <button className="button button-secondary" type="button" disabled={busy} onClick={() => void sendRequest(true)}>{busy ? translate('Consultando…') : <><RefreshCw size={15}/>{translate('Atualizar situação')}</>}</button>}
          <a className="text-link" href={activation.data.portal_url} target="_blank" rel="noreferrer">{translate('Abrir site vHackintosh')}<ArrowRight size={15}/></a>
        </div>
      </>}
    </>}
    {!activation.data && !activation.error && <p role="status">{translate('Carregando código de ativação…')}</p>}
  </section>;
}

function Overview() {
  const { host } = useOutletContext<ShellContext>();
  const metrics = useEventStream<TelemetryResponse>('/host/metrics/stream');
  const vms = usePolling<VmListResponse>('/vms', 8000);
  const vmRuntimes = usePolling<VmRuntimeList>('/vms/runtime', 8000);
  const jobs = usePolling<JobListResponse>('/jobs?limit=5&offset=0', 5000);
  const gpuInventory = usePolling<GpuInventoryResponse>('/host/gpus', 15000);
  const samples = metrics.data?.samples;
  const cpu = sampleFor(samples, 'host.cpu_percent');
  const memoryTotal = sampleFor(samples, 'host.memory_total_bytes');
  const memoryUsed = sampleFor(samples, 'host.memory_used_bytes');
  const diskRead = sampleFor(samples, 'host.disk_read_bytes_per_second');
  const diskWrite = sampleFor(samples, 'host.disk_write_bytes_per_second');
  const netRx = samples?.filter((sample) => sample.name === 'host.network_rx_bytes_per_second');
  const netTx = samples?.filter((sample) => sample.name === 'host.network_tx_bytes_per_second');
  const sumValues = (entries: TelemetrySample[] | undefined) => entries?.length && entries.every((sample) => sample.value != null && sample.availability === 'available')
    ? entries.reduce((sum, sample) => sum + Number(sample.value), 0) : undefined;
  const rateSample = (entries: TelemetrySample[] | undefined): TelemetrySample | undefined => {
    const value = sumValues(entries);
    const first = entries?.[0];
    return first && value != null ? { ...first, value, availability: 'available' } : undefined;
  };
  const vmItems = vms.data?.items ?? [];
  return <>
    <PageHeading eyebrow="CENTRO DE OPERAÇÕES" title={translate("Visão geral")} description="Acompanhe o appliance e gerencie suas máquinas virtuais." actions={<NavLink className="button button-primary" to="/vms/new"><Plus size={17}/>{translate("Criar máquina virtual")}</NavLink>} />
    <WebActivationPanel />
    <DataState data={host.data} error={host.error} loading={host.loading} updatedAt={host.updatedAt}/>
    <DataState data={metrics.data} error={metrics.error} loading={metrics.loading} updatedAt={metrics.updatedAt}/>
    <section className="metric-grid" aria-label={translate("Telemetria do host")}>
      <MetricCard label="CPU" icon={Cpu} value={formatSample(cpu)} helper={cpu ? `${cpu.source} · ${formatTimestamp(cpu.timestamp)}` : translate("Sem amostra disponível")} />
      <MetricCard label="Memória usada" icon={Layers3} value={memoryUsed ? formatBytes(Number(memoryUsed.value)) : translate("Indisponível")} helper={memoryTotal ? `de ${formatBytes(Number(memoryTotal.value))} · fonte: ${memoryUsed?.source ?? memoryTotal.source}` : translate("Sem amostra disponível")} />
      <MetricCard label="Disco · leitura / escrita" icon={Database} value={`${formatSample(diskRead)} / ${formatSample(diskWrite)}`} helper="Taxas reportadas pelo coletor do host" />
      <MetricCard label="Rede · recebida / enviada" icon={Network} value={`${formatSample(rateSample(netRx), (n) => `${formatBytes(n)}/s`)} / ${formatSample(rateSample(netTx), (n) => `${formatBytes(n)}/s`)}`} helper="Soma das interfaces disponíveis" />
    </section>
    <section className="panel overview-gpu-vram-panel" aria-labelledby="overview-gpu-vram-heading">
      <div className="panel-heading"><div><span className="section-kicker">GPU</span><h2 id="overview-gpu-vram-heading">{translate("VRAM por placa")}</h2></div><NavLink className="text-link" to="/gpu">{translate("Ver inventário de GPU")}<ArrowRight size={15}/></NavLink></div>
      {gpuInventory.data?.items.length ? <ul className="overview-gpu-vram-list">{gpuInventory.data.items.map((device) => {
        const used = samples?.find((sample) => sample.name === 'host.gpu.vram_used_bytes' && sample.labels.pci_bdf === device.pci_bdf);
        const total = samples?.find((sample) => sample.name === 'host.gpu.vram_total_bytes' && sample.labels.pci_bdf === device.pci_bdf);
        const usedBytes = used?.availability === 'available' && used.value != null && Number.isFinite(Number(used.value)) ? Number(used.value) : null;
        const totalBytes = total?.availability === 'available' && total.value != null && Number.isFinite(Number(total.value)) ? Number(total.value) : null;
        const percent = usedBytes != null && totalBytes != null && totalBytes > 0 ? Math.min(100, Math.max(0, usedBytes / totalBytes * 100)) : null;
        const usageLabel = usedBytes == null ? translate("Indisponível") : usedBytes <= 0 ? translate("Sem uso") : `${formatBytes(usedBytes)}${totalBytes == null ? '' : ` / ${formatBytes(totalBytes)}`}`;
        return <li className="overview-gpu-vram-row" key={device.pci_bdf}>
          <div className="overview-gpu-vram-label"><strong>{device.name}</strong><span>{usageLabel}</span></div>
          <div className="overview-gpu-vram-track" role={percent == null ? 'img' : 'progressbar'} aria-label={`${device.name}: ${percent == null ? usageLabel : `${percent.toLocaleString(appLocale, { maximumFractionDigits: 1 })}%`}`} aria-valuemin={percent == null ? undefined : 0} aria-valuemax={percent == null ? undefined : 100} aria-valuenow={percent == null ? undefined : percent}><span style={{ width: `${percent ?? 0}%` }}/></div>
        </li>;
      })}</ul> : gpuInventory.data ? <p className="overview-gpu-vram-empty">{translate("O inventário não encontrou placas gráficas físicas.")}</p> : <p className="overview-gpu-vram-empty" role={gpuInventory.error ? 'alert' : 'status'}>{translate(gpuInventory.error?.message ?? "Aguardando dados do inventário de placas gráficas.")}</p>}
    </section>
    <section className="telemetry-history-grid" aria-label={translate("Histórico de telemetria do host")}>
      <TelemetryHistoryChart title={translate("Uso do processador")} valueLabel="CPU" unit="%" color="#22d3ee" currentValue={formatSample(cpu)} currentSource={cpu?.source ?? ''} currentUpdatedAt={cpu ? formatTimestamp(cpu.timestamp) : translate("sem amostra")} historyPath="/host/metrics/history" metricName="host.cpu_percent" min={0} max={100}/>
      <TelemetryHistoryChart title={translate("Memória usada")} valueLabel="Memória" unit="GiB" color="#60a5fa" currentValue={memoryUsed ? formatBytes(Number(memoryUsed.value)) : translate("Indisponível")} currentSource={memoryUsed?.source ?? ''} currentUpdatedAt={memoryUsed ? formatTimestamp(memoryUsed.timestamp) : translate("sem amostra")} historyPath="/host/metrics/history" metricName="host.memory_used_bytes" transform={bytesToGiB} formatAxis={formatGiB}/>
    </section>
    <section className="panel host-overview-panel"><div className="panel-heading"><div><span className="section-kicker">{translate("HOST")}</span><h2>{host.data?.hostname ?? 'Informações do host indisponíveis'}</h2></div><Gauge size={18}/></div><div className="host-facts"><span><small>{translate("Processador")}</small><strong>{host.data?.cpu_model ?? 'Indisponível'}</strong></span><span><small>{translate("Arquitetura")}</small><strong>{host.data?.architecture ?? '—'}</strong></span><span><small>{translate("Núcleos lógicos")}</small><strong>{host.data?.logical_cpus ?? '—'}</strong></span><span><small>{translate("Memória total")}</small><strong>{host.data ? formatBytes(host.data.memory_total_bytes) : '—'}</strong></span><span><small>{translate("Endereços")}</small><strong>{host.data?.addresses.length ? host.data.addresses.join(', ') : translate("Indisponível")}</strong></span></div></section>
    <div className="content-grid">
      <Card className="vm-panel"><div className="panel-heading"><div><span className="section-kicker">{translate("INVENTÁRIO")}</span><h2>{translate("Máquinas virtuais")}</h2></div><NavLink className="text-link" to="/vms">{translate("Ver inventário")}{' '}<ArrowRight size={15}/></NavLink></div><DataState data={vms.data} error={vms.error} loading={vms.loading} updatedAt={vms.updatedAt}/><DataState data={vmRuntimes.data} error={vmRuntimes.error} loading={vmRuntimes.loading} updatedAt={vmRuntimes.updatedAt}/>{vmItems.length ? <ResponsiveList className="compact-vm-list" label={translate("Máquinas virtuais recentes")}>{vmItems.slice(0, 4).map(({ config }) => <li key={config.id}><VmCard config={config} runtime={vmRuntimes.data?.items.find((item) => item.id === config.id)}/></li>)}</ResponsiveList> : vms.data ? <EmptyState icon={Layers3} title={translate("Nenhuma VM cadastrada")} body={translate("O catálogo respondeu sem máquinas virtuais.")}/> : <EmptyState icon={Layers3} title={translate("Inventário indisponível")} body={translate("As VMs aparecerão quando o endpoint de catálogo responder.")}/>}</Card>
      <section className="panel tasks-panel"><div className="panel-heading"><div><span className="section-kicker">{translate("ATIVIDADE")}</span><h2>{translate("Tarefas recentes")}</h2></div><NavLink className="text-link" to="/tasks">{translate("Ver tarefas")}{' '}<ArrowRight size={15}/></NavLink></div><DataState data={jobs.data} error={jobs.error} loading={jobs.loading} updatedAt={jobs.updatedAt} staleAfter={12000}/>{jobs.data?.items.length ? <div className="task-list overview-task-list">{jobs.data.items.map((task) => <NavLink key={task.id} className="task-row" to="/tasks"><span className={`task-icon task-state-${task.state}`}><Activity size={16}/></span><span className="task-row-main"><strong>{jobKindLabel(task.kind)}</strong><small>{task.target_vm_id ? `VM ${task.target_vm_id}` : `${translate('Tarefa')} ${task.id}`} · {formatTimestamp(task.updated_at)}</small>{<ProgressBar value={task.progress_percent} label={`${translate('Progresso da tarefa')} ${task.id}`} className="task-row-progress"/>}</span><StatusBadge className={`vm-state task-state-${task.state}`}>{translate(taskStateLabels[task.state])}</StatusBadge></NavLink>)}</div> : <EmptyState icon={Activity} title={jobs.data ? translate("Nenhuma tarefa registrada") : jobs.error ? translate("Tarefas indisponíveis") : translate("Aguardando tarefas")} body={jobs.error?.message ? translate(jobs.error.message) : jobs.data ? translate("As operações recentes do appliance aparecerão aqui.") : translate("A atividade será exibida quando o agente responder.")}/>}</section>
    </div>
    <div className="bottom-grid"><section className="panel host-panel"><div className="panel-heading"><div><span className="section-kicker">{translate("TELEMETRIA")}</span><h2>{translate("Amostragem")}</h2></div><Gauge size={18}/></div><div className="host-status"><span className="status-ring"><Activity size={19}/></span><div><strong>{metrics.updatedAt ? `${translate('Última resposta')} ${formatTimestamp(new Date(metrics.updatedAt).toISOString())}` : translate("Sem resposta do coletor")}</strong><p>{translate("As amostras têm timestamp e fonte individuais.")}</p></div></div></section><section className="panel quick-panel"><div className="panel-heading"><div><span className="section-kicker">{translate("ACESSO RÁPIDO")}</span><h2>{translate("Recursos")}</h2></div></div><div className="quick-links"><NavLink to="/gpu"><Monitor/><span>{translate("Ver inventário de GPU")}</span><ArrowRight/></NavLink><NavLink to="/storage"><HardDrive/><span>{translate("Armazenamento")}</span><ArrowRight/></NavLink></div></section></div>
  </>;
}

function HostPage() {
  const { host } = useOutletContext<ShellContext>();
  const metrics = useEventStream<TelemetryResponse>('/host/metrics/stream');
  const gpus = usePolling<GpuInventoryResponse>('/host/gpus', 30000);
  const samples = metrics.data?.samples ?? [];
  const cpu = sampleFor(samples, 'host.cpu_percent');
  const cpuCores = samples
    .filter((sample) => sample.name === 'host.cpu_core_percent')
    .sort((left, right) => Number(left.labels.core) - Number(right.labels.core));
  const memoryUsed = sampleFor(samples, 'host.memory_used_bytes');
  const memoryTotal = sampleFor(samples, 'host.memory_total_bytes');
  const memoryAvailable = sampleFor(samples, 'host.memory_available_bytes');
  const swapTotal = sampleFor(samples, 'host.swap_total_bytes');
  const swapFree = sampleFor(samples, 'host.swap_free_bytes');
  const swapUsed = swapTotal && swapFree && typeof swapTotal.value === 'number' && typeof swapFree.value === 'number'
    ? swapTotal.value - swapFree.value : undefined;
  const diskRead = samples.filter((sample) => sample.name === 'host.disk_read_bytes_per_second');
  const diskWrite = samples.filter((sample) => sample.name === 'host.disk_write_bytes_per_second');
  const sumRates = (entries: TelemetrySample[]) => entries.length > 0 && entries.every((sample) => sample.availability === 'available' && typeof sample.value === 'number')
    ? { ...entries[0], value: entries.reduce((sum, sample) => sum + Number(sample.value), 0), availability: 'available' as const }
    : undefined;
  const diskReadTotal = sumRates(diskRead);
  const diskWriteTotal = sumRates(diskWrite);
  const temperatures = samples.filter((sample) => sample.name === 'host.temperature_celsius');
  const availableTemperatures = [...new Map(temperatures
    .filter((sample) => sample.availability === 'available')
    .map((sample) => {
      // hwmonN is an enumeration slot, not a stable sensor identity. Ignore it
      // here so duplicate kernel aliases do not create a second card or make
      // the list jump when Linux enumerates hwmon devices in another order.
      const identity = [sample.source, sample.labels.pci_bdf, sample.labels.chip, sample.labels.sensor]
        .filter(Boolean).join(':') || `${sample.source}:${JSON.stringify(Object.entries(sample.labels).sort(([a], [b]) => a.localeCompare(b)))}`;
      return [identity, sample] as const;
    })).entries()].sort(([left], [right]) => left.localeCompare(right, 'pt-BR'));
  const gpuTelemetry = samples.filter((sample) => sample.name.startsWith('host.gpu.'));
  const gpuBdfs = [...new Set((gpus.data?.items ?? []).map((device) => device.pci_bdf))];
  const gpuSample = (bdf: string, metric: string) => gpuTelemetry.find((sample) => sample.labels.pci_bdf === bdf && sample.name === metric);
  const gpuLabel = (bdf: string) => gpus.data?.items.find((device) => device.pci_bdf === bdf)?.name ?? bdf;
  const interfaces = [...new Set(samples.filter((sample) => sample.name.startsWith('host.network_')).map((sample) => sample.labels.interface).filter((name): name is string => Boolean(name)))];
  const networkValue = (name: string, iface: string) => formatSample(samples.find((sample) => sample.name === name && sample.labels.interface === iface), (value) => `${formatBytes(value)}/s`);
  const load = ['host.load_1m', 'host.load_5m', 'host.load_15m'].map((name) => sampleFor(samples, name));
  const filesystemMounts = [...new Set(samples.filter((sample) => sample.name === 'host.filesystem_total_bytes').map((sample) => sample.labels.mount).filter((mount): mount is string => Boolean(mount)))].sort();
  const filesystemCapacity = (mount: string, metric: string) => samples.find((sample) => sample.name === metric && sample.labels.mount === mount);
  const unavailableSensors = !temperatures.some((sample) => sample.availability === 'available');
  return <>
    <PageHeading eyebrow="HOST · TELEMETRIA AO VIVO" title={translate("Telemetria do host")} description="Uso de recursos, interfaces de rede e sensores reportados pelo appliance." actions={<span className="live-badge"><span className="status-dot status-live"/>{translate("Amostras a cada 1 s")}</span>}/>
    <DataState data={host.data} error={host.error} loading={host.loading} updatedAt={host.updatedAt}/>
    <DataState data={metrics.data} error={metrics.error} loading={metrics.loading} updatedAt={metrics.updatedAt}/>
    <section className="metric-grid" aria-label={translate("Uso atual do host")}>
      <MetricCard label="Processador" icon={Cpu} value={formatSample(cpu)} helper={cpu ? `${cpu.source} · ${formatTimestamp(cpu.timestamp)}` : translate("Sem amostra disponível")}/>
      <MetricCard label="Memória usada" icon={Layers3} value={memoryUsed ? formatBytes(Number(memoryUsed.value)) : translate("Indisponível")} helper={memoryTotal ? `de ${formatBytes(Number(memoryTotal.value))}` : translate("Sem amostra disponível")}/>
      <MetricCard label="Memória disponível" icon={Layers3} value={formatSample(memoryAvailable, formatBytes)} helper="Estimativa reportada pelo kernel Linux"/>
      <MetricCard label="Swap usada" icon={Layers3} value={swapUsed == null ? translate("Indisponível") : formatBytes(Number(swapUsed))} helper={swapTotal ? `de ${formatBytes(Number(swapTotal.value))}` : translate("Swap não configurada ou indisponível")}/>
      <MetricCard label="Disco · leitura / escrita" icon={Database} value={`${formatSample(diskReadTotal, (value) => `${formatBytes(value)}/s`)} / ${formatSample(diskWriteTotal, (value) => `${formatBytes(value)}/s`)}`} helper={`Soma de ${diskRead.length} dispositivo(s) · taxas do intervalo`}/>
      <MetricCard label="Sensores térmicos" icon={Thermometer} value={unavailableSensors ? translate("Indisponíveis") : `${temperatures.filter((sample) => sample.availability === 'available').length} sensores`} helper={unavailableSensors ? translate("O host não reportou sensores hwmon") : translate("Leitura fornecida pelo sysfs do host")}/>
    </section>
    <HostAlerts />
    <section className="panel host-overview-panel host-core-panel" aria-label={translate("Uso por processador lógico")}>
      <div className="panel-heading"><div><span className="section-kicker">{translate("PROCESSADOR")}</span><h2>{translate("Uso por núcleo lógico")}</h2></div><Cpu size={18}/></div>
      {cpuCores.length ? <div className="host-core-grid">{cpuCores.map((sample) => {
        const value = sample.availability === 'available' && typeof sample.value === 'number' ? sample.value : null;
        return <div className="host-core-meter" key={sample.labels.core ?? sample.timestamp}>
          <div className="host-core-meter-label"><span>{translate("CPU")}{' '}{sample.labels.core ?? '—'}</span><strong>{value == null ? 'Aguardando amostra' : `${value.toLocaleString(appLocale, { maximumFractionDigits: 1 })}%`}</strong></div>
          <div className="host-core-track" role="meter" aria-label={`Uso da CPU ${sample.labels.core ?? ''}`} aria-valuemin={0} aria-valuemax={100} aria-valuenow={value ?? undefined} aria-valuetext={value == null ? translate("Amostra indisponível") : `${value.toLocaleString(appLocale, { maximumFractionDigits: 1 })}%`}><span style={{ width: `${value ?? 0}%` }}/></div>
        </div>;
      })}</div> : <p className="inline-message">{translate("O coletor ainda não forneceu amostras por núcleo lógico.")}</p>}
    </section>
    <section className="telemetry-history-grid" aria-label={translate("Histórico recente do host")}>
      <TelemetryHistoryChart title={translate("Uso do processador")} valueLabel="CPU" unit="%" color="#22d3ee" currentValue={formatSample(cpu)} currentSource={cpu?.source ?? ''} currentUpdatedAt={cpu ? formatTimestamp(cpu.timestamp) : translate("sem amostra")} historyPath="/host/metrics/history" metricName="host.cpu_percent" min={0} max={100}/>
      <TelemetryHistoryChart title={translate("Memória usada")} valueLabel="Memória" unit="GiB" color="#60a5fa" currentValue={memoryUsed ? formatBytes(Number(memoryUsed.value)) : translate("Indisponível")} currentSource={memoryUsed?.source ?? ''} currentUpdatedAt={memoryUsed ? formatTimestamp(memoryUsed.timestamp) : translate("sem amostra")} historyPath="/host/metrics/history" metricName="host.memory_used_bytes" transform={bytesToGiB} formatAxis={formatGiB}/>
    </section>
    <div className="host-detail-grid">
      <section className="panel host-overview-panel"><div className="panel-heading"><div><span className="section-kicker">{translate("IDENTIFICAÇÃO")}</span><h2>{host.data?.hostname ?? 'Informações indisponíveis'}</h2></div><Gauge size={18}/></div><div className="host-facts"><span><small>{translate("Processador")}</small><strong>{host.data?.cpu_model ?? 'Indisponível'}</strong></span><span><small>{translate("Arquitetura")}</small><strong>{host.data?.architecture ?? '—'}</strong></span><span><small>{translate("Núcleos lógicos")}</small><strong>{host.data?.logical_cpus ?? '—'}</strong></span><span><small>{translate("Memória total")}</small><strong>{host.data ? formatBytes(host.data.memory_total_bytes) : '—'}</strong></span><span><small>{translate("Tempo ligado")}</small><strong>{host.data ? `${Math.floor(host.data.uptime_seconds / 3600)} h` : '—'}</strong></span></div></section>
      <section className="panel host-overview-panel"><div className="panel-heading"><div><span className="section-kicker">{translate("CARGA MÉDIA")}</span><h2>{translate("Últimos intervalos")}</h2></div><Activity size={18}/></div><div className="host-facts">{load.map((sample, index) => <span key={index}><small>{[translate("1 minuto"), translate("5 minutos"), translate("15 minutos")][index]}</small><strong>{formatSample(sample)}</strong></span>)}</div></section>
    </div>
    <section className="panel host-overview-panel"><div className="panel-heading"><div><span className="section-kicker">{translate("REDE")}</span><h2>{translate("Interfaces do host")}</h2></div><Wifi size={18}/></div>
      {interfaces.length ? <div className="host-interface-list">{interfaces.map((iface) => <article className="host-interface-row" key={iface}><strong>{iface}</strong><span><small>{translate("Recebido")}</small>{networkValue('host.network_rx_bytes_per_second', iface)}</span><span><small>{translate("Enviado")}</small>{networkValue('host.network_tx_bytes_per_second', iface)}</span></article>)}</div> : <EmptyState icon={Network} title={translate("Sem interfaces medidas")} body={translate("As taxas de rede serão mostradas quando o coletor tiver duas amostras disponíveis.")}/>}
    </section>
    <section className="panel host-overview-panel"><div className="panel-heading"><div><span className="section-kicker">{translate("ARMAZENAMENTO DO HOST")}</span><h2>{translate("Capacidade dos volumes")}</h2></div><Database size={18}/></div>
      {filesystemMounts.length ? <div className="host-filesystem-list">{filesystemMounts.map((mount) => {
        const total = filesystemCapacity(mount, 'host.filesystem_total_bytes');
        const available = filesystemCapacity(mount, 'host.filesystem_available_bytes');
        return <div className="host-filesystem-row" key={mount}><strong>{mount}</strong><span>{available ? `${formatBytes(Number(available.value))} livres` : translate("Livre indisponível")}</span><small>{total ? `de ${formatBytes(Number(total.value))}` : translate("Capacidade indisponível")}</small></div>;
      })}</div> : <p className="inline-message">{translate("O coletor não encontrou volumes reportáveis neste ciclo.")}</p>}
    </section>
    <section className="panel host-overview-panel"><div className="panel-heading"><div><span className="section-kicker">{translate("SENSORES")}</span><h2>{translate("Temperatura do host")}</h2></div><Thermometer size={18}/></div>
      {unavailableSensors ? <p className="inline-message">{translate("O sistema não reportou sensores de temperatura. O painel não estima nem substitui essa leitura.")}</p> : <div className="host-facts">{availableTemperatures.map(([key, sample]) => {
        const sensorName = sample.labels.sensor_label
          ? `${sample.labels.sensor_label}${sample.labels.sensor ? ` (${sample.labels.sensor})` : ''}`
          : sample.labels.sensor;
        const label = [sample.labels.chip, sensorName, sample.labels.pci_bdf ?? sample.labels.hwmon].filter(Boolean).join(' · ') || 'Sensor';
        return <span key={key}><small>{label}</small><strong>{formatSample(sample)}</strong></span>;
      })}</div>}
    </section>
    <section className="panel host-overview-panel"><div className="panel-heading"><div><span className="section-kicker">{translate("GPU · TELEMETRIA LOCAL")}</span><h2>{translate("Placas gráficas")}</h2></div><Monitor size={18}/></div>
      {gpus.data?.stale && <p className="inline-message" role="status">{translate(gpus.data.message ?? 'O inventário de GPU não foi atualizado.')}{' '}{translate("Exibindo a última leitura conhecida; novas seleções estão desativadas.")}</p>}
      {gpuBdfs.length ? <div className="gpu-telemetry-grid">{gpuBdfs.map((bdf) => {
        const busy = gpuSample(bdf, 'host.gpu.busy_percent');
        const used = gpuSample(bdf, 'host.gpu.vram_used_bytes');
        const total = gpuSample(bdf, 'host.gpu.vram_total_bytes');
        const temperature = gpuSample(bdf, 'host.gpu.temperature_celsius');
        const power = gpuSample(bdf, 'host.gpu.power_watts');
        const fan = gpuSample(bdf, 'host.gpu.fan_percent');
        const memory = used?.availability === 'available' && total?.availability === 'available'
          ? `${formatBytes(Number(used.value))} / ${formatBytes(Number(total.value))}` : translate("Indisponível");
        return <article className="gpu-telemetry-card" key={bdf}><div><strong>{gpuLabel(bdf)}</strong><code>{bdf}</code></div><dl>
          <div><dt>{translate("Uso do processador gráfico")}</dt><dd>{formatSample(busy, (value) => `${value.toLocaleString(appLocale, { maximumFractionDigits: 1 })}%`)}</dd></div>
          <div><dt>{translate("Memória de vídeo")}</dt><dd>{memory}</dd></div>
          <div><dt>{translate("Temperatura")}</dt><dd>{formatSample(temperature)}</dd></div>
          <div><dt>{translate("Potência")}</dt><dd>{formatSample(power, (value) => `${value.toLocaleString(appLocale, { maximumFractionDigits: 1 })} W`)}</dd></div>
          <div><dt>{translate("Ventoinha")}</dt><dd>{formatSample(fan, (value) => `${value.toLocaleString(appLocale, { maximumFractionDigits: 1 })}%`)}</dd></div>
        </dl></article>;
      })}</div> : <p className="inline-message">{gpus.data?.stale ? `${translate(gpus.data.message ?? 'Não foi possível atualizar o inventário de GPU.')} ${translate('Não há uma leitura anterior para exibir.')}` : gpus.data?.items.length === 0 ? translate("O inventário não encontrou placas gráficas físicas.") : gpus.error ? translate("Não foi possível consultar o inventário de placas gráficas.") : translate("Aguardando dados do inventário de placas gráficas.")}</p>}
    </section>
  </>;
}

function Vms() {
  const navigate = useNavigate();
  const notify = useToast();
  const vms = usePolling<VmListResponse>('/vms', 8000);
  const vmRuntimes = usePolling<VmRuntimeList>('/vms/runtime', 8000);
  const [importOpen, setImportOpen] = useState(false);
  const [importId, setImportId] = useState('');
  const [importVersion, setImportVersion] = useState('tahoe');
  const [importing, setImporting] = useState(false);
  const imports = usePolling<ReimsImportCandidateList>(importOpen ? '/vms/import-candidates' : null, 30000);
  const selectedImport = imports.data?.items.find((candidate) => candidate.id === importId) ?? null;
  useEffect(() => {
    if (!importOpen || !imports.data || imports.data.items.some((candidate) => candidate.id === importId)) return;
    setImportId(imports.data.items.find((candidate) => candidate.importable)?.id ?? '');
  }, [importOpen, imports.data, importId]);
  const items = vms.data?.items ?? [];
  const [search, setSearch] = useState('');
  const [versionFilter, setVersionFilter] = useState('all');
  const [installFilter, setInstallFilter] = useState('all');
  const [sortBy, setSortBy] = useState('created');
  const versions = useMemo(() => [...new Set(items.map(({ config }) => config.macos_version))].sort((a, b) => a.localeCompare(b, 'pt-BR', { numeric: true })), [items]);
  const filteredItems = useMemo(() => {
    const query = search.trim().toLocaleLowerCase('pt-BR');
    const filtered = items.filter(({ config }) => {
      const matchesSearch = !query || [config.name, config.id, config.macos_version, config.selected_gpu_name ?? '', config.selected_gpu ?? '']
        .some((value) => value.toLocaleLowerCase('pt-BR').includes(query));
      return matchesSearch
        && (versionFilter === 'all' || config.macos_version === versionFilter)
        && (installFilter === 'all' || config.install_phase === installFilter);
    });
    return [...filtered].sort((left, right) => {
      const a = left.config;
      const b = right.config;
      if (sortBy === 'name') return a.name.localeCompare(b.name, 'pt-BR', { sensitivity: 'base' }) || a.id.localeCompare(b.id);
      if (sortBy === 'ram') return b.ram_gb - a.ram_gb || a.name.localeCompare(b.name, 'pt-BR', { sensitivity: 'base' });
      if (sortBy === 'vcpus') return b.vcpus - a.vcpus || a.name.localeCompare(b.name, 'pt-BR', { sensitivity: 'base' });
      return b.created_at.localeCompare(a.created_at) || a.name.localeCompare(b.name, 'pt-BR', { sensitivity: 'base' });
    });
  }, [items, search, versionFilter, installFilter, sortBy]);
  const filtersActive = Boolean(search.trim()) || versionFilter !== 'all' || installFilter !== 'all' || sortBy !== 'created';
  const resetFilters = () => { setSearch(''); setVersionFilter('all'); setInstallFilter('all'); setSortBy('created'); };
  const importVm = async () => {
    if (!selectedImport?.importable || !vms.data?.revision) return;
    setImporting(true);
    try {
      await api.requestWithHeaders<VmConfigEnvelope>('/vms/import', {
        method: 'POST',
        headers: { 'If-Match': `"${vms.data.revision}"` },
        body: JSON.stringify({ id: selectedImport.id, macos_version: importVersion }),
      });
      setImportOpen(false);
      setImportId('');
      await vms.refresh();
      notify(`VM ${selectedImport.name} adicionada ao catálogo. OpenCore, EFI e discos existentes foram preservados.`, 'success');
    } catch (error) {
      notify(error instanceof Error ? translate(error.message) : translate("Não foi possível importar a VM do Reims."), 'error');
    } finally {
      setImporting(false);
    }
  };
  return <>
    <PageHeading eyebrow="RECURSOS COMPUTACIONAIS" title={translate("Máquinas virtuais")} description="VMs e recursos configurados conforme o catálogo do appliance." actions={<button className="button button-primary" onClick={() => navigate('/vms/new')}><Plus size={17}/>{translate("Criar VM")}</button>}/>
    <DataState data={vms.data} error={vms.error} loading={vms.loading} updatedAt={vms.updatedAt}/>
    <DataState data={vmRuntimes.data} error={vmRuntimes.error} loading={vmRuntimes.loading} updatedAt={vmRuntimes.updatedAt}/>
    <div className="toolbar inventory-toolbar">
      <span className="inventory-count" aria-live="polite">{vms.data ? `${filteredItems.length} de ${items.length} ${items.length === 1 ? translate("máquina cadastrada") : translate("máquinas cadastradas")}` : translate("Catálogo não disponível")}</span>
      <button className="button button-secondary" type="button" onClick={() => setImportOpen(true)}><ArrowDownToLine size={16}/>{translate("Importar do Reims")}</button>
    </div>
    {items.length > 0 && <section className="inventory-controls" aria-label={translate("Buscar, filtrar e ordenar máquinas virtuais")}>
      <label className="inventory-search"><span>{translate("Buscar")}</span><input type="search" value={search} onChange={(event) => setSearch(event.target.value)} placeholder={translate("Nome, ID, macOS ou GPU")} aria-label={translate("Buscar VMs por nome, ID, versão do macOS ou placa de vídeo")} /></label>
      <label><span>{translate("Versão do macOS")}</span><select value={versionFilter} onChange={(event) => setVersionFilter(event.target.value)}><option value="all">{translate("Todas")}</option>{versions.map((version) => <option key={version} value={version}>{version}</option>)}</select></label>
      <label><span>{translate("Instalação")}</span><select value={installFilter} onChange={(event) => setInstallFilter(event.target.value)}><option value="all">{translate("Todas")}</option><option value="pending">{translate("Pendente")}</option><option value="installing">{translate("Em andamento")}</option><option value="installed">{translate("Instalada")}</option></select></label>
      <label><span>{translate("Ordenar por")}</span><select value={sortBy} onChange={(event) => setSortBy(event.target.value)}><option value="created">{translate("Mais recentes")}</option><option value="name">{translate("Nome (A–Z)")}</option><option value="ram">{translate("Mais memória")}</option><option value="vcpus">{translate("Mais vCPUs")}</option></select></label>
    </section>}
    <section className="panel inventory-panel">
      {filteredItems.length ? <ResponsiveList className="vm-list" label={translate("Máquinas virtuais cadastradas")}>{filteredItems.map((item) => <li className="vm-row-entry" key={item.config.id}><VmCard config={item.config} runtime={vmRuntimes.data?.items.find((runtime) => runtime.id === item.config.id)}/><NavLink className="vm-row-edit" to={`/vms/${encodeURIComponent(item.config.id)}/edit`} aria-label={`Editar ${item.config.name}`}>{translate("Editar")}</NavLink><VmDeleteAction vmId={item.config.id} vmName={item.config.name} revision={item.revision} onDeleted={() => { void vms.refresh(); }}/></li>)}</ResponsiveList>
        : items.length ? <EmptyState icon={Layers3} title={translate("Nenhuma VM corresponde aos filtros")} body={translate("Ajuste a busca ou os filtros para ver outras máquinas.")} action={filtersActive ? <button className="button button-secondary" onClick={resetFilters}>{translate("Limpar filtros")}</button> : undefined}/>
          : vms.data ? <EmptyState icon={Layers3} title={translate("Nenhuma VM cadastrada")} body={translate("O catálogo do appliance não contém máquinas virtuais.")} action={<button className="button button-primary" onClick={() => navigate('/vms/new')}><Plus size={16}/>{translate("Criar primeira VM")}</button>}/>
            : <EmptyState icon={Layers3} title={vms.error ? translate("Não foi possível carregar as VMs") : translate("Carregando catálogo")} body={translate(vms.error?.message ?? 'Aguardando a resposta de /vms.')}/>}
    </section>
    <ConfirmationDialog
      open={importOpen}
      title={translate("Importar VM do Reims")}
      description="Revise a configuração registrada. A importação adiciona a VM ao catálogo sem copiar nem alterar seus discos, OpenCore, EFI ou NVRAM."
      confirmLabel="Importar VM"
      busy={importing}
      confirmDisabled={!selectedImport?.importable || !vms.data?.revision}
      onConfirm={() => void importVm()}
      onClose={() => { if (!importing) setImportOpen(false); }}
    >
      {imports.loading && <p role="status">{translate("Lendo o registro local do Reims…")}</p>}
      {imports.error && <p className="ui-field-error" role="alert">{translate(imports.error.message)}</p>}
      {imports.data && imports.data.items.length === 0 && <p>{translate("Nenhuma VM registrada no Reims foi encontrada.")}</p>}
      {imports.data && imports.data.items.length > 0 && <div className="reims-import-fields">
        <label className="ui-field"><span>{translate("VM registrada")}</span><select value={importId} onChange={(event) => setImportId(event.target.value)}>
          <option value="">{translate("Selecione uma VM")}</option>
          {imports.data.items.map((candidate) => <option key={`${candidate.id}:${candidate.directory}`} value={candidate.id} disabled={!candidate.importable}>
            {candidate.name}{candidate.runtime_state === 'running' ? ` · ${translate('em execução')} (PID ${candidate.runtime_pid})` : candidate.runtime_state === 'unknown' ? translate(" · estado não confirmado") : candidate.importable ? '' : translate(" · indisponível")}
          </option>)}
        </select></label>
        <label className="ui-field"><span>{translate("Versão do macOS")}</span><select value={importVersion} onChange={(event) => setImportVersion(event.target.value)}>
          <option value="tahoe">{translate("macOS Tahoe 26")}</option><option value="sequoia">{translate("macOS Sequoia 15")}</option><option value="ventura">{translate("macOS Ventura 13")}</option>
        </select></label>
        {selectedImport && <div className="reims-import-review" aria-live="polite">
          <strong>{selectedImport.name}</strong>
          <span>{selectedImport.vcpus}{' '}{translate("vCPU ·")}{' '}{selectedImport.ram_gb}{' '}{translate("GiB RAM ·")}{' '}{selectedImport.disk_size_gb}{' '}{translate("GiB de disco")}</span>
          <span>{translate("MAC:")}{' '}{selectedImport.mac ?? 'não informado'} · {selectedImport.display_resolution}{selectedImport.display_hidpi ? ' HiDPI' : ''}</span>
          <code>{selectedImport.directory}</code>
          {selectedImport.runtime_state === 'running' && <small role="alert">{translate("VM legada ativa no Reims (PID")}{' '}{selectedImport.runtime_pid}{translate("). O painel apenas a observa; pare-a pelo Reims e atualize a lista antes de importar.")}</small>}
          {selectedImport.runtime_state === 'unknown' && <small role="alert">{translate("O estado da VM não pôde ser confirmado pelo Reims. A importação fica bloqueada para evitar assumir controle enquanto ela pode estar ativa.")}</small>}
          {selectedImport.runtime_state === 'stopped' && <small>{translate("VM parada confirmada pelo Reims. Importar adiciona somente o cadastro e mantém discos, EFI, NVRAM e configuração de origem sem alteração.")}</small>}
          {selectedImport.limitation && <small role={selectedImport.importable ? 'note' : 'alert'}>{translate(selectedImport.limitation)}</small>}
          {selectedImport.importable && <small>{translate("Identidade de firmware/EFI permanece nos arquivos existentes; não é regenerada pela importação.")}</small>}
        </div>}
      </div>}
    </ConfirmationDialog>
  </>;
}

function VmDetail() {
  const location = useLocation();
  const navigate = useNavigate();
  const notify = useToast();
  const { vmId } = useParams();
  const safeId = vmId ? encodeURIComponent(vmId) : '';
  const catalog = usePolling<VmListResponse>('/vms', 10000);
  const runtime = usePolling<VmRuntime>(vmId ? `/vms/${safeId}/runtime` : null, 4000);
  const capabilities = usePolling<CapabilitiesResponse>('/capabilities', 15000);
  const [lifecyclePending, setLifecyclePending] = useState(false);
  const [installConfirmOpen, setInstallConfirmOpen] = useState(false);
  const [installConfirmation, setInstallConfirmation] = useState('');
  const [installConfirmPending, setInstallConfirmPending] = useState(false);
  const [pickerDialogOpen, setPickerDialogOpen] = useState(false);
  const [pickerDesired, setPickerDesired] = useState(false);
  const [pickerConfirmation, setPickerConfirmation] = useState('');
  const [pickerPending, setPickerPending] = useState(false);
  const [smbiosDiagnosticPending, setSmbiosDiagnosticPending] = useState(false);
  const [confirmAction, setConfirmAction] = useState<'shutdown' | 'restart' | 'force_stop' | null>(null);
  const [forceStopConfirmation, setForceStopConfirmation] = useState('');
  const [backupPending, setBackupPending] = useState(false);
  const [backupConfirmOpen, setBackupConfirmOpen] = useState(false);
  const [verifyingBackupId, setVerifyingBackupId] = useState<string | null>(null);
  const [restoreBackupId, setRestoreBackupId] = useState<string | null>(null);
  const [restoreConfirmation, setRestoreConfirmation] = useState('');
  const [restoringBackupId, setRestoringBackupId] = useState<string | null>(null);
  const [cloneDialogOpen, setCloneDialogOpen] = useState(false);
  const [cloneId, setCloneId] = useState('');
  const [cloneName, setCloneName] = useState('');
  const [cloneConfirmation, setCloneConfirmation] = useState('');
  const [clonePending, setClonePending] = useState(false);
  const metrics = useEventStream<TelemetryResponse & { vm_id: string; message?: string | null }>(vmId ? `/vms/${safeId}/metrics/stream` : null);
  const current = location.pathname.split('/').filter(Boolean).slice(2)[0] ?? 'summary';
  const serialConsole = usePolling<VmSerialConsole>(vmId && current === 'console' ? `/vms/${safeId}/serial` : null, 5000);
  const storage = usePolling<StorageInventory>(vmId && current === 'storage' ? '/storage' : null, 15000);
  const backups = usePolling<VmBackupListResponse>(vmId && current === 'storage' ? `/vms/${safeId}/backups` : null, 15000);
  const configEntry = catalog.data?.items.find((item) => item.config.id === vmId);
  const config = configEntry?.config;
  const pickerCapability = capabilities.data?.items.find((item) => item.id === 'vm.opencore.picker');
  const smbiosDiagnosticCapability = capabilities.data?.items.find((item) => item.id === 'vm.smbios.diagnostics');
  const virtioAudioAvailable = capabilities.data?.items.some((item) => item.id === 'vm.audio.virtio_device' && item.available) ?? false;
  const audioRelease = capabilities.data?.runtime?.reims_release_tag;
  const audioModelLabel = !config?.audio_enabled
    ? 'Desativado'
    : virtioAudioAvailable
      ? `VirtIO Sound · Reims ${audioRelease ?? 'v1.4.0+'} (próximo boot)`
      : audioRelease
        ? `USB Audio + HDA · Reims ${audioRelease} (próximo boot)`
        : translate("Modelo do Reims não confirmado");
  const lifecycleCapability = capabilities.data?.items.find((item) => item.id === 'vm.lifecycle');
  const canStartVm = lifecycleCapability?.available === true;
  // A failed systemd unit with no QEMU PID is a confirmed stopped guest and
  // systemd start can retry it. Unknown state or any listed process stays blocked.
  const canRetryFailedStart = runtime.data?.state === 'degraded'
    && runtime.data.pid === null
    && runtime.data.supervisor?.active_state === 'failed';
  const startableVmState = runtime.data?.state === 'stopped' || canRetryFailedStart;
  const lifecycleMessage = runtime.data?.state === 'running' && !runtime.data.qmp_available
    ? translate("Esta VM já estava em execução quando o relay QMP foi atualizado; as ações de energia ficam disponíveis na próxima partida da VM.")
    : translate("A inicialização usa uma unidade isolada. Desligamento e reinício aguardam ACPI; a parada forçada exige confirmação e QMP.");
  const startDisabledReason = capabilities.error
    ? translate("Não foi possível confirmar se o agente permite iniciar VMs.")
    : !lifecycleCapability
      ? translate('Verificando se o runtime pode iniciar VMs…')
      : (lifecycleCapability.limitation ? translate(lifecycleCapability.limitation) : null) ?? (!canStartVm
        ? translate("O supervisor por VM e o serviço de tarefas precisam estar disponíveis no appliance.")
        : runtime.loading || !runtime.data
          ? translate("Aguardando confirmação do estado e do supervisor da VM.")
          : canRetryFailedStart
            ? translate("A tentativa anterior falhou e o host confirmou que não há processo QEMU; você pode tentar iniciar novamente.")
            : runtime.data.state !== 'stopped'
            ? translate('A VM precisa estar parada para iniciar.')
            : !runtime.data.supervisor
              ? translate("O agente não confirmou o supervisor desta VM.")
              : null);
  const samples = metrics.data?.samples;
  const vmCpuSample = sampleFor(samples, 'vm.process.cpu_percent');
  const vmRssSample = sampleFor(samples, 'vm.process.rss_bytes');
  const runtimeLabels: Record<VmRuntime['state'], string> = { stopped: 'Parada', starting: 'Iniciando', running: translate("Em execução"), stopping: 'Parando', degraded: 'Degradada', unknown: 'Desconhecido' };
  const tabs = ['Resumo', 'Hardware', translate("GPU"), 'Discos', translate("Rede"), 'Console', translate("Opções")];
  const selected = ({ summary: 'Resumo', hardware: 'Hardware', gpu: translate("GPU"), storage: 'Discos', network: translate("Rede"), console: 'Console', options: translate("Opções") } as Record<string, string>)[current] ?? 'Resumo';
  const storageItem = storage.data?.items.find((item) => item.references.includes(vmId ?? ''));
  const vmLabels = (name: string) => name === 'vm.process.cpu_percent' ? 'CPU do processo'
    : name === 'vm.process.rss_bytes' ? translate("Memória residente")
      : translate("Métrica");
  const requestLifecycleAction = async (operation: 'start' | 'shutdown' | 'restart' | 'force_stop') => {
    if (!vmId || !configEntry || lifecyclePending) return;
    if (operation === 'start' && (!canStartVm || !startableVmState || !runtime.data?.supervisor)) return;
    if (operation !== 'start' && (runtime.data?.state !== 'running' || !runtime.data.qmp_available)) return;
    setLifecyclePending(true);
    try {
      const accepted = await api.request<JobAccepted>(`/vms/${safeId}/actions`, {
        method: 'POST',
        body: JSON.stringify({ operation, expected_revision: configEntry.revision, confirmation: operation === 'force_stop' ? forceStopConfirmation : undefined, idempotency_key: crypto.randomUUID() }),
      });
      const actionLabel = ({ start: translate("Início"), shutdown: 'Desligamento ACPI', restart: translate("Reinicialização"), force_stop: translate("Parada forçada") } as Record<typeof operation, string>)[operation];
      notify(`${actionLabel} enviado. Acompanhe a tarefa ${accepted.job_id.slice(0, 8)}.`, 'success');
      navigate('/tasks');
    } catch (error) {
      notify(error instanceof Error ? translate(error.message) : translate("Não foi possível controlar a VM."), 'error');
    } finally { setLifecyclePending(false); setConfirmAction(null); setForceStopConfirmation(''); }
  };
  const confirmInstallation = async () => {
    if (!vmId || !configEntry || runtime.data?.state !== 'stopped' || installConfirmation !== vmId || installConfirmPending) return;
    setInstallConfirmPending(true);
    try {
      await api.request<VmConfigEnvelope>(`/vms/${safeId}/install/confirm`, {
        method: 'POST',
        headers: { 'If-Match': `"${configEntry.revision}"` },
        body: JSON.stringify({ confirmation: installConfirmation }),
      });
      await catalog.refresh();
      await runtime.refresh();
      notify(translate("Instalação marcada como concluída. A identidade da VM e o macOS não foram alterados."), 'success');
      setInstallConfirmOpen(false);
      setInstallConfirmation('');
    } catch (error) {
      notify(error instanceof Error ? translate(error.message) : translate("Não foi possível confirmar a instalação."), 'error');
    } finally { setInstallConfirmPending(false); }
  };
  const updateOpenCorePicker = async () => {
    if (!vmId || !configEntry || runtime.data?.state !== 'stopped' || pickerConfirmation !== vmId || pickerPending || !pickerCapability?.available) return;
    setPickerPending(true);
    try {
      const accepted = await api.request<JobAccepted>(`/vms/${safeId}/opencore/picker`, {
        method: 'POST',
        headers: { 'If-Match': `"${configEntry.revision}"` },
        body: JSON.stringify({ boot_selection: pickerDesired, confirmation: pickerConfirmation, idempotency_key: crypto.randomUUID() }),
      });
      notify(`Regeneração do OpenCore enviada. Acompanhe a tarefa ${accepted.job_id.slice(0, 8)}.`, 'success');
      navigate('/tasks');
    } catch (error) {
      notify(error instanceof Error ? translate(error.message) : translate("Não foi possível solicitar a atualização do picker OpenCore."), 'error');
    } finally { setPickerPending(false); }
  };
  const diagnoseSmbios = async () => {
    if (!vmId || !configEntry || runtime.data?.state !== 'stopped' || smbiosDiagnosticPending || !smbiosDiagnosticCapability?.available) return;
    setSmbiosDiagnosticPending(true);
    try {
      const accepted = await api.request<JobAccepted>(`/vms/${safeId}/smbios/diagnostics`, {
        method: 'POST',
        headers: { 'If-Match': `"${configEntry.revision}"` },
        body: JSON.stringify({ idempotency_key: crypto.randomUUID() }),
      });
      notify(`Diagnóstico SMBIOS enviado. Confira o resultado na tarefa ${accepted.job_id.slice(0, 8)}.`, 'success');
      navigate('/tasks');
    } catch (error) {
      notify(error instanceof Error ? translate(error.message) : translate("Não foi possível iniciar o diagnóstico SMBIOS."), 'error');
    } finally { setSmbiosDiagnosticPending(false); }
  };
  const requestBackup = async () => {
    if (!vmId || !config || runtime.data?.state !== 'stopped' || backupPending) return;
    setBackupPending(true);
    try {
      const accepted = await api.request<JobAccepted>(`/vms/${safeId}/backups`, {
        method: 'POST',
        body: JSON.stringify({ idempotency_key: crypto.randomUUID() }),
      });
      notify(`Backup offline iniciado. Acompanhe a tarefa ${accepted.job_id.slice(0, 8)}.`, 'success');
      navigate('/tasks');
    } catch (error) {
      notify(error instanceof Error ? translate(error.message) : translate("Não foi possível iniciar o backup offline."), 'error');
    } finally { setBackupPending(false); }
  };
  const requestBackupVerification = async (backupId: string) => {
    if (!vmId || verifyingBackupId) return;
    setVerifyingBackupId(backupId);
    try {
      const accepted = await api.request<JobAccepted>(`/vms/${safeId}/backups/${encodeURIComponent(backupId)}/verify`, {
        method: 'POST',
        body: JSON.stringify({ idempotency_key: crypto.randomUUID() }),
      });
      notify(`Verificação de integridade iniciada. Acompanhe a tarefa ${accepted.job_id.slice(0, 8)}.`, 'success');
      navigate('/tasks');
    } catch (error) {
      notify(error instanceof Error ? translate(error.message) : translate("Não foi possível verificar o backup."), 'error');
    } finally { setVerifyingBackupId(null); }
  };
  const requestBackupRestore = async () => {
    if (!vmId || !restoreBackupId || runtime.data?.state !== 'stopped' || restoringBackupId || restoreConfirmation !== vmId) return;
    setRestoringBackupId(restoreBackupId);
    try {
      const accepted = await api.request<JobAccepted>(`/vms/${safeId}/backups/${encodeURIComponent(restoreBackupId)}/restore`, {
        method: 'POST',
        body: JSON.stringify({ idempotency_key: crypto.randomUUID(), confirmation: `${vmId}:${restoreBackupId}` }),
      });
      notify(`Restauração iniciada. Acompanhe a tarefa ${accepted.job_id.slice(0, 8)}.`, 'success');
      navigate('/tasks');
    } catch (error) {
      notify(error instanceof Error ? translate(error.message) : translate("Não foi possível iniciar a restauração."), 'error');
    } finally { setRestoringBackupId(null); setRestoreBackupId(null); setRestoreConfirmation(''); }
  };
  const requestClone = async () => {
    if (!vmId || !config || runtime.data?.state !== 'stopped' || clonePending || cloneConfirmation !== cloneId) return;
    setClonePending(true);
    try {
      const accepted = await api.request<JobAccepted>(`/vms/${safeId}/clone`, {
        method: 'POST',
        body: JSON.stringify({
          idempotency_key: crypto.randomUUID(), clone_id: cloneId,
          name: cloneName.trim(), confirmation: `${vmId}:${cloneId}`,
        }),
      });
      notify(`Clonagem iniciada. Acompanhe a tarefa ${accepted.job_id.slice(0, 8)}.`, 'success');
      navigate('/tasks');
    } catch (error) {
      notify(error instanceof Error ? translate(error.message) : translate("Não foi possível iniciar a clonagem."), 'error');
    } finally { setClonePending(false); setCloneDialogOpen(false); setCloneConfirmation(''); }
  };
  return <>
    <button className="back-link" onClick={() => navigate('/vms')}><ArrowLeft size={16}/>{translate("Voltar às máquinas virtuais")}</button>
    <PageHeading eyebrow="MÁQUINA VIRTUAL" title={config?.name ?? translate('Detalhes da VM')} description={config ? `${config.macos_version} · ID ${config.id}` : translate("Carregando configuração do catálogo.")} actions={<div className="vm-detail-actions"><div className="button-row"><button className="button button-secondary" disabled={!config} onClick={() => navigate(`/vms/${safeId}/edit`)} title={translate("Editar os campos de configuração suportados")}><Settings2 size={16}/>{translate("Editar")}</button><button className="button button-primary" disabled={!configEntry || !canStartVm || !startableVmState || !runtime.data?.supervisor || lifecyclePending} onClick={() => void requestLifecycleAction('start')} title={translate(startDisabledReason ?? 'Inicia a VM pela unidade systemd isolada')}><Power size={16}/>{canRetryFailedStart ? translate("Tentar novamente") : translate('Iniciar')}</button>{runtime.data?.state === 'running' && <><button className="button button-secondary" disabled={!runtime.data.qmp_available || lifecyclePending} onClick={() => setConfirmAction('shutdown')} title={translate("Envia pedido ACPI pelo QMP; não força o desligamento")}><Power size={16}/>{translate("Desligar")}</button><button className="button button-secondary" disabled={!runtime.data.qmp_available || !runtime.data.supervisor || lifecyclePending} onClick={() => setConfirmAction('restart')} title={translate("Desliga por ACPI e inicia novamente pela unidade supervisionada")}><RefreshCw size={16}/>{translate("Reiniciar")}</button><button className="button button-danger" disabled={!runtime.data.qmp_available || lifecyclePending} onClick={() => setConfirmAction('force_stop')} title={translate("Envia QMP quit, sem matar o processo pelo sistema")}><Power size={16}/>{translate("Parada forçada")}</button></>}{config && configEntry && <VmDeleteAction vmId={config.id} vmName={config.name} revision={configEntry.revision} onDeleted={() => navigate('/vms', { replace: true })}/>}</div><small className="disabled-reason">{runtime.data?.state === 'running' && !runtime.data.qmp_available ? translate(lifecycleMessage) : translate(startDisabledReason ?? lifecycleMessage)}</small></div>}/>
    <DataState data={config} error={catalog.error} loading={catalog.loading} updatedAt={catalog.updatedAt}/>
    {config && <div className="detail-shell">
      <NavigationTabs label={translate("Seções da máquina virtual")} className="detail-tabs" items={tabs.map((tab) => {
        const slug = ({ Resumo: 'summary', Hardware: 'hardware', GPU: 'gpu', Discos: 'storage', Rede: 'network', Console: 'console', Opções: 'options' } as Record<string, string>)[tab];
        return { label: tab, to: `/vms/${safeId}/${slug}`, current: selected === tab };
      })}/>
      <section className="panel detail-content">
        {selected === 'Resumo' ? <>
          <div className="runtime-banner"><span className={`vm-state state-${runtime.data?.state ?? 'unknown'}`}>{runtime.data ? translate(runtimeLabels[runtime.data.state]) : translate("Estado indisponível")}</span><span className="runtime-observed">{translate("Observado:")}{' '}{formatTimestamp(runtime.data?.observed_at)}</span></div>
          {configEntry?.projection_status === 'unknown' && <p className="vm-field-hint" role="status">{translate("Ainda não há uma projeção Reims confiável para comparar com a configuração salva.")}</p>}
          {configEntry?.runtime_config_differs && <><p className="vm-pending-note" role="status">{translate("Há diferenças entre a configuração salva e a última projeção local do Reims. As mudanças salvas entram no próximo boot completo.")}</p><div className="vm-config-diff-list">{configEntry.config_differences.map((difference) => <div key={difference.field}><strong>{({ vcpus: translate("vCPU"), cpu_model: 'Modelo de CPU', ram_gb: translate("Memória"), selected_gpu: translate("Placa gráfica"), audio_backend: translate("Backend de áudio"), network_enabled: translate("Rede"), network_mac: translate("Endereço MAC"), display_resolution: translate("Resolução"), display_hidpi: 'HiDPI', fullscreen: 'Tela cheia', display_refresh_hz: translate("Taxa de atualização"), qemu_extra_args: translate("Opções QEMU") } as Record<string, string>)[difference.field] ?? difference.field.replaceAll('_', ' ')}</strong><span>{translate("Salvo:")}{' '}{difference.saved_value}</span><span>{translate("Última projeção:")}{' '}{difference.last_projection_value}</span></div>)}</div></>}
          <DataState data={runtime.data} error={runtime.error} loading={runtime.loading} updatedAt={runtime.updatedAt}/>
          <DataState data={metrics.data} error={metrics.error} loading={metrics.loading} updatedAt={metrics.updatedAt}/>
          <div className="telemetry-history-grid vm-telemetry-history">
            <TelemetryHistoryChart title={vmLabels('vm.process.cpu_percent')} valueLabel={translate("CPU")} unit="%" color="#22d3ee" currentValue={formatSample(vmCpuSample)} currentSource={vmCpuSample?.source ?? ''} currentUpdatedAt={vmCpuSample ? formatTimestamp(vmCpuSample.timestamp) : translate("sem amostra")} historyPath={`/vms/${safeId}/metrics/history`} metricName="vm.process.cpu_percent" min={0} max={100}/>
            <TelemetryHistoryChart title={vmLabels('vm.process.rss_bytes')} valueLabel="RSS" unit={translate("GiB")} color="#60a5fa" currentValue={formatSample(vmRssSample, formatBytes)} currentSource={vmRssSample?.source ?? ''} currentUpdatedAt={vmRssSample ? formatTimestamp(vmRssSample.timestamp) : translate("sem amostra")} historyPath={`/vms/${safeId}/metrics/history`} metricName="vm.process.rss_bytes" transform={bytesToGiB} formatAxis={formatGiB}/>
          </div>
      <div className="detail-metrics">
            <MetricCard label={translate("CPU do processo")} icon={Cpu} value={formatSample(vmCpuSample)} helper={translate("Fonte: processo QEMU, normalizado pelos vCPUs")}/>
            <MetricCard label={translate("Memória residente")} icon={Layers3} value={formatSample(vmRssSample, formatBytes)} helper={translate("RSS do processo; não representa uso interno do macOS")}/>
            <MetricCard label={translate("Memória configurada")} icon={Database} value={`${config.ram_gb} GiB`} helper={translate("Valor configurado na VM")}/>
            <MetricCard label={translate("Uso de memória guest")} icon={Activity} value={formatSample(sampleFor(samples, 'vm.guest.memory_used_bytes'), formatBytes)} helper={translate("Disponível somente se o agente do guest reportar")}/>
          </div>
          <div className="detail-facts"><span><small>{translate("QMP")}</small><strong>{runtime.data ? runtime.data.qmp_available ? translate("Disponível") : translate("Indisponível") : translate("Sem resposta")}</strong></span><span><small>{translate("Supervisor")}</small><strong>{runtime.data?.supervisor ? translate(({ active: 'Ativo', activating: 'Iniciando', deactivating: 'Encerrando', failed: 'Falhou', inactive: 'Parado' } as Record<string, string>)[runtime.data.supervisor.active_state] ?? runtime.data.supervisor.active_state) : translate("Não instalado")}</strong></span><span><small>{translate("Fase de instalação")}</small><strong>{translate(runtime.data?.install_phase ?? config.install_phase)}</strong></span><span><small>{translate("GPU configurada")}</small><strong>{translate(runtime.data?.gpu?.configured_device_uuid ?? runtime.data?.gpu?.configured_pci_address ?? config.selected_gpu ?? 'Padrão do Reims')}</strong></span><span><small>{translate("GPU efetiva")}</small><strong>{runtime.data?.gpu?.effective_uuid ? `${runtime.data.gpu.effective_uuid} · ${runtime.data.gpu.effective_bdf}` : translate("Não confirmada")}</strong></span><span><small>{translate("Verificação de GPU")}</small><strong>{translate(runtime.data?.gpu?.verification ?? 'Sem verificação')}</strong></span></div>
          {config.install_phase !== 'installed' && <div className="install-confirm-panel"><div><strong>{translate("Instalação do macOS")}</strong><p>{translate("Quando o macOS terminar de instalar e iniciar normalmente, confirme a fase com a VM parada. Esta ação registra sua confirmação; não detecta nem valida o macOS automaticamente.")}</p></div><button className="button button-secondary" type="button" disabled={runtime.data?.state !== 'stopped' || !configEntry || installConfirmPending} onClick={() => { setInstallConfirmation(''); setInstallConfirmOpen(true); }} title={runtime.data?.state !== 'stopped' ? translate("Pare a VM antes de confirmar a instalação") : undefined}>{translate("Confirmar instalação concluída")}</button></div>}
          {runtime.data?.message && <p className="inline-message">{translate(runtime.data.message)}</p>}
          {metrics.data?.message && <p className="inline-message">{metrics.data.message}</p>}
        </> : selected === 'Hardware' ? <div className="detail-facts"><span><small>{translate("vCPU")}</small><strong>{config.vcpus}</strong></span><span><small>{translate("Memória")}</small><strong>{config.ram_gb}{' '}{translate("GiB")}</strong></span><span><small>{translate("Disco configurado")}</small><strong>{config.disk_size_gb}{' '}{translate("GiB")}</strong></span><span><small>{translate("Fase registrada")}</small><strong>{translate(config.install_phase)}</strong></span><span><small>{translate("Política de energia")}</small><strong>{config.power_sync_mode}</strong></span></div>
          : selected === 'GPU' ? <>
            <div className="detail-section-heading"><div><span className="section-kicker">{translate("REIMS VGPU")}</span><h2>{translate("Identidade da placa")}</h2></div><Monitor size={18}/></div>
            <div className="detail-facts"><span><small>{translate("Modo configurado")}</small><strong>{translate(config.gpu_mode || 'Padrão do runtime')}</strong></span><span><small>{translate("Placa configurada")}</small><strong>{translate(config.selected_gpu_name ?? config.selected_gpu ?? 'Padrão do runtime')}</strong></span><span><small>{translate("BDF efetivo")}</small><strong>{translate(runtime.data?.gpu?.effective_bdf ?? 'Não confirmado')}</strong></span><span><small>{translate("UUID efetivo")}</small><strong>{translate(runtime.data?.gpu?.effective_uuid ?? 'Não informado')}</strong></span><span><small>{translate("Verificação")}</small><strong>{translate(runtime.data?.gpu?.verification ?? 'Aguardando runtime')}</strong></span></div>
            <p className="detail-explanation">{translate("A seleção gravada e a GPU efetivamente aberta pelo Reims são mostradas separadamente. A alteração fica disponível quando o runtime puder validar e aplicar a troca com segurança.")}</p>
            {runtime.data?.gpu?.message && <p className="inline-message">{translate(runtime.data.gpu.message)}</p>}
            <DataState data={runtime.data} error={runtime.error} loading={runtime.loading} updatedAt={runtime.updatedAt}/>
          </> : selected === 'Discos' ? <>
            <div className="detail-section-heading"><div><span className="section-kicker">{translate("ARMAZENAMENTO DA VM")}</span><h2>{translate("Disco principal")}</h2></div><HardDrive size={18}/></div>
            <DataState data={storage.data} error={storage.error} loading={storage.loading} updatedAt={storage.updatedAt} staleAfter={45000}/>
            <div className="detail-facts"><span><small>{translate("Arquivo")}</small><strong>{config.disk_path || 'Ainda não provisionado'}</strong></span><span><small>{translate("Tamanho virtual")}</small><strong>{formatBytes(storageItem?.virtual_size_bytes ?? config.disk_size_gb * 1024 ** 3)}</strong></span><span><small>{translate("Espaço ocupado")}</small><strong>{storageItem?.allocated_bytes == null ? translate("Não medido") : formatBytes(storageItem.allocated_bytes)}</strong></span><span><small>{translate("Disponibilidade")}</small><strong>{storageItem ? storageItem.availability === 'available' ? translate("Disponível") : storageItem.availability === 'missing' ? translate("Arquivo ausente") : translate("Caminho externo") : config.disk_path ? translate("Sem inventário") : translate("Pendente")}</strong></span><span><small>{translate("Referências")}</small><strong>{storageItem ? `${storageItem.references.length} VM(s)` : config.disk_path ? translate("Não inspecionadas") : '—'}</strong></span></div>
            <div className="detail-section-heading detail-backup-heading"><div><span className="section-kicker">{translate("PROTEÇÃO DE DADOS")}</span><h2>{translate("Backups e pontos de restauração")}</h2></div><Database size={18}/></div>
            <p className="detail-explanation">{translate("Um ponto de restauração é uma cópia completa e independente, criada com a VM parada. Inclui disco, OpenCore, EFI/NVRAM, configuração e manifesto SHA-256; não é incremental e pode usar espaço e tempo semelhantes a um backup. Discos externos, instaladores vinculados e QCOW2 com backing chain são recusados.")}</p>
            <div className="button-row"><button className="button button-secondary" type="button" disabled={runtime.data?.state !== 'stopped' || clonePending} onClick={() => { setCloneId(''); setCloneName(`${config.name} cópia`); setCloneConfirmation(''); setCloneDialogOpen(true); }} title={runtime.data?.state !== 'stopped' ? 'Pare a VM antes de clonar' : undefined}><Layers3 size={16}/>{translate("Clonar VM")}</button>
            <button className="button button-primary" type="button" disabled={!config.disk_path || runtime.data?.state !== 'stopped' || backupPending} onClick={() => setBackupConfirmOpen(true)} title={runtime.data?.state !== 'stopped' ? translate("Pare a VM antes de criar o ponto de restauração") : undefined}><Database size={16}/>{backupPending ? 'Preparando…' : translate("Criar ponto de restauração")}</button>
            </div>
            {!config.disk_path && <p className="inline-message">{translate("Esta VM ainda não possui disco provisionado para incluir no backup.")}</p>}
            {runtime.data && runtime.data.state !== 'stopped' && <p className="inline-message">{translate("Desligue a VM e aguarde o estado “Parada” antes de iniciar o backup.")}</p>}
            <section className="storage-uploads-panel backup-inventory-panel"><div className="panel-heading"><div><span className="section-kicker">{translate("ARQUIVOS DE BACKUP")}</span><h2>{translate("Cópias desta VM")}</h2></div><Database size={18}/></div><DataState data={backups.data} error={backups.error} loading={backups.loading} updatedAt={backups.updatedAt} staleAfter={45000}/>
              {backups.data?.items.length ? <ResponsiveList className="storage-inventory-list" label={translate("Backups desta máquina virtual")}>{backups.data.items.map((backup) => <li className="panel storage-item" key={backup.backup_id}><div className="storage-item-heading"><span className="gpu-icon"><Database size={18}/></span><div><strong>{formatTimestamp(backup.created_at)}</strong><small>{backup.backup_id}</small></div><StatusBadge className={`vm-state ${backup.metadata_valid ? 'state-running' : 'state-unknown'}`}>{backup.metadata_valid ? 'Metadados conferem' : translate("Backup inválido")}</StatusBadge></div><dl className="storage-item-facts"><div><dt>{translate("Tamanho")}</dt><dd>{formatBytes(backup.size_bytes)}</dd></div><div><dt>{translate("Arquivos")}</dt><dd>{backup.file_count}</dd></div></dl><p className="detail-explanation">{backup.integrity_note}</p>{backup.metadata_valid && <div className="button-row"><button className="button button-secondary" type="button" disabled={verifyingBackupId !== null || restoringBackupId !== null} onClick={() => void requestBackupVerification(backup.backup_id)}><ShieldCheck size={16}/>{verifyingBackupId === backup.backup_id ? translate("Enviando verificação…") : 'Verificar hashes e discos'}</button><button className="button button-danger" type="button" disabled={runtime.data?.state !== 'stopped' || restoringBackupId !== null || verifyingBackupId !== null} onClick={() => { setRestoreBackupId(backup.backup_id); setRestoreConfirmation(''); }}><Database size={16}/>{restoringBackupId === backup.backup_id ? 'Restaurando…' : 'Restaurar sobre esta VM'}</button></div>}</li>)}</ResponsiveList> : backups.data ? <EmptyState icon={Database} title={translate("Nenhum backup encontrado")} body={translate("Quando o job offline terminar, a cópia aparecerá aqui.")}/> : null}
            </section>
          </> : selected === 'Rede' ? <>
            <div className="detail-section-heading"><div><span className="section-kicker">{translate("INTERFACE VIRTUAL")}</span><h2>{translate("Rede")}</h2></div><Network size={18}/></div>
            <div className="detail-facts"><span><small>{translate("Estado")}</small><strong>{config.extra_config.network_enabled === false ? translate("Desativada na configuração") : 'Habilitada pelo runtime'}</strong></span><span><small>{translate("Modo de conexão")}</small><strong>{typeof config.extra_config.network_mode === 'string' ? config.extra_config.network_mode : 'Gerenciado pelo Reims'}</strong></span><span><small>{translate("Endereço MAC")}</small><strong>{typeof config.extra_config.mac_address === 'string' ? config.extra_config.mac_address : 'Gerenciado pelo appliance'}</strong></span><span><small>{translate("Redirecionamentos")}</small><strong>{Array.isArray(config.extra_config.port_forwards) ? config.extra_config.port_forwards.length : 'Nenhum registrado'}</strong></span></div>
            <p className="detail-explanation">{translate("O painel ainda não altera bridge, TAP ou encaminhamento de portas. A configuração de rede permanece sob controle do runtime Reims.")}</p>
          </> : selected === 'Console' ? <>
            <div className="detail-section-heading"><div><span className="section-kicker">{translate("DISPLAY LOCAL")}</span><h2>{translate("Acesso à tela")}</h2></div><Monitor size={18}/></div>
            <div className="detail-local-display" role="status"><span className="detail-local-display-icon"><Monitor size={21} aria-hidden="true"/></span><div><strong>{translate("A tela é exibida no host")}</strong><p>{translate("O Reims vGPU abre a janela gráfica na sessão local do appliance. O painel web ainda não transmite o desktop da VM.")}</p></div></div>
            <div className="detail-facts"><span><small>{translate("Janela em tela cheia")}</small><strong>{config.fullscreen ? 'Ativada' : 'Desativada'}</strong></span><span><small>{translate("Seletor OpenCore")}</small><strong>{config.opencore_show_picker ? 'Ativado' : 'Desativado'}</strong></span><span><small>{translate("Estado da VM")}</small><strong>{runtime.data ? translate(runtimeLabels[runtime.data.state]) : translate("Indisponível")}</strong></span></div>
            <section className="panel vm-serial-panel" aria-labelledby="vm-serial-heading"><div className="panel-heading"><div><span className="section-kicker">{translate("SAÍDA DO CONVIDADO")}</span><h3 id="vm-serial-heading">{translate("Console serial")}</h3></div><button className="button button-secondary" type="button" onClick={() => void serialConsole.refresh()}>{translate("Atualizar")}</button></div>{serialConsole.data?.available ? <><p className="detail-explanation">{translate("Última atualização:")}{' '}{serialConsole.data.updated_at ? formatTimestamp(serialConsole.data.updated_at) : translate("indisponível")}{serialConsole.data.truncated ? translate(" · exibindo apenas o trecho mais recente (máximo 64 KiB)") : ''}</p><pre className="vm-serial-output" aria-live="polite">{serialConsole.data.content || 'O arquivo existe, mas ainda não contém saída.'}</pre></> : <p className="detail-explanation" role="status">{serialConsole.data?.message ?? (serialConsole.loading ? translate("Consultando saída serial do Reims…") : serialConsole.error instanceof Error ? serialConsole.error.message : serialConsole.error ?? 'A saída serial ainda não está disponível.')}</p>}</section>
          </> : selected === 'Opções' ? <>
            <div className="detail-section-heading"><div><span className="section-kicker">{translate("PARÂMETROS PERSISTIDOS")}</span><h2>{translate("Opções da máquina")}</h2></div><Settings2 size={18}/></div>
            <div className="detail-facts"><span><small>{translate("vCPUs")}</small><strong>{config.vcpus}</strong></span><span><small>{translate("Memória")}</small><strong>{config.ram_gb}{' '}{translate("GiB")}</strong></span><span><small>{translate("Modelo de áudio no próximo boot")}</small><strong>{audioModelLabel}</strong></span><span><small>{translate("Backend de áudio do host")}</small><strong>{config.audio_backend === 'auto' ? translate("Automático · Reims escolhe o backend") : config.audio_backend}</strong></span><span><small>{translate("Inicialização automática")}</small><strong>{config.auto_start ? 'Ativada' : 'Desativada'}</strong></span><span><small>{translate("Política de energia")}</small><strong>{config.power_sync_mode}</strong></span><span><small>{translate("Dispositivos USB automáticos")}</small><strong>{config.usb_auto_attach.length ? config.usb_auto_attach.join(', ') : 'Nenhum'}</strong></span><span><small>{translate("Argumentos extras QEMU")}</small><strong>{config.qemu_extra_args.length ? `${config.qemu_extra_args.length} token(s), aplicados no próximo boot` : 'Nenhum'}</strong></span></div>
            <div className="button-row"><button className="button button-secondary" type="button" disabled={!pickerCapability?.available || runtime.data?.state !== 'stopped' || pickerPending} onClick={() => { setPickerDesired(!config.opencore_show_picker); setPickerConfirmation(''); setPickerDialogOpen(true); }} title={pickerCapability?.limitation ?? (runtime.data?.state !== 'stopped' ? 'Pare a VM antes de regenerar o OpenCore' : undefined)}>{translate("Alterar seletor OpenCore")}</button><span className="detail-explanation">{translate("Estado atual:")}{' '}{config.opencore_show_picker ? translate("visível ao iniciar") : 'oculto'}{!pickerCapability?.available && pickerCapability?.limitation ? ` · ${pickerCapability.limitation}` : ''}</span></div>
            <div className="button-row"><button className="button button-secondary" type="button" disabled={!smbiosDiagnosticCapability?.available || runtime.data?.state !== 'stopped' || smbiosDiagnosticPending} onClick={() => void diagnoseSmbios()} title={smbiosDiagnosticCapability?.limitation ?? (runtime.data?.state !== 'stopped' ? 'Pare a VM antes de inspecionar a EFI' : undefined)}>{smbiosDiagnosticPending ? translate("Preparando diagnóstico…") : 'Verificar identidade SMBIOS'}</button><span className="detail-explanation">{translate("A verificação não mostra números de série e não testa ativação Apple.")}{!smbiosDiagnosticCapability?.available && smbiosDiagnosticCapability?.limitation ? ` · ${smbiosDiagnosticCapability.limitation}` : ''}</span></div>
            <p className="detail-explanation">{translate("Edição SMBIOS indisponível: o Reims atual conserva a identidade da imagem e não oferece aplicação estruturada de novos valores.")}</p>
            {config.qemu_extra_args.length > 0 && <pre className="detail-token-list" aria-label={translate("Argumentos QEMU registrados")}>{config.qemu_extra_args.join('\n')}</pre>}
            <p className="detail-explanation">{translate("A configuração exibida vem do catálogo. Campos ainda não consumidos pelo runtime são informativos e não alteram o processo QEMU.")}</p>
          </> : <div className="detail-facts"><span><small>{translate("vCPU")}</small><strong>{config.vcpus}</strong></span><span><small>{translate("Memória")}</small><strong>{config.ram_gb}{' '}{translate("GiB")}</strong></span><span><small>{translate("Disco configurado")}</small><strong>{config.disk_size_gb}{' '}{translate("GiB")}</strong></span><span><small>{translate("Fase registrada")}</small><strong>{translate(config.install_phase)}</strong></span><span><small>{translate("Política de energia")}</small><strong>{config.power_sync_mode}</strong></span></div>}
      </section>
    </div>}
    {catalog.data && !config && <EmptyState icon={Monitor} title={translate("VM não encontrada")} body={translate("O identificador da rota não aparece no catálogo atual.")}/>}
    <ConfirmationDialog open={confirmAction !== null} title={confirmAction === 'shutdown' ? 'Solicitar desligamento do macOS?' : confirmAction === 'restart' ? translate("Reiniciar a máquina virtual?") : translate("Forçar parada da máquina virtual?")} description={confirmAction === 'shutdown' ? `O painel enviará um pedido ACPI para ${config?.name ?? 'esta VM'} pelo QMP. Se o macOS não desligar, a tarefa informará o tempo esgotado sem forçar a parada.` : confirmAction === 'restart' ? translate("A VM será desligada por ACPI e só será iniciada novamente quando o processo atual encerrar.") : `O comando QMP quit encerra o QEMU sem desligamento do macOS. Digite exatamente “${config?.id ?? ''}” para confirmar.`} confirmLabel={confirmAction === 'shutdown' ? 'Enviar pedido ACPI' : confirmAction === 'restart' ? 'Reiniciar VM' : translate("Forçar parada")} confirmClassName={confirmAction === 'force_stop' ? 'button-danger' : 'button-primary'} busy={lifecyclePending} confirmDisabled={confirmAction === 'force_stop' && forceStopConfirmation !== config?.id} onClose={() => { setConfirmAction(null); setForceStopConfirmation(''); }} onConfirm={() => { if (confirmAction) void requestLifecycleAction(confirmAction); }}>{confirmAction === 'force_stop' && <label className="ui-field vm-field"><span className="ui-field-label vm-field-label">{translate("Identificador da VM")}</span><input autoComplete="off" value={forceStopConfirmation} onChange={(event) => setForceStopConfirmation(event.target.value)} aria-label={translate("Digite o identificador exato da VM")} /></label>}</ConfirmationDialog>
    <ConfirmationDialog open={installConfirmOpen} title={translate("Confirmar instalação do macOS?")} description={`Esta ação marca ${config?.name ?? 'esta VM'} como instalada no catálogo. Confirme somente depois de concluir a instalação e verificar que o macOS inicia; a VM precisa estar parada.`} confirmLabel="Marcar como instalada" busy={installConfirmPending} confirmDisabled={!vmId || installConfirmation !== vmId || runtime.data?.state !== 'stopped'} onClose={() => { if (!installConfirmPending) { setInstallConfirmOpen(false); setInstallConfirmation(''); } }} onConfirm={() => void confirmInstallation()}>{config && <label className="ui-field vm-field"><span className="ui-field-label vm-field-label">{translate("Digite o identificador exato:")}{' '}{config.id}</span><input autoComplete="off" value={installConfirmation} onChange={(event) => setInstallConfirmation(event.target.value)} aria-label={translate("Confirme o identificador exato da VM")} /></label>}</ConfirmationDialog>
    <ConfirmationDialog open={pickerDialogOpen} title={translate("Regenerar a imagem OpenCore?")} description={`O Reims criará uma nova imagem OpenCore para ${config?.name ?? 'esta VM'}, preservando a identidade SMBIOS lida da imagem atual e as variáveis UEFI. Outras personalizações feitas diretamente na imagem OpenCore não são copiadas. A VM precisa estar parada.`} confirmLabel="Enfileirar regeneração" busy={pickerPending} confirmDisabled={!pickerCapability?.available || !vmId || pickerConfirmation !== vmId || runtime.data?.state !== 'stopped' || config?.opencore_show_picker === pickerDesired} onClose={() => { if (!pickerPending) { setPickerDialogOpen(false); setPickerConfirmation(''); } }} onConfirm={() => void updateOpenCorePicker()}>{config && <div className="vm-editor-grid"><label className="ui-field vm-field"><span className="ui-field-label vm-field-label">{translate("Seletor OpenCore")}</span><select value={pickerDesired ? 'visible' : 'hidden'} onChange={(event) => setPickerDesired(event.target.value === 'visible')}><option value="visible">{translate("Visível ao iniciar")}</option><option value="hidden">{translate("Iniciar macOS automaticamente")}</option></select></label><label className="ui-field vm-field"><span className="ui-field-label vm-field-label">{translate("Digite o identificador exato:")}{' '}{config.id}</span><input autoComplete="off" value={pickerConfirmation} onChange={(event) => setPickerConfirmation(event.target.value)} aria-label={translate("Confirme o identificador exato da VM para regenerar o OpenCore")} /></label></div>}</ConfirmationDialog>
    <ConfirmationDialog open={backupConfirmOpen} title={translate("Criar ponto de restauração offline?")} description={`Será criada uma cópia completa e independente de ${config?.name ?? 'esta VM'}, incluindo discos e estado EFI/NVRAM. Ela pode usar espaço e tempo semelhantes a um backup. A operação é recusada se não for possível confirmar que a VM está parada.`} confirmLabel="Criar cópia completa" busy={backupPending} onClose={() => setBackupConfirmOpen(false)} onConfirm={() => void requestBackup()}/>
    <ConfirmationDialog open={restoreBackupId !== null} title={translate("Substituir os arquivos atuais da VM?")} description={`O backup ${restoreBackupId ?? ''} será restaurado sobre ${config?.name ?? 'esta VM'}. A VM precisa estar parada; a integridade será verificada novamente e os arquivos atuais serão mantidos até o catálogo confirmar a troca. Esta ação substitui o disco, OpenCore, EFI/NVRAM e configuração atual.`} confirmLabel="Restaurar backup" confirmClassName="button-danger" busy={restoringBackupId !== null} confirmDisabled={restoreConfirmation !== vmId || runtime.data?.state !== 'stopped'} onClose={() => { setRestoreBackupId(null); setRestoreConfirmation(''); }} onConfirm={() => void requestBackupRestore()}>{restoreBackupId && <label className="ui-field vm-field"><span className="ui-field-label vm-field-label">{translate("Digite o identificador da VM para confirmar:")}{' '}{vmId}</span><input autoComplete="off" value={restoreConfirmation} onChange={(event) => setRestoreConfirmation(event.target.value)} aria-label={translate("Digite o identificador da VM para confirmar a restauração")} /></label>}</ConfirmationDialog>
    <ConfirmationDialog open={cloneDialogOpen} title={translate("Criar clone independente?")} description={`Será criada uma cópia offline de ${config?.name ?? 'esta VM'} com novo identificador, MAC, SMBIOS e NVRAM limpa. A VM original não será alterada. O OpenCore precisa ser acessível para atualizar e verificar a identidade do clone.`} confirmLabel="Clonar VM" busy={clonePending} confirmDisabled={runtime.data?.state !== 'stopped' || !/^[a-z0-9][a-z0-9-]{0,62}$/.test(cloneId) || cloneName.trim().length < 1 || cloneConfirmation !== cloneId} onClose={() => { setCloneDialogOpen(false); setCloneConfirmation(''); }} onConfirm={() => void requestClone()}>
      <div className="vm-editor-grid"><label className="ui-field vm-field"><span className="ui-field-label vm-field-label">{translate("Novo identificador")}</span><input autoComplete="off" value={cloneId} onChange={(event) => setCloneId(event.target.value.toLowerCase())} aria-label={translate("Novo identificador da VM clonada")} /></label><label className="ui-field vm-field"><span className="ui-field-label vm-field-label">{translate("Nome do clone")}</span><input maxLength={80} value={cloneName} onChange={(event) => setCloneName(event.target.value)} aria-label={translate("Nome da VM clonada")} /></label><label className="ui-field vm-field"><span className="ui-field-label vm-field-label">{translate("Digite o novo identificador para confirmar")}</span><input autoComplete="off" value={cloneConfirmation} onChange={(event) => setCloneConfirmation(event.target.value)} aria-label={translate("Confirme o identificador da VM clonada")} /></label></div>
    </ConfirmationDialog>
  </>;
}

const taskStateLabels: Record<JobView['state'], string> = {
  queued: 'Na fila', running: 'Em execução', succeeded: 'Concluída', failed: 'Falhou',
  cancelling: 'Cancelando', cancelled: 'Cancelada', interrupted: 'Interrompida',
};

function jobKindLabel(kind: string): string {
  return translate(({ 'vm.provision': 'Criar máquina virtual', 'vm.start': 'Iniciar máquina virtual', 'vm.shutdown': 'Desligar máquina virtual', 'vm.force_stop': 'Parada forçada da máquina virtual', 'vm.restart': 'Reiniciar máquina virtual', 'vm.opencore.picker': 'Regenerar OpenCore', 'vm.smbios.diagnose': 'Verificar identidade SMBIOS', 'vm.backup.restore': 'Restaurar backup de máquina virtual', 'vm.clone': 'Clonar máquina virtual', 'media.import': 'Importar mídia', 'media.convert': 'Converter mídia', 'media.apple.download': 'Baixar Recovery Apple' } as Record<string, string>)[kind] ?? kind);
}

type TaskFilter = 'all' | 'active' | 'succeeded' | 'attention';
const taskFilters: Array<{ id: TaskFilter; label: string; states?: JobView['state'][] }> = [
  { id: 'all', label: 'Todas' },
  { id: 'active', label: 'Ativas', states: ['queued', 'running', 'cancelling'] },
  { id: 'succeeded', label: 'Concluídas', states: ['succeeded'] },
  { id: 'attention', label: 'Falhas / canceladas', states: ['failed', 'cancelled', 'interrupted'] },
];

function TaskDetail({ jobId }: { jobId: string }) {
  const detail = usePolling<JobView>(`/jobs/${encodeURIComponent(jobId)}`, 3000);
  const [canceling, setCanceling] = useState(false);
  const [retrying, setRetrying] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const cancel = async () => {
    if (!detail.data?.cancellable || canceling) return;
    setActionError(null);
    setCanceling(true);
    try { await api.request<JobView>(`/jobs/${encodeURIComponent(jobId)}/cancel`, { method: 'POST' }); await detail.refresh(); }
    catch (error) { setActionError(error instanceof Error ? translate(error.message) : translate("Não foi possível cancelar a tarefa.")); }
    finally { setCanceling(false); }
  };
  const retry = async () => {
    if (!detail.data?.retryable || retrying) return;
    setActionError(null);
    setRetrying(true);
    try { await api.request<JobAccepted>(`/jobs/${encodeURIComponent(jobId)}/retry`, { method: 'POST' }); await detail.refresh(); }
    catch (error) { setActionError(error instanceof Error ? translate(error.message) : translate("Não foi possível repetir a tarefa.")); }
    finally { setRetrying(false); }
  };
  const checkpoints = detail.data?.checkpoints ?? [];
  const logs = detail.data?.logs ?? [];
  return <section id="task-inspector" className="panel task-detail" aria-label={translate("Detalhes da tarefa")}>
    <div className="task-detail-heading"><div><span className="section-kicker">{translate("DETALHES DA OPERAÇÃO")}</span><h2>{detail.data ? jobKindLabel(detail.data.kind) : 'Tarefa'}</h2></div><div className="task-detail-actions"><button className="icon-button" type="button" aria-label={translate("Atualizar tarefa")} title={translate("Atualizar agora")} onClick={() => void detail.refresh()}><RefreshCw size={16}/></button>{detail.data?.retryable && <button className="button button-secondary" type="button" disabled={retrying} onClick={retry}>{retrying ? translate("Enviando…") : translate("Tentar novamente")}</button>}{detail.data?.cancellable && <button className="button button-secondary" type="button" disabled={canceling} onClick={cancel}>{canceling ? 'Cancelando…' : 'Cancelar tarefa'}</button>}</div></div>
    <DataState data={detail.data} error={detail.error} loading={detail.loading} updatedAt={detail.updatedAt} staleAfter={10000}/>
    {detail.data && <>
      <div className="task-detail-summary"><StatusBadge className={`vm-state task-state-${detail.data.state}`}>{translate(taskStateLabels[detail.data.state])}</StatusBadge><strong>{translate(detail.data.stage || 'Etapa não informada')}</strong><span>{detail.data.progress_percent == null ? translate("Progresso não informado") : `${Math.round(detail.data.progress_percent)}%`}</span><time dateTime={detail.data.updated_at}>{formatTimestamp(detail.data.updated_at)}</time></div>
      <ProgressBar value={detail.data.progress_percent} label="Progresso da tarefa" className="vm-job-progress"/>
      {detail.data.error?.message && <div className="inline-message error-message" role="alert">{translate(detail.data.error.message)}</div>}
      {actionError && <div className="inline-message error-message" role="alert">{actionError}</div>}
      <div className="task-detail-grid">
        <section><div className="task-section-heading"><h3>{translate("Etapas")}</h3><span>{checkpoints.length}</span></div>{checkpoints.length ? <ol className="task-checkpoints">{checkpoints.slice(-30).map((checkpoint, index) => <li key={`${checkpoint.name}-${checkpoint.updated_at}-${index}`} className={checkpoint.completed ? 'checkpoint-complete' : ''}><span className="checkpoint-mark"/><div><strong>{translate(checkpoint.name)}</strong><small>{formatTimestamp(checkpoint.updated_at)}</small>{typeof checkpoint.payload.message === 'string' && <p>{translate(checkpoint.payload.message)}</p>}</div></li>)}</ol> : <p className="task-empty-copy">{translate("O agente ainda não registrou etapas para esta tarefa.")}</p>}</section>
        <section><div className="task-section-heading"><h3>{translate("Registro")}</h3><span>{logs.length > 30 ? `30 ${translate('mais recentes')}` : logs.length}</span></div>{logs.length ? <ol className="task-logs">{logs.slice(-30).reverse().map((entry) => <li key={entry.id}><time dateTime={entry.created_at}>{new Date(entry.created_at).toLocaleTimeString(appLocale, { hour: '2-digit', minute: '2-digit', second: '2-digit' })}</time><span className={`log-level log-${entry.level}`}>{entry.level}</span><p>{translate(entry.message)}</p></li>)}</ol> : <p className="task-empty-copy">{translate("Nenhuma mensagem foi registrada.")}</p>}</section>
      </div>
    </>}
  </section>;
}

function TasksPage() {
  const [filter, setFilter] = useState<TaskFilter>('all');
  const [offset, setOffset] = useState(0);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const currentFilter = taskFilters.find((item) => item.id === filter) ?? taskFilters[0];
  const params = new URLSearchParams({ limit: '50', offset: String(offset) });
  currentFilter.states?.forEach((state) => params.append('state', state));
  const tasks = usePolling<JobListResponse>(`/jobs?${params.toString()}`, 5000);
  useEffect(() => { setOffset(0); setSelectedId(null); }, [filter]);
  const items = tasks.data?.items ?? [];
  const start = tasks.data ? Math.min(tasks.data.total, tasks.data.offset + 1) : 0;
  const end = tasks.data ? Math.min(tasks.data.total, tasks.data.offset + items.length) : 0;
  return <>
    <PageHeading eyebrow="OPERAÇÕES DO APPLIANCE" title={translate("Tarefas")} description="Acompanhe provisionamentos e outras operações executadas pelo appliance."/>
    <DataState data={tasks.data} error={tasks.error} loading={tasks.loading} updatedAt={tasks.updatedAt} staleAfter={12000}/>
    <div className="tasks-toolbar"><div className="task-filters" role="group" aria-label={translate("Filtrar tarefas")}>{taskFilters.map((item) => <button key={item.id} type="button" aria-pressed={filter === item.id} className={`filter-tab ${filter === item.id ? 'selected' : ''}`} onClick={() => setFilter(item.id)}>{translate(item.label)}</button>)}</div><span className="inventory-count">{tasks.data ? `${start}–${end} de ${tasks.data.total}` : tasks.loading ? 'Consultando tarefas…' : translate("Lista indisponível")}</span></div>
    <div className="tasks-layout">
      <section className="panel tasks-list-panel" aria-label={translate("Lista de tarefas")}>
        {items.length ? <ul className="task-list" aria-label={translate("Tarefas disponíveis")}>{items.map((task) => <li key={task.id}><button type="button" className={`task-row ${selectedId === task.id ? 'task-row-selected' : ''}`} onClick={() => setSelectedId(selectedId === task.id ? null : task.id)} aria-expanded={selectedId === task.id} aria-controls="task-inspector">
          <span className={`task-icon task-state-${task.state}`} aria-hidden="true"><Activity size={17}/></span><span className="task-row-main"><strong>{jobKindLabel(task.kind)}</strong><small>{task.target_vm_id ? `VM ${task.target_vm_id}` : `${translate('Tarefa')} ${task.id}`} · {formatTimestamp(task.updated_at)}</small>{task.progress_percent != null && <span className="task-row-progress" role="progressbar" aria-label={`${translate('Progresso da tarefa')} ${task.id}`} aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.max(0, Math.min(100, task.progress_percent))} aria-valuetext={`${Math.max(0, Math.min(100, task.progress_percent))}%`}><span style={{ width: `${Math.max(0, Math.min(100, task.progress_percent))}%` }}/></span>}</span><span className={`vm-state task-state-${task.state}`}>{translate(taskStateLabels[task.state])}</span><ArrowRight size={15} aria-hidden="true"/>
        </button></li>)}</ul> : <EmptyState icon={Clock3} title={tasks.data ? translate('Nenhuma tarefa neste filtro') : tasks.error ? translate("Tarefas indisponíveis") : translate("Aguardando tarefas")} body={tasks.error?.message ? translate(tasks.error.message) : (tasks.data ? translate("Não há operações correspondentes na página atual.") : translate("O histórico aparecerá quando o agente responder."))}/>}
        {tasks.data && tasks.data.total > 50 && <div className="task-pagination"><button className="button button-secondary" type="button" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 50))}>{translate("Mais recentes")}</button><button className="button button-secondary" type="button" disabled={offset + 50 >= tasks.data.total} onClick={() => setOffset(offset + 50)}>{translate("Anteriores")}</button></div>}
      </section>
      {selectedId ? <TaskDetail key={selectedId} jobId={selectedId}/> : <section id="task-inspector" className="panel task-inspector-empty" aria-label={translate("Detalhes da tarefa selecionada")}><EmptyState icon={Activity} title={translate("Selecione uma tarefa")} body={translate("O detalhe mostra etapas, progresso e mensagens registradas pelo agente.")}/></section>}
    </div>
  </>;
}

function PasswordChangeForm({ onComplete }: { onComplete?: () => Promise<void> } = {}) {
  const notify = useToast();
  const summaryRef = useRef<HTMLDivElement>(null);
  const [currentPassword, setCurrentPassword] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [confirmation, setConfirmation] = useState('');
  const [showPasswords, setShowPasswords] = useState(false);
  const [busy, setBusy] = useState(false);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [formError, setFormError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);

  const validate = () => {
    const next: Record<string, string> = {};
    if (!currentPassword) next.current = 'Informe sua senha atual.';
    if (newPassword.length < 12) next.next = 'Use pelo menos 12 caracteres.';
    else if (newPassword === currentPassword) next.next = 'A nova senha deve ser diferente da atual.';
    if (!confirmation) next.confirm = 'Confirme a nova senha.';
    else if (confirmation !== newPassword) next.confirm = 'As senhas não coincidem.';
    return next;
  };

  const validateField = (field: 'current' | 'next' | 'confirm') => {
    const all = validate();
    setFieldErrors((previous) => ({ ...previous, [field]: all[field] ?? '' }));
  };

  const submit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setSuccess(null);
    setFormError(null);
    const errors = validate();
    setFieldErrors(errors);
    if (Object.keys(errors).length) {
      setFormError('Revise os campos destacados antes de continuar.');
      window.requestAnimationFrame(() => summaryRef.current?.focus());
      return;
    }
    setBusy(true);
    try {
      await api.request<SessionView>('/session/password', {
        method: 'POST',
        body: JSON.stringify({ current_password: currentPassword, new_password: newPassword } satisfies PasswordChangeRequest),
      });
      setCurrentPassword('');
      setNewPassword('');
      setConfirmation('');
      setFieldErrors({});
      setSuccess(translate("Senha atualizada. As outras sessões foram encerradas."));
      notify('Senha administrativa atualizada.', 'success');
      await onComplete?.();
    } catch (error) {
      const message = error instanceof Error ? translate(error.message) : translate("Não foi possível atualizar a senha.");
      if (error instanceof ApiError && error.code === 'invalid_current_password') {
        setFieldErrors((previous) => ({ ...previous, current: message }));
      }
      setFormError(message);
      window.requestAnimationFrame(() => summaryRef.current?.focus());
    } finally {
      setBusy(false);
    }
  };

  return <article className="panel settings-card settings-password-card">
    <div className="settings-card-heading"><span className="settings-icon"><KeyRound size={18}/></span><div><span className="section-kicker">{translate("CONTA ADMINISTRATIVA")}</span><h2>{translate("Trocar senha")}</h2></div></div>
    <p className="settings-password-help">{translate("A senha protege o acesso local ao painel. Ao alterá-la, outras sessões abertas serão encerradas.")}</p>
    <form className="settings-password-form" onSubmit={(event) => void submit(event)} noValidate>
      {formError && <div className="settings-form-error" role="alert" tabIndex={-1} ref={summaryRef}><strong>{translate("Não foi possível salvar")}</strong><span>{formError}</span></div>}
      <div className="vm-field">
        <label htmlFor="settings-current-password">{translate("Senha atual")}</label>
        <input id="settings-current-password" type={showPasswords ? 'text' : 'password'} autoComplete="current-password" value={currentPassword} required aria-invalid={Boolean(fieldErrors.current)} aria-describedby={fieldErrors.current ? 'settings-current-error' : undefined} onChange={(event) => setCurrentPassword(event.target.value)} onBlur={() => validateField('current')} />
        {fieldErrors.current && <p className="field-error" id="settings-current-error">{fieldErrors.current}</p>}
      </div>
      <div className="vm-field">
        <label htmlFor="settings-new-password">{translate("Nova senha")}</label>
        <input id="settings-new-password" type={showPasswords ? 'text' : 'password'} autoComplete="new-password" minLength={12} value={newPassword} required aria-invalid={Boolean(fieldErrors.next)} aria-describedby={fieldErrors.next ? 'settings-new-hint settings-new-error' : 'settings-new-hint'} onChange={(event) => setNewPassword(event.target.value)} onBlur={() => validateField('next')} />
        <small className="vm-field-hint" id="settings-new-hint">{translate("Use pelo menos 12 caracteres. Gerenciadores de senha podem preencher este campo.")}</small>
        {fieldErrors.next && <p className="field-error" id="settings-new-error">{fieldErrors.next}</p>}
      </div>
      <div className="vm-field">
        <label htmlFor="settings-confirm-password">{translate("Confirme a nova senha")}</label>
        <input id="settings-confirm-password" type={showPasswords ? 'text' : 'password'} autoComplete="new-password" minLength={12} value={confirmation} required aria-invalid={Boolean(fieldErrors.confirm)} aria-describedby={fieldErrors.confirm ? 'settings-confirm-error' : undefined} onChange={(event) => setConfirmation(event.target.value)} onBlur={() => validateField('confirm')} />
        {fieldErrors.confirm && <p className="field-error" id="settings-confirm-error">{fieldErrors.confirm}</p>}
      </div>
      <div className="settings-password-actions">
        <button className="button button-secondary" type="button" aria-pressed={showPasswords} onClick={() => setShowPasswords((visible) => !visible)}>{showPasswords ? <EyeOff aria-hidden="true"/> : <Eye aria-hidden="true"/>}{showPasswords ? translate("Ocultar senhas") : translate("Mostrar senhas")}</button>
        <button className="button button-primary" type="submit" disabled={busy}>{busy ? translate("Atualizando…") : translate("Atualizar senha")}</button>
      </div>
      {success && <p className="settings-form-success" role="status">{success}</p>}
    </form>
  </article>;
}

function TelemetryRetentionSettings() {
  const notify = useToast();
  const settings = usePolling<TelemetrySettingsView>('/settings/telemetry', 30000);
  const [draft, setDraft] = useState<TelemetrySettingsView['history_retention_hours'] | null>(null);
  const [busy, setBusy] = useState(false);
  const current = settings.data?.history_retention_hours ?? 24;
  const selected = draft ?? current;
  const changed = draft !== null && draft !== current;

  const save = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!settings.data?.history_persistence_enabled || !changed) return;
    setBusy(true);
    try {
      const updated = await api.request<TelemetrySettingsView>('/settings/telemetry', {
        method: 'PUT', body: JSON.stringify({ history_retention_hours: selected }),
      });
      setDraft(null);
      await settings.refresh();
      notify(`Retenção do histórico ajustada para ${updated.history_retention_hours} h.`, 'success');
    } catch (error) {
      notify(error instanceof Error ? translate(error.message) : translate("Não foi possível atualizar a retenção."), 'error');
    } finally {
      setBusy(false);
    }
  };

  const enabled = settings.data?.history_persistence_enabled === true;
  return <article className="panel settings-card settings-retention-card">
    <div className="settings-card-heading"><span className="settings-icon"><Activity size={18}/></span><div><span className="section-kicker">{translate("HISTÓRICO DO HOST")}</span><h2>{translate("Retenção da telemetria")}</h2></div></div>
    <p className="settings-password-help">{translate("Escolha por quanto tempo os agregados de 1 minuto ficam no histórico persistente. O detalhe de 10 segundos continua limitado a 1 hora.")}</p>
    <form className="settings-retention-form" onSubmit={(event) => void save(event)}>
      <label htmlFor="telemetry-retention">{translate("Período de retenção")}</label>
      <select id="telemetry-retention" value={selected} disabled={!enabled || busy} onChange={(event) => setDraft(Number(event.target.value) as TelemetrySettingsView['history_retention_hours'])}>
        {[1, 6, 12, 24].map((hours) => <option key={hours} value={hours}>{hours} {hours === 1 ? 'hora' : 'horas'}</option>)}
      </select>
      {!settings.data && <p className="settings-card-foot">{translate(settings.error?.message ?? 'Consultando as configurações do agente…')}</p>}
      {settings.data && !enabled && <p className="settings-card-foot">{translate("O histórico persistente está desativado neste modo do appliance.")}</p>}
      {enabled && <p className="settings-card-foot">{translate("Atual:")}{' '}<strong>{current}{' '}{translate("h")}</strong>{' '}{translate("· os dados anteriores ao novo limite serão removidos.")}</p>}
      <button className="button button-primary" type="submit" disabled={!enabled || !changed || busy}>{busy ? translate("Salvando…") : translate("Salvar retenção")}</button>
    </form>
  </article>;
}

function ReimsReleaseSettings() {
  const notify = useToast();
  const navigate = useNavigate();
  // GitHub's unauthenticated API is rate limited; refresh on demand and poll
  // slowly while this page remains open.
  const releases = usePolling<ReimsReleaseList>('/settings/reims/releases', 300000);
  const [selectedTag, setSelectedTag] = useState('');
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (selectedTag || !releases.data?.items.length) return;
    setSelectedTag(releases.data.items[0].tag);
  }, [releases.data, selectedTag]);

  const selected = releases.data?.items.find((item) => item.tag === selectedTag) ?? null;
  const update = async () => {
    if (!selected) return;
    setBusy(true);
    try {
      await api.request<JobAccepted>('/settings/reims/releases', {
        method: 'POST',
        body: JSON.stringify({ tag: selected.tag, idempotency_key: crypto.randomUUID() }),
      });
      setConfirmOpen(false);
      notify(`Atualização do Reims ${selected.tag} adicionada às tarefas.`, 'success');
      navigate('/tasks');
    } catch (error) {
      notify(error instanceof Error ? translate(error.message) : translate("Não foi possível atualizar o runtime Reims."), 'error');
    } finally {
      setBusy(false);
    }
  };

  return <article className="panel settings-card settings-reims-card">
    <div className="settings-card-heading"><span className="settings-icon"><ArrowDownToLine size={18}/></span><div><span className="section-kicker">{translate("RUNTIME GRÁFICO")}</span><h2>{translate("Reims vGPU")}</h2></div><button className="icon-button settings-refresh" type="button" aria-label={translate("Atualizar lista de releases Reims")} title={translate("Atualizar lista")} disabled={releases.loading} onClick={() => void releases.refresh()}><RefreshCw size={16} className={releases.loading ? 'spin' : undefined}/></button></div>
    <p className="settings-password-help">{translate("Consulte as versões publicadas no fork e escolha quando atualizar o runtime empacotado neste appliance.")}</p>
    {releases.error && !releases.data && <div className="settings-empty" role="status">{translate(releases.error.message)}{' '}{translate("Verifique a conexão e tente atualizar a lista.")}</div>}
    {releases.data && <>
      <dl className="settings-status-list settings-reims-status"><div><dt>{translate("Fork")}</dt><dd>{releases.data.repository}</dd></div><div><dt>{translate("Árvore preparada")}</dt><dd>{releases.data.current_release ?? 'Fontes locais'}</dd></div><div><dt>{translate("QEMU ativo")}</dt><dd>{releases.data.active_release ?? 'Não identificado'}</dd></div></dl>
      {releases.error && <p className="settings-card-foot" role="status">{translate("A última lista carregada foi mantida.")}{' '}{translate(releases.error.message)}</p>}
      {releases.data.items.length ? <div className="settings-reims-form">
        <label htmlFor="reims-release-tag">{translate("Release disponível")}</label>
        <select id="reims-release-tag" value={selectedTag} onChange={(event) => setSelectedTag(event.target.value)}>
          {releases.data.items.map((release) => <option key={release.tag} value={release.tag}>{release.name} · {release.tag}{release.prerelease ? translate(" · pré-lançamento") : ''}</option>)}
        </select>
        {selected && <p className="settings-card-foot">{selected.published_at ? `Publicada em ${formatTimestamp(selected.published_at)} · ` : ''}{formatBytes(selected.asset_size_bytes)}{selected.tag === releases.data.current_release ? translate(" · já está na árvore local") : ''}</p>}
        <button className="button button-primary" type="button" disabled={!selected || busy || selected.tag === releases.data.current_release} onClick={() => setConfirmOpen(true)}>{translate("Preparar esta release")}</button>
      </div> : <div className="settings-empty">{translate("Nenhuma release compatível com o appliance foi encontrada.")}</div>}
      <p className="settings-card-foot">{translate("O download só acontece após sua confirmação. A troca exige a janela de manutenção livre e será ativada no próximo início de uma VM; não há download automático ao iniciar.")}</p>
    </>}
    {!releases.data && !releases.error && <div className="settings-empty">{translate("Consultando releases do fork…")}</div>}
    <ConfirmationDialog open={confirmOpen} title={`Preparar Reims ${selected?.tag ?? ''}?`} description={`O appliance baixará e validará a release selecionada de ${releases.data?.repository ?? 'seu fork'}. A atualização só prossegue quando a janela de manutenção estiver livre; o runtime novo será ativado no próximo início de uma VM.`} confirmLabel="Enfileirar atualização" busy={busy} confirmDisabled={!selected} onClose={() => setConfirmOpen(false)} onConfirm={() => void update()}/>
  </article>;
}

function SettingsPage() {
  const notify = useToast();
  const host = usePolling<HostOverview>('/host', 10000);
  const health = usePolling<HealthView>('/health', 10000);
  const session = usePolling<SessionView>('/session', 20000);
  const audit = usePolling<AuditListResponse>('/audit?limit=50', 15000);
  const capabilities = usePolling<CapabilitiesResponse>('/capabilities', 30000);
  const [copied, setCopied] = useState<string | null>(null);
  const fingerprint = host.data?.tls_fingerprint ?? session.data?.tls_fingerprint ?? null;
  const fingerprintDisplay = fingerprint?.match(/.{1,2}/g)?.join(':') ?? null;
  const copy = async (key: string, value: string) => {
    try { await navigator.clipboard.writeText(value); setCopied(key); window.setTimeout(() => setCopied(null), 1800); notify(translate("Copiado para a área de transferência."), 'success'); }
    catch { setCopied(null); notify(translate("Não foi possível copiar. Verifique a permissão do navegador."), 'error'); }
  };
  const addresses = host.data?.addresses ?? [];
  const urls = addresses.map((address) => ({
    address,
    url: `https://${address.includes(':') ? `[${address}]` : address}:${host.data?.web_port ?? 8443}`,
  }));
  const healthLabel = health.data?.status === 'ok' ? translate("Operacional")
    : health.data?.status === 'degraded' ? 'Degradada'
      : health.data?.status === 'unavailable' ? translate("Indisponível")
        : health.error ? translate("Sem resposta") : translate("Consultando");
  const expires = session.data?.expires_at ? formatTimestamp(session.data.expires_at) : translate("Não informado");
  return <>
    <PageHeading eyebrow="SISTEMA DO APPLIANCE" title={translate("Configurações")} description="Acesso, segurança e estado dos serviços do vHackintosh Control."/>
    <div className="settings-readonly-note" role="note"><ShieldCheck size={18}/><div><strong>{translate("Acesso administrativo")}</strong><p>{translate("Senha, retenção do histórico e release do Reims podem ser gerenciadas aqui. Rede e certificados continuam somente para consulta.")}</p></div></div>
    <DataState data={health.data} error={health.error} loading={health.loading} updatedAt={health.updatedAt} staleAfter={20000}/>
    <DataState data={host.data} error={host.error} loading={host.loading} updatedAt={host.updatedAt} staleAfter={20000}/>
    <section className="settings-card-grid" aria-label={translate("Estado e acesso do appliance")}>
      <article className="panel settings-card settings-access-card"><div className="settings-card-heading"><span className="settings-icon"><Globe2 size={18}/></span><div><span className="section-kicker">{translate("ACESSO AO PAINEL")}</span><h2>{translate("Endereços HTTPS")}</h2></div></div>
        {urls.length ? <ul className="settings-address-list">{urls.map(({ address, url }) => <li key={address}><div><strong>{address}</strong><a href={url} target="_blank" rel="noreferrer">{url}</a></div><button className="icon-button" type="button" aria-label={`Copiar endereço ${url}`} title={translate("Copiar endereço")} onClick={() => void copy(`url-${address}`, url)}>{copied === `url-${address}` ? <Check size={16}/> : <Copy size={16}/>}</button></li>)}</ul> : <div className="settings-empty">{host.error ? translate("Não foi possível consultar os endereços deste appliance.") : translate("Nenhum endereço de rede disponível no momento.")}</div>}
        <p className="settings-card-foot">{translate("Porta HTTPS")}{' '}<strong>{host.data?.web_port ?? '—'}</strong>{' '}{translate("· endereço IPv6 aparece entre colchetes para formar uma URL válida.")}</p>
      </article>
      <article className="panel settings-card"><div className="settings-card-heading"><span className="settings-icon"><ShieldCheck size={18}/></span><div><span className="section-kicker">{translate("TRANSPORTE SEGURO")}</span><h2>{translate("Certificado TLS")}</h2></div></div>
        {fingerprintDisplay ? <><div className="settings-fingerprint-label">{translate("Impressão digital SHA-256")}</div><code className="settings-fingerprint">{fingerprintDisplay}</code><button className="button button-secondary settings-copy-fingerprint" type="button" onClick={() => void copy('fingerprint', fingerprint ?? '')}>{copied === 'fingerprint' ? <Check size={15}/> : <Copy size={15}/>}{copied === 'fingerprint' ? translate("Copiado") : translate("Copiar impressão")}</button></> : <div className="settings-empty">{translate("A impressão digital não foi informada pelo serviço.")}</div>}
        <p className="settings-card-foot">{translate("Compare este SHA-256 com o certificado apresentado pelo painel antes do primeiro acesso.")}</p>
      </article>
      <article className="panel settings-card"><div className="settings-card-heading"><span className="settings-icon"><Activity size={18}/></span><div><span className="section-kicker">{translate("SERVIÇOS LOCAIS")}</span><h2>{translate("API e agente")}</h2></div></div>
        <dl className="settings-status-list"><div><dt>{translate("API web")}</dt><dd><span className={`status-dot ${health.data?.status === 'ok' ? 'status-live' : health.data?.status === 'degraded' ? 'status-stale' : health.error ? 'status-down' : 'status-unknown'}`}/>{healthLabel}</dd></div><div><dt>{translate("Versão da API")}</dt><dd>{health.data?.version ?? (health.error ? translate("Indisponível") : '—')}</dd></div><div><dt>{translate("Agente local")}</dt><dd><span className={`status-dot ${health.data?.agent_connected ? 'status-live' : health.data ? 'status-down' : 'status-unknown'}`}/>{health.data ? health.data.agent_connected ? translate("Conectado") : translate("Desconectado") : translate("Sem resposta")}</dd></div><div><dt>{translate("Versão do agente")}</dt><dd>{translate("Não exposta pela API atual")}</dd></div><div><dt>{translate("Verificado em")}</dt><dd>{health.data ? formatTimestamp(health.data.checked_at) : '—'}</dd></div></dl>
      </article>
      <article className="panel settings-card"><div className="settings-card-heading"><span className="settings-icon"><Cpu size={18}/></span><div><span className="section-kicker">{translate("RUNTIME SELECIONADO")}</span><h2>{translate("Reims e QEMU")}</h2></div></div>
        <dl className="settings-status-list"><div><dt>{translate("Reims")}</dt><dd>{capabilities.data?.runtime?.reims_version ?? 'Não confirmado'}</dd></div><div><dt>{translate("QEMU")}</dt><dd>{capabilities.data?.runtime?.qemu_version ?? 'Não confirmado'}</dd></div><div><dt>{translate("Inventário consultado")}</dt><dd>{capabilities.data?.runtime?.status === 'available' ? translate("Sim") : capabilities.data?.runtime?.status === 'partial' ? translate("Parcial") : capabilities.data?.runtime?.status === 'missing' ? translate("Indisponível") : translate("Consultando")}</dd></div><div><dt>{translate("Opções básicas")}</dt><dd>{capabilities.data?.runtime?.qemu_options.join(', ') || 'Indisponíveis'}</dd></div><div><dt>{translate("Dispositivos")}</dt><dd>{capabilities.data?.runtime?.qemu_devices.length ?? '—'}</dd></div><div><dt>{translate("Máquinas / CPUs")}</dt><dd>{capabilities.data?.runtime ? `${capabilities.data.runtime.qemu_machine_types.length} / ${capabilities.data.runtime.qemu_cpu_models.length}` : translate("Consultando")}</dd></div></dl>
        {!capabilities.data && <p className="settings-card-foot">{translate(capabilities.error?.message ?? 'Consultando os binários efetivamente selecionados pelo appliance…')}</p>}
        <p className="settings-card-foot">{translate("Fingerprint dos binários:")}{' '}<code>{capabilities.data?.runtime?.binary_fingerprint.slice(0, 16) ?? '—'}</code>{' '}{translate("· snapshot")}{' '}{capabilities.data?.runtime_fingerprint.slice(0, 16) ?? '—'}</p>
      </article>
      <article className="panel settings-card"><div className="settings-card-heading"><span className="settings-icon"><Activity size={18}/></span><div><span className="section-kicker">{translate("RECURSOS VERIFICADOS")}</span><h2>{translate("Capacidades da VM")}</h2></div></div>
        {capabilities.data?.items.length ? <ul className="settings-capability-list">{capabilities.data.items.map((item) => <li key={item.id}><div><strong>{translate(item.label)}</strong><small>{translate(item.limitation ?? item.description ?? 'Disponibilidade informada pelo agente.')}</small></div><span className={item.available ? 'capability-available' : 'capability-unavailable'}>{item.available ? translate("Disponível") : translate("Indisponível")}</span></li>)}</ul> : <div className="settings-empty">{translate(capabilities.error?.message ?? 'Aguardando a confirmação do inventário de capacidades.')}</div>}
      </article>
      <article className="panel settings-card"><div className="settings-card-heading"><span className="settings-icon"><Clock3 size={18}/></span><div><span className="section-kicker">{translate("SESSÃO ATUAL")}</span><h2>{translate("Acesso administrativo")}</h2></div></div>
        <dl className="settings-status-list"><div><dt>{translate("Usuário")}</dt><dd>{session.data?.username ?? 'Indisponível'}</dd></div><div><dt>{translate("Expira em")}</dt><dd>{expires}</dd></div><div><dt>{translate("Autenticação")}</dt><dd>{session.data?.authenticated ? translate("Sessão autenticada") : session.error ? translate("Estado indisponível") : translate("Sem sessão")}</dd></div></dl>
        <p className="settings-card-foot">{translate("Para encerrar a sessão, use a opção de sair no menu do perfil.")}</p>
      </article>
      <TelemetryRetentionSettings/>
      <ReimsReleaseSettings/>
    </section>
    <section className="settings-password-section" aria-label={translate("Segurança da conta")}><PasswordChangeForm/></section>
    <section className="panel audit-panel" aria-labelledby="audit-heading">
      <div className="panel-heading"><div><span className="section-kicker">{translate("RASTREABILIDADE")}</span><h2 id="audit-heading">{translate("Atividade administrativa recente")}</h2></div><ClipboardList size={18}/></div>
      <p className="audit-help">{translate("Ações, operador, alvo e resultado são registrados sem salvar senhas, cookies, corpos de solicitação ou dados de identidade SMBIOS.")}</p>
      <DataState data={audit.data} error={audit.error} loading={audit.loading} updatedAt={audit.updatedAt} staleAfter={30000}/>
      {audit.data?.items.length ? <ol className="audit-list">{audit.data.items.map((event) => <li key={`${event.request_id}-${event.action}`}>
        <span className={`audit-result ${event.outcome < 400 ? 'audit-success' : 'audit-failure'}`} aria-label={`Resultado HTTP ${event.outcome}`}>{event.outcome}</span>
        <span className="audit-entry-main"><strong>{event.action}</strong><small>{event.actor} · {event.target}</small><small>{translate("Operação")}{' '}{event.operation_id}{' '}{translate("· solicitação")}{' '}{event.request_id}</small></span>
        <time dateTime={event.created_at}>{formatTimestamp(event.created_at)}</time>
      </li>)}</ol> : audit.data ? <EmptyState icon={ClipboardList} title={translate("Sem ações registradas")} body={translate("As operações administrativas aparecerão aqui depois do primeiro uso.")}/> : null}
    </section>
  </>;
}

function ResourcePage({ page }: { page: 'GPU' | 'Armazenamento' }) {
  const gpu = usePolling<GpuInventoryResponse>(page === 'GPU' ? '/host/gpus' : null, 15000);
  const gpuMetrics = useEventStream<TelemetryResponse>(page === 'GPU' ? '/host/metrics/stream' : null);
  const storage = usePolling<StorageInventory>(page === 'Armazenamento' ? '/storage' : null, 15000);
  const transfers = usePolling<PrivateTransferListResponse>(page === 'Armazenamento' ? '/transfers' : null, 15000);
  const notify = useToast();
  const [transferFormat, setTransferFormat] = useState<'iso' | 'qcow2' | 'dmg'>('iso');
  const [transferFile, setTransferFile] = useState<File | null>(null);
  const [appleRecoveryVersion, setAppleRecoveryVersion] = useState<'tahoe' | 'sequoia' | 'ventura'>('sequoia');
  const [downloadingAppleRecovery, setDownloadingAppleRecovery] = useState(false);
  const [transferBusy, setTransferBusy] = useState(false);
  const [importingTransferId, setImportingTransferId] = useState<string | null>(null);
  const [convertingMediaId, setConvertingMediaId] = useState<string | null>(null);
  const config: Record<typeof page, { eyebrow: string; description: string; icon: typeof Monitor; title: string; body: string }> = {
    GPU: { eyebrow: translate("DISPOSITIVOS GRÁFICOS"), description: translate("Consulte compatibilidade e atribua a GPU às máquinas virtuais."), icon: Monitor, title: translate("Inventário de GPU indisponível"), body: translate("Placas, BDFs e estado de uso serão exibidos a partir dos dados reais do host.") },
    Armazenamento: { eyebrow: translate("DISCOS E MÍDIA"), description: translate("Acompanhe imagens referenciadas no catálogo e o espaço disponível no appliance."), icon: HardDrive, title: translate("Armazenamento não consultado"), body: translate("O painel não possui dados de disco enquanto o serviço local estiver desconectado.") },
  };
  const item = config[page];
  const uploadTransfer = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!transferFile) return;
    const maxBytes = transfers.data?.max_file_bytes;
    if (maxBytes != null && transferFile.size > maxBytes) {
      notify(`O arquivo excede o limite de ${formatBytes(maxBytes)}.`, 'error');
      return;
    }
    setTransferBusy(true);
    try {
      await api.request('/transfers', {
        method: 'POST',
        headers: { 'X-Transfer-Filename': transferFile.name, 'X-Transfer-Format': transferFormat, 'Content-Type': 'application/octet-stream' },
        body: transferFile,
      });
      setTransferFile(null);
      const input = document.getElementById('storage-transfer-file') as HTMLInputElement | null;
      if (input) input.value = '';
      await transfers.refresh();
      notify(translate("Arquivo validado e guardado na área temporária privada."), 'success');
    } catch (error) {
      notify(error instanceof Error ? translate(error.message) : translate("Não foi possível enviar o arquivo."), 'error');
    } finally {
      setTransferBusy(false);
    }
  };
  const deleteTransfer = async (transferId: string, filename: string) => {
    if (!window.confirm(`Excluir o upload temporário “${filename}”?`)) return;
    try {
      await api.request<void>(`/transfers/${encodeURIComponent(transferId)}`, { method: 'DELETE' });
      await transfers.refresh();
      notify(translate("Upload temporário excluído."), 'success');
    } catch (error) {
      notify(error instanceof Error ? translate(error.message) : translate("Não foi possível excluir o upload."), 'error');
    }
  };
  const importTransfer = async (transferId: string, filename: string) => {
    setImportingTransferId(transferId);
    try {
      const job = await api.request<{ job_id: string }>(`/transfers/${encodeURIComponent(transferId)}/import`, { method: 'POST' });
      notify(`Importação de “${filename}” adicionada às tarefas.`, 'success');
      await Promise.all([transfers.refresh(), storage.refresh()]);
      return job;
    } catch (error) {
      notify(error instanceof Error ? translate(error.message) : translate("Não foi possível importar a mídia."), 'error');
      return null;
    } finally {
      setImportingTransferId(null);
    }
  };
  const convertMedia = async (media: StorageInventory['items'][number], outputFormat: 'raw' | 'qcow2') => {
    setConvertingMediaId(media.id);
    try {
      const job = await api.request<JobAccepted>(`/storage/${encodeURIComponent(media.id)}/convert`, {
        method: 'POST', body: JSON.stringify({ output_format: outputFormat, idempotency_key: crypto.randomUUID() }),
      });
      notify(`Conversão de “${media.name}” adicionada às tarefas.`, 'success');
      await storage.refresh();
      return job;
    } catch (error) {
      notify(error instanceof Error ? translate(error.message) : translate("Não foi possível converter a mídia."), 'error');
      return null;
    } finally {
      setConvertingMediaId(null);
    }
  };
  const downloadAppleRecovery = async () => {
    setDownloadingAppleRecovery(true);
    try {
      const job = await api.request<JobAccepted>('/storage/apple-recovery', {
        method: 'POST', body: JSON.stringify({ macos_version: appleRecoveryVersion, idempotency_key: crypto.randomUUID() }),
      });
      notify(`Download do Recovery Apple adicionado às tarefas (${appleRecoveryVersion}).`, 'success');
      await storage.refresh();
      return job;
    } catch (error) {
      notify(error instanceof Error ? translate(error.message) : translate("Não foi possível solicitar o Recovery Apple."), 'error');
      return null;
    } finally {
      setDownloadingAppleRecovery(false);
    }
  };
  if (page === 'Armazenamento') return <>
    <PageHeading eyebrow={translate(item.eyebrow)} title={translate(page)} description={translate(item.description)}/>
    <DataState data={storage.data} error={storage.error} loading={storage.loading} updatedAt={storage.updatedAt} staleAfter={45000}/>
    <section className="panel storage-volume-panel"><div className="panel-heading"><div><span className="section-kicker">{translate("VOLUME GERENCIADO")}</span><h2>{storage.data?.managed_volume ?? 'Volume das VMs gerenciadas'}</h2></div><HardDrive size={18}/></div><div className="storage-free-space"><span>{translate("Espaço livre")}</span><strong>{storage.data?.free_bytes == null ? translate("Indisponível") : formatBytes(storage.data.free_bytes)}</strong></div></section>
    <section className="panel storage-upload-panel"><div className="panel-heading"><div><span className="section-kicker">{translate("TRANSFERÊNCIA PRIVADA")}</span><h2>{translate("Adicionar mídia")}</h2></div><Upload size={18}/></div>
      <div className="storage-upload-form"><label>{translate("Recovery oficial Apple")}<select value={appleRecoveryVersion} onChange={(event) => setAppleRecoveryVersion(event.target.value as typeof appleRecoveryVersion)}><option value="sequoia">{translate("macOS Sequoia 15")}</option><option value="tahoe">{translate("macOS Tahoe 26")}</option><option value="ventura">{translate("macOS Ventura 13")}</option></select></label><button className="button button-secondary" type="button" disabled={downloadingAppleRecovery} onClick={() => void downloadAppleRecovery()}>{downloadingAppleRecovery ? translate("Enfileirando download…") : translate("Baixar Recovery Apple")}</button></div>
      <p className="storage-transfer-help">{translate("Envie uma ISO, imagem QCOW2 ou DMG. O agente valida o formato e mantém o arquivo isolado até uma etapa explícita de importação.")}</p>
      <form className="storage-upload-form" onSubmit={(event) => void uploadTransfer(event)}>
        <label>{translate("Formato")}<select value={transferFormat} onChange={(event) => setTransferFormat(event.target.value as 'iso' | 'qcow2' | 'dmg')}><option value="iso">{translate("ISO")}</option><option value="qcow2">{translate("QCOW2")}</option><option value="dmg">{translate("DMG")}</option></select></label>
        <label>{translate("Arquivo")}<input id="storage-transfer-file" type="file" accept={`.${transferFormat}`} onChange={(event) => setTransferFile(event.target.files?.[0] ?? null)}/></label>
        <button type="submit" className="button button-primary" disabled={!transferFile || transferBusy}><Upload size={16}/>{transferBusy ? translate("Enviando…") : translate("Enviar e validar")}</button>
      </form>
      <p className="storage-transfer-quota">{translate("Limite por arquivo:")}{' '}{transfers.data ? formatBytes(transfers.data.max_file_bytes) : translate("consultando")}{' '}{translate("· reserva livre do volume:")}{' '}{transfers.data ? formatBytes(transfers.data.min_free_bytes) : translate("consultando")}</p>
    </section>
    <section className="panel storage-uploads-panel"><div className="panel-heading"><div><span className="section-kicker">{translate("ÁREA TEMPORÁRIA")}</span><h2>{translate("Uploads validados")}</h2></div><Database size={18}/></div><DataState data={transfers.data} error={transfers.error} loading={transfers.loading} updatedAt={transfers.updatedAt} staleAfter={45000}/>
      {transfers.data?.items.length ? <ResponsiveList className="storage-transfer-list" label={translate("Uploads temporários validados")}>{transfers.data.items.map((transfer) => <li className="storage-transfer-row" key={transfer.id}><span className="gpu-icon"><Database size={17}/></span><span className="storage-transfer-main"><strong>{transfer.filename}</strong><small>{transfer.format.toUpperCase()} · {formatBytes(transfer.size_bytes)}</small></span><button className="button button-secondary storage-transfer-import" type="button" disabled={importingTransferId !== null} onClick={() => void importTransfer(transfer.id, transfer.filename)}>{importingTransferId === transfer.id ? 'Enfileirando…' : translate("Importar na biblioteca")}</button><a className="button button-secondary storage-transfer-download" href={`/api/v1/transfers/${encodeURIComponent(transfer.id)}/content`}><ArrowDownToLine size={15}/>{translate("Baixar")}</a><button className="icon-button storage-transfer-delete" type="button" aria-label={`Excluir ${transfer.filename}`} title={translate("Excluir upload")} onClick={() => void deleteTransfer(transfer.id, transfer.filename)}><Trash2 size={17}/></button></li>)}</ResponsiveList> : transfers.data ? <EmptyState icon={Database} title={translate("Nenhum upload temporário")} body={translate("Arquivos enviados e validados aparecerão nesta lista até serem importados ou removidos.")}/> : null}
    </section>
    {storage.data?.items.length ? <ResponsiveList className="storage-inventory-list" label={translate("Discos e mídias do catálogo")}>{storage.data.items.map((disk) => {
      const suffix = disk.name.toLowerCase().split('.').pop();
      const convertible = disk.location === 'managed' && disk.availability === 'available' && disk.references.length === 0 && ['qcow2', 'img', 'raw'].includes(suffix ?? '');
      const outputFormat = suffix === 'qcow2' ? 'raw' : 'qcow2';
      return <li className="panel storage-item" key={disk.id}>
      <div className="storage-item-heading"><span className="gpu-icon"><Database size={18}/></span><div><strong>{disk.name}</strong><small>{disk.location === 'managed' ? translate("Pasta gerenciada") : translate("Caminho externo")}</small></div><StatusBadge className={`vm-state ${disk.availability === 'available' ? 'state-running' : 'state-unknown'}`}>{disk.availability === 'available' ? translate("Disponível") : disk.availability === 'missing' ? translate("Arquivo ausente") : translate("Não inspecionado")}</StatusBadge></div>
      <dl className="storage-item-facts"><div><dt>{translate("Tamanho virtual configurado")}</dt><dd>{disk.virtual_size_bytes == null ? translate("Indisponível") : formatBytes(disk.virtual_size_bytes)}</dd></div><div><dt>{translate("Espaço alocado")}</dt><dd>{disk.allocated_bytes == null ? translate("Não medido") : formatBytes(disk.allocated_bytes)}</dd></div><div><dt>{translate("Referências no catálogo")}</dt><dd>{disk.references.length ? disk.references.map((vmId) => <NavLink key={vmId} to={`/vms/${encodeURIComponent(vmId)}/summary`}>{vmId}</NavLink>) : translate("Nenhuma")}</dd></div></dl>
      {convertible && <div className="button-row"><button className="button button-secondary" type="button" disabled={convertingMediaId !== null} onClick={() => void convertMedia(disk, outputFormat)}>{convertingMediaId === disk.id ? translate("Enfileirando conversão…") : `Converter para ${outputFormat.toUpperCase()}`}</button></div>}
      {disk.location === 'external' && <p className="inline-message">{translate("O caminho está fora da pasta gerenciada; o agente não consultou o arquivo.")}</p>}
    </li>})}</ResponsiveList> : storage.data ? <div className="panel resource-panel"><EmptyState icon={HardDrive} title={translate("Nenhuma mídia gerenciada")} body={translate("Discos e mídias dentro da área gerenciada aparecerão aqui, mesmo sem vínculo com uma VM. Caminhos externos e uploads temporários ficam indicados em suas próprias seções.")}/></div> : <div className="panel resource-panel"><EmptyState icon={HardDrive} title={storage.error ? translate(item.title) : translate("Consultando armazenamento")} body={translate(storage.error?.message ?? item.body)}/></div>}
  </>;
  return <><PageHeading eyebrow={translate(item.eyebrow)} title={translate(page)} description={translate(item.description)}/><DataState data={gpu.data} error={gpu.error} loading={gpu.loading} updatedAt={gpu.updatedAt} staleAfter={45000}/>{gpu.data?.stale && <p className="inline-message" role="status">{translate(gpu.data.message ?? 'O inventário de GPU não foi atualizado.')}</p>}<section className="gpu-grid">{gpu.data?.items.length ? gpu.data.items.map((device) => {
    const metric = (name: string) => gpuMetrics.data?.samples.find((sample) => sample.name === name && sample.labels.pci_bdf === device.pci_bdf);
    const busy = metric('host.gpu.busy_percent');
    const used = metric('host.gpu.vram_used_bytes');
    const total = metric('host.gpu.vram_total_bytes');
    const vramUsedBytes = used?.availability === 'available' ? Number(used.value) : null;
    const memory = used?.availability === 'available' && total?.availability === 'available'
      ? `${formatBytes(Number(used.value))} / ${formatBytes(Number(total.value))}` : translate("Indisponível");
    return <article className="panel gpu-card" key={device.pci_bdf}><div className="gpu-card-heading"><span className="gpu-icon"><Monitor size={19}/></span><span className="vm-state state-unknown">{device.selection_supported ? translate("Seleção disponível") : translate("Somente inventário")}</span></div><h2>{device.name}</h2><p>{device.kind}</p><dl><div><dt>{translate("PCI BDF")}</dt><dd>{device.pci_bdf}</dd></div><div><dt>{translate("Vulkan")}</dt><dd>{device.vulkan_available ? translate("Disponível") : translate("Indisponível")}</dd></div><div><dt>{translate("Driver")}</dt><dd>{device.driver_file ?? 'Não informado'}</dd></div>{device.reims_device_uuid && <div><dt>{translate("UUID Reims")}</dt><dd className="mono-value">{device.reims_device_uuid}</dd></div>}{device.reims_shares_driver !== null && <div><dt>{translate("Driver compartilhado")}</dt><dd>{device.reims_shares_driver ? translate("Sim") : translate("Não")}</dd></div>}<div><dt>{translate("Uso pelo host")}</dt><dd>{device.used_by_host == null ? translate("Não informado") : device.used_by_host ? 'Em uso' : translate("Livre")}</dd></div><div><dt>{translate("VM atribuída")}</dt><dd>{device.assigned_vm_id ?? 'Nenhuma informada'}</dd></div>{device.vulkan_uuid && <div><dt>{translate("UUID Vulkan")}</dt><dd className="mono-value">{device.vulkan_uuid}</dd></div>}</dl>
      <div className="gpu-resource-metrics" aria-label={`Telemetria de ${device.name}`}><h3>{translate("Telemetria ao vivo")}</h3><dl>
        <div><dt>{translate("Uso do processador gráfico")}</dt><dd>{formatSample(busy, (value) => `${value.toLocaleString(appLocale, { maximumFractionDigits: 1 })}%`)}</dd></div>
        <div><dt>{translate("Memória de vídeo")}</dt><dd>{memory}</dd></div>
        <div><dt>{translate("Temperatura")}</dt><dd>{formatSample(metric('host.gpu.temperature_celsius'))}</dd></div>
        <div><dt>{translate("Potência")}</dt><dd>{formatSample(metric('host.gpu.power_watts'))}</dd></div>
        <div><dt>{translate("Ventoinha")}</dt><dd>{formatSample(metric('host.gpu.fan_percent'))}</dd></div>
      </dl>{gpuMetrics.error && <small role="status">{translate("Fluxo de telemetria interrompido; tentando reconectar.")}</small>}</div>
      <TelemetryHistoryChart title={`${translate("Uso da VRAM")} · ${device.name}`} valueLabel={translate("VRAM usada")} unit={translate("GiB")} color="#a78bfa" currentValue={vramUsedBytes == null ? translate("Indisponível") : vramUsedBytes <= 0 ? translate("Sem uso") : `${formatBytes(vramUsedBytes)}${total?.availability === 'available' ? ` / ${formatBytes(Number(total.value))}` : ''}`} currentSource={used?.source ?? ''} currentUpdatedAt={used ? formatTimestamp(used.timestamp) : translate("sem amostra")} historyPath="/host/metrics/history" metricName="host.gpu.vram_used_bytes" labels={{ pci_bdf: device.pci_bdf }} transform={bytesToGiB} formatAxis={formatGiB}/>
      {device.limitation && <p className="inline-message">{translate(device.limitation)}</p>}<button className="button button-secondary" disabled title={translate("A API atual não fornece endpoint para alterar atribuição de GPU")}>{translate("Atribuição não disponível")}</button></article>}) : gpu.data ? <div className="panel resource-panel"><EmptyState icon={item.icon} title={gpu.data.stale ? translate("Inventário de GPU indisponível") : translate("Nenhuma GPU listada")} body={translate(gpu.data.message ?? 'O endpoint respondeu com inventário vazio.')}/></div> : <div className="panel resource-panel"><EmptyState icon={item.icon} title={gpu.error ? translate(item.title) : translate("Consultando inventário")} body={translate(gpu.error?.message ?? 'Aguardando dados reais do host.')}/></div>}</section></>;
}

function Login({ session, sessionError, refreshSession }: { session: SessionView | null; sessionError: Error | null; refreshSession: () => Promise<void> }) {
  const navigate = useNavigate();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [loginError, setLoginError] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const submit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setLoginError(null);
    setFieldErrors({});
    setSubmitting(true);
    try {
      await api.request<SessionView>('/session/login', {
        method: 'POST',
        body: JSON.stringify({ username, password }),
      });
      setPassword('');
      await refreshSession();
      navigate('/', { replace: true });
    } catch (error) {
      if (error instanceof ApiError) {
        const hiddenValues = [password].filter(Boolean);
        const safeMessage = hiddenValues.reduce((message, secret) => message.split(secret).join('[oculto]'), error.message);
        setLoginError(translate(safeMessage));
        setFieldErrors(Object.fromEntries(Object.entries(error.fieldErrors).map(([field, message]) => [field, translate(message)])));
      } else setLoginError(error instanceof Error ? translate(error.message) : translate("Não foi possível iniciar a sessão."));
    } finally {
      setSubmitting(false);
    }
  };
  const bootstrapRequired = Boolean(session?.bootstrap_required);
  const defaultCredentialsAvailable = Boolean(session?.default_credentials_available);
  const retrySession = async () => { await refreshSession(); };
  return <div className="login-page"><div className="login-card"><div className="login-header"><Brand/><LanguageSelector/></div><div className="eyebrow"><span className="eyebrow-line"/>{translate('ACESSO AO APPLIANCE')}</div><h1>{translate('Entrar no painel')}</h1><p>{translate('Use as credenciais administrativas configuradas neste appliance.')}</p>{sessionError && <div className="inline-message error-message" role="alert">{translate("Serviço de sessão indisponível:")}{' '}{translate(sessionError.message)} <button type="button" className="inline-retry" onClick={retrySession}>{translate("Tentar novamente")}</button></div>}{loginError && <div className="inline-message error-message" role="alert">{translate(loginError)}</div>}{defaultCredentialsAvailable && <p className="setup-code-help">{translate('Primeiro acesso: usuário admin e senha vhackintosh. Será obrigatório criar uma nova senha após entrar.')}</p>}{bootstrapRequired && <p className="setup-code-help">{translate('A primeira conta é configurada apenas no console local do appliance. Execute vhackintosh-web-admin setup e depois entre aqui com o usuário e a senha criados. A ativação do produto é opcional e fica disponível após o login.')}</p>}<form onSubmit={submit}>
    <label htmlFor="username">{translate("Usuário administrador")}</label><input id="username" name="username" autoComplete="username" required minLength={1} maxLength={64} value={username} aria-invalid={Boolean(fieldErrors.username)} aria-describedby={fieldErrors.username ? 'username-error' : undefined} onChange={(event) => setUsername(event.target.value)}/>{fieldErrors.username && <small id="username-error" className="field-error">{translate(fieldErrors.username)}</small>}
    <label htmlFor="password">{translate("Senha")}</label><input id="password" name="password" type="password" autoComplete="current-password" required minLength={1} maxLength={1024} value={password} aria-invalid={Boolean(fieldErrors.password)} aria-describedby={fieldErrors.password ? 'password-error' : undefined} onChange={(event) => setPassword(event.target.value)}/>{fieldErrors.password && <small id="password-error" className="field-error">{translate(fieldErrors.password)}</small>}
    <button className="button button-primary login-submit" type="submit" disabled={submitting || !session || Boolean(sessionError)}><span>{submitting ? translate("Validando…") : translate("Entrar")}</span></button></form><div className="login-foot"><ShieldCheck size={15}/>{translate("Sessão protegida por cookie")}{' '}<span>·</span>{' '}{translate("Acesso local")}</div></div></div>;
}

function LoadingSession() {
  return <div className="login-page"><div className="login-card"><div className="login-header"><Brand/><LanguageSelector/></div><p role="status">{translate("Consultando sessão do appliance…")}</p></div></div>;
}

function AppContent() {
  const session = usePolling<SessionView>('/session', 20000);
  const navigate = useNavigate();
  if (!session.data?.authenticated) {
    if (!session.data && !session.error) return <LoadingSession/>;
    return <Login session={session.data} sessionError={session.error} refreshSession={session.refresh}/>;
  }
  if (session.data.must_change_password) {
    return <div className="login-page"><section className="login-card"><div className="login-header"><Brand/><LanguageSelector/></div><div className="eyebrow"><span className="eyebrow-line"/> {translate('PROTEÇÃO DA CONTA')}</div><h1>{translate('Crie uma nova senha')}</h1><p>{translate('A senha provisória só pode ser usada neste primeiro acesso. Defina uma senha nova para continuar.')}</p><PasswordChangeForm onComplete={async () => { await session.refresh(); navigate('/settings', { replace: true }); }}/></section></div>;
  }
  const vmEditorRoute = <Suspense fallback={<div className="data-state" role="status">{translate('Carregando configuração da VM…')}</div>}><VmEditor/></Suspense>;
  return <Routes><Route path="/login" element={<Navigate to="/" replace/>}/><Route element={<Shell username={session.data.username ?? 'Administrador'} onLogout={async () => { window.location.replace('/login'); }}/> }><Route index element={<Overview/>}/><Route path="host" element={<HostPage/>}/><Route path="vms" element={<Vms/>}/><Route path="vms/new" element={vmEditorRoute}/><Route path="vms/:vmId/edit" element={vmEditorRoute}/><Route path="vms/:vmId/*" element={<VmDetail/>}/><Route path="gpu" element={<ResourcePage page="GPU"/>}/><Route path="storage" element={<ResourcePage page="Armazenamento"/>}/><Route path="tasks" element={<TasksPage/>}/><Route path="settings" element={<SettingsPage/>}/><Route path="*" element={<Overview/>}/></Route></Routes>;
}

export default function App() {
  appLocale = useLocale();
  return <ToastProvider><AppContent/></ToastProvider>;
}
