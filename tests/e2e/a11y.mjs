// Accessibility scan with axe-core (WCAG 2.2 A and AA rules) on every page, light and dark.
// Usage: AXE=path/to/axe.min.js BASE=https://127.0.0.1:8443/ node tests/e2e/a11y.mjs
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
const require = createRequire(import.meta.url);
let pw;
try { pw = require('playwright'); } catch { pw = await import('/opt/node22/lib/node_modules/playwright/index.mjs'); }
const base = process.env.BASE || 'https://127.0.0.1:8443/';
const axe = readFileSync(process.env.AXE || require.resolve('axe-core/axe.min.js'), 'utf8');
const b = await pw.chromium.launch();
const ctx = await b.newContext({ ignoreHTTPSErrors: true, bypassCSP: true, locale: 'en-US', viewport: { width: 1440, height: 900 } });
const p = await ctx.newPage();
const runs = await (await p.request.get(base + 'api/runs')).json();
const runId = runs.runs[0] && runs.runs[0].id;
const pages = ['#/runs', '#/new', '#/targets', '#/profiles', '#/compare', '#/settings', '#/system'];
if (runId) pages.push(`#/runs/${runId}`);
let total = 0;
for (const theme of ['light', 'dark']) {
  await p.goto(base + '#/settings');
  await p.evaluate((t) => localStorage.setItem('ballast.theme', t), theme);
  for (const route of pages) {
    await p.goto(base + route);
    await p.reload();
    await p.waitForTimeout(1800);
    if (route === '#/profiles') { const btn = await p.$('table.data button.btn'); if (btn) { await btn.click(); await p.waitForTimeout(500); } }
    await p.addScriptTag({ content: axe });
    const result = await p.evaluate(async () => await window.axe.run(document, { runOnly: { type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa'] } }));
    const v = result.violations;
    total += v.length;
    console.log(`${theme.padEnd(5)} ${route.padEnd(22)} ${v.length ? 'VIOLATIONS ' + v.length : 'ok'} (${result.passes.length} rules passed)`);
    for (const x of v) console.log(`      ${x.id} [${x.impact}] ${x.help} :: ${x.nodes.slice(0, 3).map((n) => n.target.join(' ')).join(' | ')}`);
  }
}
await p.evaluate(() => localStorage.removeItem('ballast.theme'));
await b.close();
console.log(total ? `${total} violations` : 'no violations');
process.exit(total ? 1 : 0);
