import { Children, cloneElement, createContext, isValidElement, useCallback, useContext, useEffect, useId, useMemo, useRef, useState } from 'react';
import type { ElementType, HTMLAttributes, ReactElement, ReactNode } from 'react';
import { NavLink } from 'react-router-dom';
import { X } from 'lucide-react';
import { translate as t } from './i18n';

type CardProps = HTMLAttributes<HTMLElement> & { as?: ElementType };

/** Surface primitive shared by dashboard, detail and settings pages. */
export function Card({ as: Component = 'section', className = '', ...props }: CardProps) {
  return <Component className={`panel ${className}`.trim()} {...props} />;
}

/** Semantic list container for compact desktop rows that reflow into touch-friendly mobile entries. */
export function ResponsiveList({ children, className = '', label }: { children: ReactNode; className?: string; label?: string }) {
  return <ul className={`ui-responsive-list ${className}`.trim()} aria-label={label}>{children}</ul>;
}

export function StatusBadge({ children, tone = 'neutral', className = '' }: {
  children: ReactNode; tone?: 'neutral' | 'success' | 'warning' | 'danger' | 'info'; className?: string;
}) {
  return <span className={`ui-status ui-status-${tone} ${className}`.trim()}>{children}</span>;
}

/** Accessible determinate and indeterminate progress display. */
export function ProgressBar({ value, label, className = '' }: { value: number | null; label: string; className?: string }) {
  const bounded = value == null || !Number.isFinite(value) ? null : Math.min(100, Math.max(0, value));
  return <div className={`ui-progress ${bounded == null ? 'ui-progress-indeterminate' : ''} ${className}`.trim()}
    role="progressbar" aria-label={label} aria-valuemin={0} aria-valuemax={100} aria-valuenow={bounded ?? undefined}>
    <span style={bounded == null ? undefined : { width: `${bounded}%` }} />
  </div>;
}

export function FormField({ label, hint, error, children, className = '' }: {
  label: string; hint?: string; error?: string; children: ReactNode; className?: string;
}) {
  const id = useId();
  const hintId = hint ? `${id}-hint` : undefined;
  const errorId = error ? `${id}-error` : undefined;
  const describedBy = [hintId, errorId].filter(Boolean).join(' ') || undefined;
  let controlIdAssigned = false;
  const linkedChildren = Children.map(children, (child) => {
    if (controlIdAssigned || !isValidElement(child) || typeof child.type !== 'string' || !['input', 'select', 'textarea'].includes(child.type)) return child;
    controlIdAssigned = true;
    const control = child as ReactElement<Record<string, unknown>>;
    const existingDescription = typeof control.props['aria-describedby'] === 'string' ? control.props['aria-describedby'] : '';
    const mergedDescription = [...new Set([existingDescription, describedBy].filter(Boolean).flatMap((value) => value!.split(/\s+/)))].join(' ') || undefined;
    return cloneElement(control, {
      id: typeof control.props.id === 'string' ? control.props.id : id,
      'aria-describedby': mergedDescription,
      'aria-invalid': error ? true : control.props['aria-invalid'],
    });
  });
  return <label className={`ui-field vm-field ${className}`.trim()}>
    <span className="ui-field-label vm-field-label">{label}</span>{linkedChildren}
    {hint && <small className="ui-field-hint vm-field-hint" id={hintId}>{hint}</small>}
    {error && <small className="ui-field-error field-error" id={errorId} role="alert">{error}</small>}
  </label>;
}

export interface NavigationTab { label: string; to: string; current: boolean }
export function NavigationTabs({ label, items, className = '' }: { label: string; items: NavigationTab[]; className?: string }) {
  return <nav className={`ui-tabs ${className}`.trim()} aria-label={label}>
    {items.map((item) => <NavLink key={item.to} to={item.to} aria-current={item.current ? 'page' : undefined} className={item.current ? 'selected' : ''}>{item.label}</NavLink>)}
  </nav>;
}

/** Native modal dialog provides focus containment, Escape handling and focus restoration. */
export function ConfirmationDialog({ open, title, description, confirmLabel = 'Confirmar', cancelLabel = 'Cancelar', busy = false, confirmDisabled = false, confirmClassName = 'button-primary', titleIsTranslated = false, children, onConfirm, onClose }: {
  open: boolean; title: string; description: string; confirmLabel?: string; cancelLabel?: string; busy?: boolean; confirmDisabled?: boolean;
  confirmClassName?: string; titleIsTranslated?: boolean; children?: ReactNode; onConfirm: () => void; onClose: () => void;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  const descriptionId = useId();
  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (open && !dialog.open) dialog.showModal();
    if (!open && dialog.open) dialog.close();
  }, [open]);
  return <dialog ref={ref} className="ui-confirm-dialog" aria-labelledby={titleId} aria-describedby={descriptionId}
    onCancel={(event) => { event.preventDefault(); onClose(); }} onClose={onClose}>
    <h2 id={titleId}>{titleIsTranslated ? title : t(title)}</h2><p id={descriptionId}>{t(description)}</p>{children}
    <div className="ui-confirm-actions"><button type="button" className="button button-secondary" onClick={onClose} disabled={busy}>{t(cancelLabel)}</button><button type="button" className={`button ${confirmClassName}`} onClick={onConfirm} disabled={busy || confirmDisabled}>{busy ? t('Aguarde…') : t(confirmLabel)}</button></div>
  </dialog>;
}

type ToastTone = 'success' | 'error' | 'info';
interface ToastMessage { id: number; message: string; tone: ToastTone }
interface ToastContextValue { notify: (message: string, tone?: ToastTone) => void }
const ToastContext = createContext<ToastContextValue | null>(null);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [messages, setMessages] = useState<ToastMessage[]>([]);
  const sequence = useRef(0);
  const dismiss = useCallback((id: number) => setMessages((items) => items.filter((item) => item.id !== id)), []);
  const notify = useCallback((message: string, tone: ToastTone = 'info') => {
    const id = ++sequence.current;
    setMessages((items) => [...items.slice(-3), { id, message, tone }]);
    window.setTimeout(() => dismiss(id), 5000);
  }, [dismiss]);
  const context = useMemo(() => ({ notify }), [notify]);
  return <ToastContext.Provider value={context}>
    {children}
    <div className="ui-toast-viewport" aria-label={t('Notificações')} aria-live="polite" aria-relevant="additions">
      {messages.map((item) => <div key={item.id} className={`ui-toast ui-toast-${item.tone}`} role={item.tone === 'error' ? 'alert' : 'status'}>
        <span>{t(item.message)}</span><button type="button" className="ui-toast-dismiss" aria-label={t('Dispensar notificação')} onClick={() => dismiss(item.id)}><X size={16}/></button>
      </div>)}
    </div>
  </ToastContext.Provider>;
}

export function useToast() {
  const context = useContext(ToastContext);
  if (!context) throw new Error(t('useToast deve ser usado dentro de ToastProvider'));
  return context.notify;
}
