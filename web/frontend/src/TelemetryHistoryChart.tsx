import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Pause, Play, RefreshCw } from 'lucide-react';
import type uPlot from 'uplot';
import { ApiError, api } from './api/client';
import type { TelemetryHistory } from './api/contracts';
import { translate as t } from './i18n';

interface MetricPoint { timestamp: number; value: number | null }
type WindowChoice = '5m' | '1h' | '24h';

interface TelemetryHistoryChartProps {
  title: string;
  valueLabel: string;
  unit: string;
  color: string;
  currentValue: string;
  currentSource: string;
  currentUpdatedAt: string;
  historyPath: string;
  metricName: string;
  labels?: Record<string, string>;
  min?: number;
  max?: number;
  transform?: (value: number) => number;
  formatAxis?: (value: number) => string;
}

const getPeriod = (choice: WindowChoice) => choice === '5m' ? 5 * 60 * 1000 : choice === '1h' ? 60 * 60 * 1000 : 24 * 60 * 60 * 1000;
const identity = (value: number) => value;

export default function TelemetryHistoryChart({
  title, valueLabel, unit, color, currentValue, currentSource, currentUpdatedAt,
  historyPath, metricName, labels, min, max, transform = identity, formatAxis,
}: TelemetryHistoryChartProps) {
  const rootRef = useRef<HTMLDivElement>(null);
  const plotRef = useRef<uPlot | null>(null);
  const requestGeneration = useRef(0);
  const [Plot, setPlot] = useState<typeof uPlot | null>(null);
  const [paused, setPaused] = useState(false);
  const pausedRef = useRef(paused);
  const [windowChoice, setWindowChoice] = useState<WindowChoice>('1h');
  const [history, setHistory] = useState<{ points: MetricPoint[]; updatedAt: number } | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [refreshTick, setRefreshTick] = useState(0);
  pausedRef.current = paused;
  const resolution = windowChoice === '24h' ? 60 : 10;
  const limit = 5000;
  const labelsParam = JSON.stringify(labels ?? {});

  const refresh = useCallback(async (signal?: AbortSignal) => {
    const generation = ++requestGeneration.current;
    const until = new Date();
    const since = new Date(until.getTime() - getPeriod(windowChoice));
    const query = new URLSearchParams({
      name: metricName,
      resolution_seconds: String(resolution),
      since: since.toISOString(),
      until: until.toISOString(),
      limit: String(limit),
    });
    if (labelsParam !== '{}') query.set('labels', labelsParam);
    setLoading(true);
    setError(null);
    try {
      const result = await api.request<TelemetryHistory>(`${historyPath}?${query}`, { signal });
      if (signal?.aborted || generation !== requestGeneration.current) return;
      if (result.name !== metricName || result.resolution_seconds !== resolution || !Array.isArray(result.points)) {
        throw new Error(t('A API retornou um histórico incompatível.'));
      }
      const points = result.points.flatMap((point) => {
        const timestamp = Date.parse(point.timestamp);
        const raw = point.availability !== 'unavailable' && typeof point.average === 'number' ? point.average : null;
        const value = raw == null ? null : transform(raw);
        if (!Number.isFinite(timestamp) || (value != null && !Number.isFinite(value))) return [];
        return [{ timestamp, value }];
      }).slice(-limit);
      setHistory({ points, updatedAt: Date.now() });
    } catch (reason) {
      if (signal?.aborted || generation !== requestGeneration.current) return;
      const status = reason instanceof ApiError ? reason.status : undefined;
      setError(status === 503
        ? t('O serviço de histórico está indisponível neste appliance.')
        : reason instanceof Error ? t(reason.message) : t('Não foi possível consultar o histórico.'));
      setHistory(null);
    } finally {
      if (!signal?.aborted && generation === requestGeneration.current) setLoading(false);
    }
  }, [historyPath, labelsParam, metricName, resolution, transform, windowChoice]);

  useEffect(() => {
    const controller = new AbortController();
    setHistory(null);
    void refresh(controller.signal);
    const timer = window.setInterval(() => {
      if (!pausedRef.current && document.visibilityState === 'visible') void refresh();
    }, 30_000);
    const onVisible = () => { if (!pausedRef.current && document.visibilityState === 'visible') void refresh(); };
    document.addEventListener('visibilitychange', onVisible);
    return () => {
      controller.abort();
      requestGeneration.current += 1;
      window.clearInterval(timer);
      document.removeEventListener('visibilitychange', onVisible);
    };
  }, [refresh, refreshTick]);

  const points = history?.points ?? [];
  const visiblePoints = points;
  const data = useMemo<uPlot.AlignedData>(() => [
    visiblePoints.map((point) => point.timestamp / 1000),
    visiblePoints.map((point) => point.value),
  ], [visiblePoints]);

  useEffect(() => {
    let active = true;
    void Promise.all([import('uplot'), import('uplot/dist/uPlot.min.css')]).then(([module]) => {
      if (active) setPlot(() => module.default);
    }).catch(() => { if (active) setPlot(null); });
    return () => { active = false; };
  }, []);

  useEffect(() => {
    const root = rootRef.current;
    if (!root || !Plot) return;
    const options: uPlot.Options = {
      width: Math.max(220, root.clientWidth), height: 174,
      class: 'telemetry-uplot',
      legend: { show: false },
      cursor: { points: { size: 6, width: 1 } },
      scales: { x: { time: true }, y: { auto: min == null && max == null, range: min != null || max != null ? [min ?? 0, max ?? 100] : undefined } },
      axes: [
        { stroke: '#8393a9', grid: { stroke: '#263449', width: 1 }, ticks: { stroke: '#344157', width: 1 }, font: '10px Inter, sans-serif', values: (_plot, ticks) => ticks.map((tick) => new Date(tick * 1000).toLocaleTimeString(t('pt-BR'), { hour: '2-digit', minute: '2-digit', second: '2-digit' })) },
        { stroke: '#8393a9', grid: { stroke: '#263449', width: 1 }, ticks: { stroke: '#344157', width: 1 }, font: '10px Inter, sans-serif', size: 45, values: (_plot, ticks) => ticks.map((tick) => formatAxis ? formatAxis(tick) : `${tick.toLocaleString(t('pt-BR'), { maximumFractionDigits: 1 })}`) },
      ],
      series: [
        {},
        { label: valueLabel, stroke: color, fill: `${color}22`, width: 2, points: { show: false }, spanGaps: false },
      ],
    };
    const plot = new Plot(options, data, root);
    plotRef.current = plot;
    const observer = new ResizeObserver((entries) => {
      const width = entries[0]?.contentRect.width;
      if (width && plotRef.current === plot) plot.setSize({ width: Math.max(220, width), height: 174 });
    });
    observer.observe(root);
    return () => { observer.disconnect(); plot.destroy(); if (plotRef.current === plot) plotRef.current = null; };
    // Data changes are applied with setData below to preserve the chart instance.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [Plot, color, valueLabel, min, max, formatAxis]);

  useEffect(() => { plotRef.current?.setData(data); }, [data, Plot]);

  const latest = [...points].reverse().find((point) => point.value != null)?.value;
  const updatedAt = history ? new Date(history.updatedAt).toLocaleTimeString(t('pt-BR')) : t('sem atualização');
  const stateMessage = error ?? (loading && !history ? t('Consultando histórico…') : points.length === 0 ? t('Ainda não há histórico armazenado para esta métrica e período.') : null);
  return <article className="panel telemetry-chart-card">
    <div className="telemetry-chart-heading">
      <div><span className="section-kicker">{t('HISTÓRICO PERSISTIDO')}</span><h3>{title}</h3></div>
      <span className="telemetry-current">{currentValue}</span>
      <button type="button" className="chart-pause-button" aria-label={`${t(paused ? 'Retomar' : 'Pausar')} ${t('atualizações do histórico de')} ${title}`} aria-pressed={paused} onClick={() => setPaused((value) => !value)} title={t(paused ? 'Retomar atualizações do histórico' : 'Pausar atualizações do histórico')}>{paused ? <Play size={14} aria-hidden="true"/> : <Pause size={14} aria-hidden="true"/>}<span>{t(paused ? 'Retomar' : 'Pausar')}</span></button>
    </div>
    <div className="history-controls" role="group" aria-label={`${t('Opções do histórico de')} ${title}`}>
      <label>{t('Período e resolução')}
        <select value={windowChoice} onChange={(event) => setWindowChoice(event.target.value as WindowChoice)}>
          <option value="5m">{t('5 minutos · detalhe de 10 s')}</option>
          <option value="1h">{t('1 hora · detalhe de 10 s')}</option>
          <option value="24h">{t('24 horas · detalhe de 60 s')}</option>
        </select>
      </label>
      <button type="button" className="history-refresh" onClick={() => setRefreshTick((tick) => tick + 1)} disabled={loading} aria-label={`${t('Atualizar histórico de')} ${title}`} title={t('Consultar novamente')}><RefreshCw size={14}/></button>
    </div>
    <div className="telemetry-chart" ref={rootRef} role="region" aria-label={`${t('Gráfico de')} ${title}. ${t('Valor atual:')} ${currentValue}. ${t('Unidade:')} ${unit}. ${t('Histórico:')} ${visiblePoints.length} ${t('intervalos de')} ${resolution} ${t('segundos no período de')} ${windowChoice}.`}>
      {!Plot && <div className="chart-loading" role="status">{t('Carregando gráfico…')}</div>}
      {Plot && stateMessage && <div className={`chart-loading ${error ? 'chart-error' : ''}`} role={error ? 'alert' : 'status'}>{stateMessage}</div>}
    </div>
    <div className="telemetry-chart-foot"><span>{points.length ? `${points.length} ${t('agregações')} · ${resolution} s` : t('Sem histórico medido')}</span><span>{latest == null ? `${t(currentSource || 'Fonte indisponível')} · ${t(currentUpdatedAt)}` : `${t('Último ponto')} ${latest.toLocaleString(t('pt-BR'), { maximumFractionDigits: 2 })} ${unit} · ${t('consultado')} ${updatedAt}`}</span></div>
    <p className="sr-only">{title}: {t('valor atual')} {currentValue}; {points.length} {t('agregações históricas no período selecionado.')}. {t('Resolução de')} {resolution} {t('segundos.')}{paused ? ` ${t('Atualizações pausadas.')}` : ''}{error ? ` ${t('Erro:')} ${error}` : ''}</p>
  </article>;
}
