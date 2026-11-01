import { useState } from 'react';
import { Check, CircleAlert, Clock3, RefreshCw } from 'lucide-react';
import { api } from './api/client';
import { usePolling } from './api/usePolling';
import { useToast } from './components';
import './HostAlerts.css';
import { translate as t } from './i18n';

type AlertCode = 'cpu.high' | 'memory.high' | 'temperature.high' | 'filesystem.low';
interface HostAlert {
  id: string;
  code: AlertCode;
  title: string;
  severity: 'warning' | 'critical';
  unit: '%' | '°C';
  value: number;
  first_seen: string;
  last_seen: string;
  acknowledged_at: string | null;
  cleared_at: string | null;
}
interface HostAlertList { items: HostAlert[]; persistence_enabled: boolean; limit: number }

const formatDate = (value: string) => new Intl.DateTimeFormat(t('pt-BR'), {
  dateStyle: 'short', timeStyle: 'short',
}).format(new Date(value));

/** Shows persistent host resource alerts and allows acknowledging active events. */
export default function HostAlerts() {
  const [includeCleared, setIncludeCleared] = useState(false);
  const [acknowledging, setAcknowledging] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const notify = useToast();
  const path = '/host/alerts?include_cleared=' + includeCleared + '&limit=100';
  const alerts = usePolling<HostAlertList>(path, 15000);
  const items = alerts.data?.items ?? [];

  const acknowledge = async (alert: HostAlert) => {
    setAcknowledging(alert.id);
    setActionError(null);
    try {
      await api.request<void>('/host/alerts/' + alert.id + '/acknowledge', { method: 'POST' });
      await alerts.refresh();
      notify(t('Alerta reconhecido.'), 'success');
    } catch (error) {
      const message = error instanceof Error ? t(error.message) : t('Não foi possível reconhecer o alerta.');
      setActionError(message);
    } finally {
      setAcknowledging(null);
    }
  };

  return <section className="panel host-alerts" aria-labelledby="host-alerts-title">
    <header className="host-alerts-heading">
      <div className="host-alerts-title-wrap">
        <span className="host-alerts-icon" aria-hidden="true"><CircleAlert size={18}/></span>
        <div><span className="section-kicker">{t('EVENTOS DO SISTEMA')}</span><h2 id="host-alerts-title">{t('Alertas do host')}</h2></div>
      </div>
      <div className="host-alerts-actions">
        <label className="host-alerts-toggle"><input type="checkbox" checked={includeCleared} onChange={(event) => setIncludeCleared(event.target.checked)}/><span>{t('Incluir recuperados')}</span></label>
        <button className="icon-button" type="button" onClick={() => void alerts.refresh()} disabled={alerts.loading} aria-label={t('Atualizar alertas')} title={t('Atualizar alertas')}><RefreshCw size={16}/></button>
      </div>
    </header>

    {actionError && <div className="host-alerts-error" role="alert">{actionError}</div>}
    {alerts.loading && !alerts.data && <p className="host-alerts-state" role="status">{t('Consultando alertas persistidos…')}</p>}
    {alerts.error && !alerts.data && <div className="host-alerts-error" role="alert"><span>{t('Não foi possível carregar os alertas:')} {t(alerts.error.message)}</span><button className="button button-secondary" type="button" onClick={() => void alerts.refresh()}>{t('Tentar novamente')}</button></div>}
    {alerts.data && !alerts.data.persistence_enabled && <p className="host-alerts-note" role="status">{t('O armazenamento persistente de alertas está indisponível neste appliance.')}</p>}
    {alerts.error && alerts.data && <p className="host-alerts-note" role="status">{t('Exibindo a última leitura; a atualização falhou:')} {t(alerts.error.message)}</p>}
    {alerts.data && items.length === 0 && <p className="host-alerts-state">{t(includeCleared ? 'Nenhum alerta recente registrado.' : 'Nenhum alerta ativo. As condições do host são verificadas junto com as métricas.')}</p>}
    {items.length > 0 && <ul className="host-alert-list" aria-label={t(includeCleared ? 'Alertas recentes do host' : 'Alertas ativos do host')}>
      {items.map((alert) => {
        const cleared = Boolean(alert.cleared_at);
        const acknowledged = Boolean(alert.acknowledged_at);
        const status = t(cleared ? 'Recuperado' : acknowledged ? 'Reconhecido' : 'Ativo');
        return <li className={'host-alert-row ' + alert.severity + (cleared ? ' cleared' : '')} key={alert.id}>
          <span className="host-alert-severity" aria-label={t(alert.severity === 'critical' ? 'Crítico' : 'Aviso')}>{t(alert.severity === 'critical' ? 'Crítico' : 'Aviso')}</span>
          <div className="host-alert-main">
            <strong>{alert.title}</strong>
            <span className="host-alert-value">{t('Leitura registrada:')} {alert.value.toLocaleString(t('pt-BR'), { maximumFractionDigits: 1 })}{alert.unit}</span>
            <span className="host-alert-times"><Clock3 size={13} aria-hidden="true"/> {t('Início')} {formatDate(alert.first_seen)} · {t('última ocorrência')} {formatDate(alert.last_seen)}{alert.cleared_at ? ' · ' + t('recuperação') + ' ' + formatDate(alert.cleared_at) : ''}</span>
          </div>
          <div className="host-alert-trailing"><span className={'host-alert-status ' + (cleared ? 'recovered' : acknowledged ? 'acknowledged' : 'active')}>{status}</span>
            {!cleared && !acknowledged && <button className="button button-secondary host-alert-ack" type="button" onClick={() => void acknowledge(alert)} disabled={acknowledging !== null} aria-label={t('Reconhecer alerta:') + ' ' + t(alert.title)}>
              {acknowledging === alert.id ? t('Salvando…') : <><Check size={14} aria-hidden="true"/> {t('Reconhecer')}</>}
            </button>}
          </div>
        </li>;
      })}
    </ul>}
    {alerts.data && items.length === alerts.data.limit && <p className="host-alerts-footnote">{t('Mostrando até')} {alerts.data.limit} {t('eventos mais recentes.')}</p>}
  </section>;
}
