import assert from 'node:assert/strict';
import { readFile, readdir } from 'node:fs/promises';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const [html, app, css, i18n, localeApp, localeEditor, localeWidgets] = await Promise.all([
  readFile(resolve(root, 'index.html'), 'utf8'),
  readFile(resolve(root, 'src/App.tsx'), 'utf8'),
  readFile(resolve(root, 'src/styles.css'), 'utf8'),
  readFile(resolve(root, 'src/i18n.tsx'), 'utf8'),
  readFile(resolve(root, 'src/locales/app.ts'), 'utf8'),
  readFile(resolve(root, 'src/locales/editor.ts'), 'utf8'),
  readFile(resolve(root, 'src/locales/widgets.ts'), 'utf8'),
]);

function mediaBlock(source, query) {
  const start = source.indexOf(query);
  assert.notEqual(start, -1, `missing ${query}`);
  const open = source.indexOf('{', start);
  let depth = 0;
  for (let index = open; index < source.length; index += 1) {
    if (source[index] === '{') depth += 1;
    if (source[index] === '}' && --depth === 0) return source.slice(open + 1, index);
  }
  assert.fail(`unclosed ${query}`);
}

test('document declares Brazilian Portuguese and a device-width viewport', () => {
  assert.match(html, /<html\s+lang="pt-BR"/);
  assert.match(html, /name="viewport"\s+content="width=device-width, initial-scale=1(?:\.0)?"/);
});

test('language selector exposes both locales with an accessible native control', () => {
  assert.match(i18n, /value="pt-BR"/);
  assert.match(i18n, /value="en-US"/);
  assert.match(i18n, /aria-label=\{translate\('Idioma do painel'\)\}/);
  assert.match(i18n, /localStorage\.setItem\('vhackintosh\.locale'/);
  assert.match(i18n, /document\.documentElement\.lang = locale/);
  assert.match(app, /LanguageSelector/);
  assert.match(app, /className="login-header"><Brand\/><LanguageSelector\/>/);
  assert.match(css, /\.language-selector\s*\{[^}]*min-height:\s*44px/);
  assert.match(css, /\.language-selector select:focus-visible/);
  assert.match(css, /\.language-selector select\s*\{[^}]*color-scheme:\s*dark/);
  assert.match(css, /\.language-selector select option\s*\{[^}]*background-color:\s*#0d1119/);
  assert.match(css, /\.language-selector select option:checked\s*\{[^}]*background-color:\s*var\(--accent-cyan\)/);
  assert.match(css, /\.login-header\s*\{[^}]*justify-content:\s*space-between/);
});

test('appliance login never asks for the administrator bootstrap code', () => {
  const login = app.slice(app.indexOf('function Login('), app.indexOf('function LoadingSession('));
  assert.doesNotMatch(login, /activationCode|activation-code|session\/bootstrap|Código de configuração do administrador/);
  assert.match(login, /vhackintosh-web-admin setup/);
  assert.match(login, /JSON\.stringify\(\{ username, password \}\)/);
  assert.match(login, /translate\("Entrar"\)/);
});

test('English dictionaries exist for the application, VM editor, and shared widgets', () => {
  for (const [name, source] of [['application', localeApp], ['VM editor', localeEditor], ['widgets', localeWidgets]]) {
    assert.match(source, /export const \w+Messages: Record<string, string> = \{/i, `${name} locale dictionary is missing`);
    assert.match(source, /'[^']+':\s*'[^']+'/i, `${name} locale dictionary has no English entries`);
  }
});

test('every literal translate key in the dashboard has an English dictionary entry', async () => {
  const dictionaryKeys = new Set();
  for (const source of [localeApp, localeEditor, localeWidgets]) {
    for (const match of source.matchAll(/(['"])((?:\\.|(?!\1).)*)\1\s*:/g)) dictionaryKeys.add(match[2]);
  }
  const files = [];
  async function collect(directory) {
    for (const entry of await readdir(directory, { withFileTypes: true })) {
      const path = resolve(directory, entry.name);
      if (entry.isDirectory()) await collect(path);
      else if (/\.tsx$/.test(entry.name)) files.push(path);
    }
  }
  await collect(resolve(root, 'src'));
  const missing = [];
  for (const path of files) {
    const source = await readFile(path, 'utf8');
    for (const match of source.matchAll(/(?:translate|\bt)\(\s*(['"])((?:\\.|(?!\1).)*)\1/g)) {
      if (!dictionaryKeys.has(match[2])) missing.push(`${path}: ${match[2]}`);
    }
  }
  assert.deepEqual(missing, []);
});

test('pending appliance activation syncs every 10 seconds only while visible', () => {
  const activationPanel = app.slice(app.indexOf('function WebActivationPanel()'), app.indexOf('function Overview()'));
  assert.match(activationPanel, /if \(status !== 'pending'\) return/);
  assert.match(activationPanel, /window\.setInterval\(\(\) => void syncStatus\(\), 10_000\)/);
  assert.match(activationPanel, /document\.visibilityState !== 'visible'/);
  assert.match(activationPanel, /window\.clearInterval\(timer\)/);
});

test('approved appliance activation keeps detailed information in a native accordion', () => {
  const activationPanel = app.slice(app.indexOf('function WebActivationPanel()'), app.indexOf('function Overview()'));
  assert.match(activationPanel, /status === 'approved' \? <>/);
  assert.match(activationPanel, /<details className="web-activation-details">/);
  assert.match(activationPanel, /<summary>\{translate\('Ver detalhes da ativação'\)\}<\/summary>/);
});

test('GPU VRAM charts show the card name, filter history by PCI address, and label idle cards', async () => {
  const chart = await readFile(resolve(root, 'src/TelemetryHistoryChart.tsx'), 'utf8');
  assert.match(app, /title=\{`\$\{translate\("Uso da VRAM"\)\} · \$\{device\.name\}`\}/);
  assert.match(app, /labels=\{\{ pci_bdf: device\.pci_bdf \}\}/);
  assert.match(app, /vramUsedBytes <= 0 \? translate\("Sem uso"\)/);
  assert.match(chart, /if \(labelsParam !== '\{\}'\) query\.set\('labels', labelsParam\)/);
  assert.equal(localeApp.includes("'Uso da VRAM': 'VRAM usage'") && localeApp.includes("'Sem uso': 'Not in use'"), true);
});

test('overview shows a compact live VRAM bar for each named GPU', () => {
  const overview = app.slice(app.indexOf('function Overview()'), app.indexOf('function HostPage()'));
  assert.match(overview, /usePolling<GpuInventoryResponse>\('\/host\/gpus', 15000\)/);
  assert.match(overview, /className="overview-gpu-vram-list"/);
  assert.match(overview, /<strong>\{device\.name\}<\/strong>/);
  assert.match(overview, /usedBytes <= 0 \? translate\("Sem uso"\)/);
  assert.match(overview, /role=\{percent == null \? 'img' : 'progressbar'\}/);
  assert.match(css, /\.overview-gpu-vram-track\s*\{[^}]*height:\s*7px/);
});

test('application exposes a keyboard skip link and a focusable main landmark', () => {
  assert.match(app, /className="skip-link"\s+href="#main-content"/);
  assert.match(app, /<main\s+id="main-content"\s+tabIndex=\{-1\}/);
  assert.match(css, /\.skip-link:focus\s*\{[^}]*transform:\s*translateY\(0\)/);
});

test('interactive controls keep a visible keyboard focus indicator', () => {
  assert.match(css, /:where\(button, a, input, select, textarea, summary, \[role="button"\], \[tabindex\]:not\(\[tabindex="-1"\]\)\):focus-visible\s*\{[^}]*outline:\s*2px/);
  assert.match(css, /--focus-ring:\s*#[\da-f]{3,8}/i);
});

test('reduced-motion preference disables smooth movement and long animations', () => {
  const reducedMotion = mediaBlock(css, '@media (prefers-reduced-motion: reduce)');
  assert.match(reducedMotion, /scroll-behavior:\s*auto\s*!important/);
  assert.match(reducedMotion, /animation-duration:\s*\.01ms\s*!important/);
  assert.match(reducedMotion, /transition-duration:\s*\.01ms\s*!important/);
});

test('mobile layout uses a bottom safe-area navigation without covering the page end', () => {
  const mobile = mediaBlock(css, '@media (max-width: 767px)');
  assert.match(mobile, /\.mobile-nav\s*\{[^}]*position:\s*fixed[^}]*bottom:\s*0/);
  assert.match(mobile, /\.mobile-nav\s*\{[^}]*env\(safe-area-inset-bottom\)/);
  assert.match(mobile, /\.app-footer\s*\{[^}]*padding:[^}]*78px/);
  assert.match(mobile, /\.mobile-nav-link\s*\{[^}]*min-height:\s*53px/);
});

test('mobile destructive and navigation actions retain touch-sized targets', () => {
  const compact = mediaBlock(css, '@media (max-width: 767px)');
  const narrow = mediaBlock(css, '@media (max-width: 380px)');
  assert.match(compact, /\.vm-row-remove\s*\{[^}]*min-height:\s*44px/);
  assert.match(compact, /\.text-link, \.detail-tabs a, \.empty-state > \.button, \.inline-retry,[\s\S]*?\{\s*min-height:\s*44px/);
  assert.match(narrow, /\.page-content\s*\{[^}]*padding-right:\s*12px[^}]*padding-left:\s*12px/);
});
