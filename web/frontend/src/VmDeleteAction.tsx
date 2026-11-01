import { useState } from 'react';
import { Trash2 } from 'lucide-react';
import { ApiError, api } from './api/client';
import type { VmDeleteResponse } from './api/contracts';
import { ConfirmationDialog, useToast } from './components';
import { translate as t } from './i18n';

export default function VmDeleteAction({
  vmId, vmName, revision, onDeleted,
}: {
  vmId: string;
  vmName: string;
  revision: string;
  onDeleted: (result: VmDeleteResponse) => void;
}) {
  const [open, setOpen] = useState(false);
  const [typedId, setTypedId] = useState('');
  const [deleteDisk, setDeleteDisk] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const notify = useToast();

  const resetAfterClose = () => {
    setOpen(false);
    setTypedId('');
    setDeleteDisk(false);
    setError(null);
  };

  const remove = async () => {
    if (typedId !== vmId || !revision || busy) return;
    setBusy(true);
    setError(null);
    try {
      const result = await api.request<VmDeleteResponse>(`/vms/${encodeURIComponent(vmId)}`, {
        method: 'DELETE',
        headers: { 'If-Match': `"${revision}"` },
        body: JSON.stringify({ confirmation: typedId, delete_disk: deleteDisk }),
      });
      setOpen(false);
      setTypedId('');
      setDeleteDisk(false);
      notify(t(deleteDisk ? 'Cadastro da VM removido e exclusão do disco solicitada.' : 'Cadastro da VM removido; o disco foi preservado.'));
      onDeleted(result);
    } catch (reason) {
      if (reason instanceof ApiError && ['catalog_conflict', 'catalog_revision_required'].includes(reason.code ?? '')) {
        setError(t('O catálogo mudou desde que esta página foi aberta. Atualize as VMs e confira a configuração antes de tentar novamente.'));
      } else if (reason instanceof ApiError && reason.code === 'vm_running') {
        setError(t('Esta VM está em execução. Encerre-a antes de removê-la do catálogo.'));
      } else if (reason instanceof ApiError && reason.code === 'disk_shared') {
        setError(reason.message);
      } else if (reason instanceof ApiError && reason.status === 412) {
        setError(t('A revisão desta VM ficou desatualizada. Atualize o catálogo e confira os dados antes de tentar novamente.'));
      } else {
        setError(reason instanceof Error ? t(reason.message) : t('Não foi possível remover esta VM.'));
      }
    } finally {
      setBusy(false);
    }
  };

  return <>
    <button type="button" className="vm-row-remove" aria-label={`${t('Remover')} ${vmName}`} onClick={() => setOpen(true)}>
      <Trash2 size={15}/><span>{t('Remover')}</span>
    </button>
    <ConfirmationDialog
      open={open}
      title={`${t('Remover')} ${vmName}?`}
      titleIsTranslated
      description="A VM será removida do catálogo. O arquivo do disco será mantido, a menos que você escolha excluí-lo separadamente. A operação exige que a VM esteja parada."
      confirmLabel="Remover VM"
      confirmClassName="button-danger"
      cancelLabel="Cancelar"
      busy={busy}
      confirmDisabled={typedId !== vmId || !revision}
      onConfirm={() => void remove()}
      onClose={resetAfterClose}
    >
      <div className="vm-delete-form">
        <label htmlFor={`delete-confirm-${vmId}`}>{t('Digite o ID exato para confirmar')} <code>{vmId}</code></label>
        <input
          id={`delete-confirm-${vmId}`}
          autoComplete="off"
          autoCapitalize="none"
          spellCheck={false}
          value={typedId}
          onChange={(event) => setTypedId(event.target.value)}
          aria-describedby={`delete-confirm-hint-${vmId}`}
        />
        <small id={`delete-confirm-hint-${vmId}`}>{t('O ID diferencia letras maiúsculas e minúsculas.')}</small>
        <label className="vm-delete-disk-choice">
          <input type="checkbox" checked={deleteDisk} onChange={(event) => setDeleteDisk(event.target.checked)}/>
          <span><strong>{t('Excluir também o arquivo do disco')}</strong><small>{t('Exclusão permanente, desmarcada por padrão. Só discos gerenciados podem ser removidos; o agente bloqueia arquivos associados ou usados pela cadeia de discos de outra VM.')}</small></span>
        </label>
        {error && <div className="inline-message error-message" role="alert">{error}</div>}
      </div>
    </ConfirmationDialog>
  </>;
}
