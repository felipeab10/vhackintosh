import { createContext, useContext, useEffect, useMemo, useState } from 'react';
import type { ReactNode } from 'react';
import { Globe2 } from 'lucide-react';
import { appMessages } from './locales/app';
import { editorMessages } from './locales/editor';
import { widgetMessages } from './locales/widgets';

export type Locale = 'pt-BR' | 'en-US';
type Messages = Record<string, string>;

const en: Messages = { ...appMessages, ...editorMessages, ...widgetMessages };
const LocaleContext = createContext<{ locale: Locale; setLocale: (locale: Locale) => void } | null>(null);
let activeLocale: Locale = 'pt-BR';
const translatedTextCache = new Map<string, string>();
const templateTranslations = Object.entries(en).flatMap(([source, translated]) => {
  const names: string[] = [];
  const parts = source.split(/(\{[a-zA-Z0-9_]+\})/g);
  if (!parts.some((part) => /^\{[a-zA-Z0-9_]+\}$/.test(part))) return [];
  const pattern = parts.map((part) => {
    const match = /^\{([a-zA-Z0-9_]+)\}$/.exec(part);
    if (match) { names.push(match[1]); return '(.+?)'; }
    return part.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  }).join('');
  return [{ regex: new RegExp(`^${pattern}$`), names, translated }];
});

export function translate(message: string): string {
  if (activeLocale === 'pt-BR') return message;
  const cached = translatedTextCache.get(message);
  if (cached) return cached;
  let result = en[message];
  if (!result) {
    for (const candidate of templateTranslations) {
      const match = candidate.regex.exec(message);
      if (!match) continue;
      result = candidate.translated;
      candidate.names.forEach((name, index) => { result = result!.replace(`{${name}}`, match[index + 1]); });
      break;
    }
  }
  result ??= message;
  if (translatedTextCache.size >= 512) translatedTextCache.clear();
  translatedTextCache.set(message, result);
  return result;
}

export function I18nProvider({ children }: { children: ReactNode }) {
  const [locale, setLocaleState] = useState<Locale>(() => {
    try { return window.localStorage.getItem('vhackintosh.locale') === 'en-US' ? 'en-US' : 'pt-BR'; }
    catch { return 'pt-BR'; }
  });
  const setLocale = (value: Locale) => setLocaleState(value);
  activeLocale = locale;
  useEffect(() => {
    document.documentElement.lang = locale;
    try { window.localStorage.setItem('vhackintosh.locale', locale); } catch { /* Private browsing may disable storage. */ }
  }, [locale]);
  const value = useMemo(() => ({ locale, setLocale }), [locale]);
  return <LocaleContext.Provider value={value}>{children}</LocaleContext.Provider>;
}

/** Subscribe at the app root so a locale change redraws all routed pages. */
export function useLocale(): Locale {
  const value = useContext(LocaleContext);
  if (!value) throw new Error('useLocale must be used inside I18nProvider');
  return value.locale;
}

export function LanguageSelector() {
  const value = useContext(LocaleContext);
  if (!value) throw new Error('LanguageSelector must be used inside I18nProvider');
  return <label className="language-selector">
    <Globe2 aria-hidden="true" size={16}/>
    <span className="sr-only">{translate('Idioma do painel')}</span>
    <select aria-label={translate('Idioma do painel')} value={value.locale} onChange={(event) => value.setLocale(event.target.value as Locale)}>
      <option value="pt-BR">Português (Brasil)</option>
      <option value="en-US">English</option>
    </select>
  </label>;
}
