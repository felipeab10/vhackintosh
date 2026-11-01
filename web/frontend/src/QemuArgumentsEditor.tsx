import { useEffect, useState } from 'react';
import CodeMirror from '@uiw/react-codemirror';
import { json } from '@codemirror/lang-json';
import { EditorView } from '@codemirror/view';
import { AlertTriangle, Braces } from 'lucide-react';
import { translate as t } from './i18n';

const MAX_TOKENS = 64;
const MAX_TOKEN_LENGTH = 512;
const ALLOWED_PAIRS: Record<string, Set<string>> = {
  '-rtc': new Set(['base=utc']),
  '-msg': new Set(['timestamp=on']),
};

const editorTheme = EditorView.theme({
  '&': { backgroundColor: '#09121d', color: '#e5edf7', fontSize: '13px' },
  '.cm-content': { fontFamily: '"Fira Code", monospace', minHeight: '132px', padding: '12px 0' },
  '.cm-gutters': { backgroundColor: '#09121d', color: '#65758b', border: 'none' },
  '.cm-activeLine': { backgroundColor: '#111e2c' },
  '&.cm-focused': { outline: '2px solid #22d3ee', outlineOffset: '2px' },
});

function parseTokens(source: string): { tokens?: string[]; error?: string } {
  let parsed: unknown;
  try { parsed = JSON.parse(source); }
  catch { return { error: t('Use um array JSON válido, por exemplo ["-display", "sdl"].') }; }
  if (!Array.isArray(parsed) || parsed.some((item) => typeof item !== 'string')) {
    return { error: t('O conteúdo precisa ser um array JSON de strings. Cada string representa exatamente um token.') };
  }
  if (parsed.length > MAX_TOKENS) return { error: t('Limite de 64 tokens.') };
  if (parsed.some((token) => !token.length || token.length > MAX_TOKEN_LENGTH || token.includes('\0'))) {
    return { error: t('Cada token deve ter entre 1 e 512 caracteres e não conter NUL.') };
  }
  if (parsed.some((token) => token.startsWith('@') || /[\r\n]/.test(token))) {
    return { error: t('Arquivos de resposta e quebras de linha dentro de um token não são aceitos.') };
  }
  const seen = new Set<string>();
  for (let index = 0; index < parsed.length;) {
    const option = parsed[index];
    const allowed = ALLOWED_PAIRS[option];
    if (!allowed) return { error: `${t('A opção')} ${option} ${t('ainda não está liberada. Use apenas -rtc base=utc e -msg timestamp=on.')}` };
    if (seen.has(option)) return { error: `${option} ${t('pode aparecer somente uma vez.')}` };
    if (!allowed.has(parsed[index + 1] ?? '')) return { error: `${option} ${t('exige um valor permitido:')} ${[...allowed].join(' ou ')}.` };
    seen.add(option);
    index += 2;
  }
  return { tokens: parsed };
}

interface QemuArgumentsEditorProps {
  value: string[];
  onChange: (tokens: string[]) => void;
  onValidityChange: (valid: boolean) => void;
}

export default function QemuArgumentsEditor({ value, onChange, onValidityChange }: QemuArgumentsEditorProps) {
  const [source, setSource] = useState(() => JSON.stringify(value, null, 2));
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const formatted = JSON.stringify(value, null, 2);
    setSource(formatted);
    const result = parseTokens(formatted);
    setError(result.error ?? null);
    onValidityChange(!result.error);
    // The text buffer may temporarily contain invalid JSON, so a parent
    // rerender must not replace it until the parsed token array changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value]);

  const updateSource = (next: string) => {
    setSource(next);
    const result = parseTokens(next);
    setError(result.error ?? null);
    onValidityChange(!result.error);
    if (result.tokens) onChange(result.tokens);
  };

  return <div className="qemu-token-editor">
    <div className="qemu-editor-title"><Braces size={16}/><strong>{t('Tokens QEMU · modo avançado')}</strong><span>{value.length}/{MAX_TOKENS}</span></div>
    <label className="sr-only" htmlFor="qemu-token-editor">{t('Argumentos QEMU como array JSON ordenado')}</label>
    <CodeMirror id="qemu-token-editor" value={source} height="196px" extensions={[json(), editorTheme]} onChange={updateSource} aria-label={t('Argumentos QEMU como array JSON ordenado')} basicSetup={{ lineNumbers: true, foldGutter: false, highlightActiveLine: true, tabSize: 2 }}/>
    {error && <p className="field-error qemu-editor-error" role="alert"><AlertTriangle size={14}/>{error}</p>}
    <div className="qemu-editor-help">
      <p>{t('Informe um array JSON. Cada item é um token independente; espaços dentro de uma string permanecem no mesmo token e a ordem é preservada. Não há interpretação de shell.')}</p>
      <p>{t('Subset validado e aplicado no próximo boot:')} <code>-rtc</code> <code>base=utc</code> {t('e')} <code>-msg</code> <code>timestamp=on</code>. {t('Cada opção pode aparecer uma vez; a ordem é preservada.')}</p>
      <p>{t('Outras opções ainda são recusadas até o inventário identificar a versão e as capacidades do QEMU/Reims. Dispositivos, discos, rede, identidade, canais de controle e isolamento permanecem gerenciados pelo appliance.')}</p>
    </div>
  </div>;
}
