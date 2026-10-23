// End-to-end browser test: create a target, start a run, add markers, pause, stop, export the report.
// Also captures screenshots of every page in light and dark themes when SHOTS is set.
// Set E2E_DISRUPT_CMD to a shell command to run right after the "resize started" marker (for example an fsyncLock).
// Usage: BASE=https://127.0.0.1:8443/ BALLAST_TEST_URI=mongodb://... node tests/e2e/run.mjs
import { createRequire } from 'node:module';
import { mkdirSync, statSync } from 'node:fs';
import { spawn } from 'node:child_process';

const require = createRequire(import.meta.url);
let playwright;
try { playwright = require('playwright'); } catch { playwright = await import('/opt/node22/lib/node_modules/playwright/index.mjs'); }
const { chromium } = playwright;

const base = process.env.BASE || 'https://127.0.0.1:8443/';
const uri = process.env.BALLAST_TEST_URI || 'mongodb://rs1:27011,rs2:27012,rs3:27013/?replicaSet=rs0';
const shots = process.env.SHOTS || '';
const out = process.env.OUT || 'test-results';
mkdirSync(out, { recursive: true });
if (shots) mkdirSync(shots, { recursive: true });

const browser = await chromium.launch();
const ctx = await browser.newContext({ ignoreHTTPSErrors: true, viewport: { width: 1440, height: 900 }, locale: 'en-US', acceptDownloads: true });
await ctx.grantPermissions(['clipboard-read', 'clipboard-write'], { origin: new URL(base).origin });
const page = await ctx.newPage();
const problems = [];
const external = [];
page.on('console', (m) => { if (m.type() === 'error') problems.push(`console: ${m.text()}`); });
page.on('pageerror', (e) => problems.push(`pageerror: ${e.message}`));
page.on('request', (r) => { const u = new URL(r.url()); if (!['127.0.0.1', 'localhost'].includes(u.hostname) && u.protocol !== 'data:') external.push(r.url()); });

let failed = 0;
async function step(name, fn) {
  try {
    await fn();
    console.log(`ok   ${name}`);
  } catch (e) {
    failed++;
    console.log(`FAIL ${name}: ${e.message.split('\n')[0]}`);
    await page.screenshot({ path: `${out}/fail-${name.replace(/\W+/g, '-')}.png`, fullPage: true });
  }
}
async function shot(name, full = true) {
  if (!shots) return;
  await page.waitForTimeout(400);
  await page.evaluate(() => document.querySelectorAll('.toast').forEach((t) => t.remove()));
  if (!full) { await page.screenshot({ path: `${shots}/${name}.png` }); return; }
  const height = await page.evaluate(() => document.documentElement.scrollHeight);
  await page.setViewportSize({ width: 1440, height: Math.max(900, height) });
  await page.waitForTimeout(500);
  await page.screenshot({ path: `${shots}/${name}.png` });
  await page.setViewportSize({ width: 1440, height: 900 });
}

let runId = null;
const stamp = Date.now();

await step('runs page shows empty state', async () => {
  await page.goto(base + '#/runs');
  await page.waitForSelector('h1:has-text("Runs")');
  await shot('01-runs-empty');
});

await step('create target and test connection', async () => {
  await page.goto(base + '#/targets');
  await page.waitForSelector('h1:has-text("Targets")');
  if (!(await page.$('#t-name'))) await page.click('.page-head button:has-text("Add target")');
  await page.fill('#t-name', `e2e-${stamp}`);
  await page.fill('#t-uri', uri);
  await page.selectOption('#t-tls', 'off');
  await page.click('.panel button:has-text("Add target")');
  await page.waitForSelector('text=Connected: MongoDB', { timeout: 30000 });
  await shot('06-targets');
});

await step('profiles page lists built-in profiles', async () => {
  await page.goto(base + '#/profiles');
  await page.waitForSelector('text=OLTP steady 1k');
  await page.click('button:has-text("OLTP steady 1k")');
  await page.waitForSelector('#pf-name');
  await shot('07-profiles');
});

await step('new run wizard to review', async () => {
  await page.goto(base + '#/new');
  await page.waitForSelector('#db-name');
  await page.click(`label:has-text("e2e-${stamp}")`);
  await page.fill('#db-name', `ballast_e2e_${stamp}`);
  await shot('02a-new-run-target', false);
  await page.click('text=Next: workload');
  await page.waitForSelector('#rate');
  await page.fill('#rate', '400');
  await shot('02b-new-run-workload', false);
  await page.click('text=Next: schedule');
  await page.waitForSelector('#warmup');
  await page.fill('#warmup', '5');
  await page.fill('#load-docs', '2000');
  await page.fill('#g-err', '90');
  await page.fill('#s-p99', '60000');
  await page.fill('#s-err', '50');
  await shot('02d-new-run-schedule');
  await page.click('text=Next: review');
  await page.waitForSelector('#run-name', { timeout: 30000 });
  await page.fill('#run-name', 'resize rehearsal');
  await page.waitForSelector('text=It will stop by itself if more than 90% of operations fail');
  await shot('02c-new-run-review');
});

await step('start run opens live view', async () => {
  const ack = await page.$('#ack-foreign');
  if (ack) await ack.check();
  await page.click('button:has-text("Start run")');
  await page.waitForURL(/\/live$/, { timeout: 30000 });
  runId = page.url().match(/runs\/(\w+)\/live/)[1];
  await page.waitForSelector('.run-head .chip.running', { timeout: 60000 });
  await page.waitForTimeout(12000);
  await page.waitForSelector('.progress[role="progressbar"]');
  if (!/ops\/s, p99/.test(await page.title())) throw new Error('tab title has no live numbers: ' + (await page.title()));
  await page.waitForSelector('text=Guardrail on: the run stops by itself');
});

await step('add markers with keyboard shortcut', async () => {
  await page.keyboard.press('m');
  await page.waitForSelector('#marker-label');
  await page.fill('#marker-label', 'resize started');
  await page.keyboard.press('Enter');
  await page.waitForSelector('text=Marker: resize started', { timeout: 10000 });
  if (process.env.E2E_DISRUPT_CMD) spawn('sh', ['-c', process.env.E2E_DISRUPT_CMD], { stdio: 'ignore' });
  await page.waitForTimeout(8000);
  await page.keyboard.press('m');
  await page.fill('#marker-label', 'resize finished');
  await page.keyboard.press('Enter');
  await page.waitForSelector('text=Marker: resize finished', { timeout: 10000 });
  await page.waitForTimeout(6000);
  await shot('03-live');
});

await step('runs page shows active run band', async () => {
  await page.goto(base + '#/runs');
  await page.waitForSelector('section[aria-label="Active run"]');
  await shot('01-runs-active', false);
  await page.goto(base + `#/runs/${runId}/live`);
  await page.waitForSelector('.run-head .chip.running');
});

await step('pause and resume with keyboard', async () => {
  await page.keyboard.press('p');
  await page.waitForSelector('.run-head .chip.paused', { timeout: 10000 });
  await page.waitForTimeout(2500);
  await page.keyboard.press('p');
  await page.waitForSelector('.run-head .chip.running', { timeout: 10000 });
  await page.waitForTimeout(6000);
});

await step('stop run shows report', async () => {
  await page.keyboard.press('s');
  await page.click('dialog button:has-text("Stop run")');
  await page.waitForURL(new RegExp(`#/runs/${runId}$`), { timeout: 90000 });
  await page.waitForSelector('text=Impact analysis', { timeout: 30000 });
  const rows = await page.$$eval('.impacts tbody tr', (r) => r.length);
  if (rows < 1) throw new Error('no impact rows');
  await page.click('.impacts button:has-text("Details")');
  await shot('04-report');
});

await step('export html report works offline', async () => {
  const [dl] = await Promise.all([page.waitForEvent('download'), page.click('button:has-text("Export HTML")')]);
  const path = `${out}/report.html`;
  await dl.saveAs(path);
  if (statSync(path).size < 50000) throw new Error('report too small');
  const offline = await ctx.newPage();
  const requests = [];
  offline.on('request', (r) => { if (!r.url().startsWith('file:') && !r.url().startsWith('data:')) requests.push(r.url()); });
  offline.on('pageerror', (e) => problems.push(`export pageerror: ${e.message}`));
  await offline.goto('file://' + (path.startsWith('/') ? path : process.cwd() + '/' + path));
  await offline.waitForSelector('text=Impact analysis');
  if (requests.length) throw new Error('offline report made requests: ' + requests.join(', '));
  if (shots) await offline.screenshot({ path: `${shots}/09-export.png`, fullPage: true });
  await offline.close();
});

await step('csv and json exports', async () => {
  for (const fmt of ['csv', 'json']) {
    const r = await page.request.get(base + `api/runs/${runId}/export.${fmt}`);
    if (r.status() !== 200) throw new Error(`${fmt} export ${r.status()}`);
  }
});

await step('report: pass/fail card, tags and copy summary', async () => {
  await page.goto(base + `#/runs/${runId}`);
  await page.waitForSelector('.panel.slo >> text=Passed');
  await page.click('button:has-text("Add notes or tags")');
  await page.fill('#meta-tags', 'rehearsal, e2e');
  await page.fill('#meta-notes', 'Write stall of 4 s with fsyncLock while markers were open.');
  await page.click('dialog button:has-text("Save")');
  await page.waitForSelector('.meta-bar a.tag:has-text("e2e")');
  await page.click('button:has-text("Copy summary")');
  await page.waitForSelector('.toast:has-text("Summary copied")');
  const md = await page.evaluate(() => navigator.clipboard.readText());
  if (!md.includes('## resize rehearsal') || !md.includes('**SLO: passed**')) throw new Error('clipboard summary missing content');
  await page.evaluate(() => window.scrollTo(0, 0));
  await shot('16-report-slo-tags', false);
});

let rerunId = null;
await step('run again, then compare shows changes', async () => {
  await page.click('button:has-text("Run again")');
  const ack = await page.waitForSelector('dialog[open] button:has-text("Start anyway")', { timeout: 5000 }).catch(() => null);
  if (ack) await ack.click();
  await page.waitForURL(/\/live$/, { timeout: 30000 });
  rerunId = page.url().match(/runs\/(\w+)\/live/)[1];
  if (rerunId === runId) throw new Error('rerun has the same id');
  await page.waitForSelector('.run-head .chip.running', { timeout: 60000 });
  await page.waitForTimeout(15000);
  await page.keyboard.press('s');
  await page.click('dialog button:has-text("Stop run")');
  await page.waitForURL(new RegExp(`#/runs/${rerunId}$`), { timeout: 90000 });
  await page.waitForSelector('.meta-bar a.tag:has-text("e2e")');
  await page.goto(base + `#/compare?ids=${runId},${rerunId}`);
  await page.waitForSelector('h1:has-text("Compare")');
  await page.waitForSelector('td .delta');
  await shot('05-compare');
});

await step('runs list: tag filter is kept in the address', async () => {
  await page.goto(base + '#/runs?tag=e2e');
  await page.waitForSelector('table.data');
  const shown = await page.$$eval('table.data tbody tr', (r) => r.length);
  if (shown !== 2) throw new Error(`expected 2 runs tagged e2e, saw ${shown}`);
  await page.fill('input[data-filter]', 'rehearsal');
  await page.waitForFunction(() => window.location.hash.includes('q=rehearsal') && window.location.hash.includes('tag=e2e'));
  await page.waitForSelector('td .verdict.ok');
});

await step('command palette jumps to a page', async () => {
  await page.goto(base + '#/runs');
  await page.waitForSelector('table.data');
  await page.keyboard.press('Control+k');
  await page.waitForSelector('dialog.palette[open] input');
  await page.keyboard.type('resize');
  await page.waitForSelector('dialog.palette li[aria-selected="true"]');
  await shot('17-command-palette', false);
  await page.keyboard.press('Escape');
  await page.keyboard.press('Control+k');
  await page.keyboard.type('system');
  await page.keyboard.press('Enter');
  await page.waitForURL(/#\/system$/);
  await page.waitForSelector('text=Installed components');
});

await step('settings and system pages', async () => {
  await page.goto(base + '#/settings');
  await page.waitForSelector('h1:has-text("Settings")');
  await shot('08-settings', false);
  await page.goto(base + '#/system');
  await page.waitForSelector('text=Effective configuration');
  await shot('10-system');
});

await step('dark theme and compact density', async () => {
  await page.goto(base + '#/settings');
  await page.click('button:has-text("Dark")');
  await page.click('button:has-text("Compact")');
  if (shots) {
    await page.goto(base + `#/runs/${runId}`);
    await page.waitForSelector('text=Impact analysis');
    await shot('11-report-dark');
    await page.goto(base + '#/runs');
    await page.waitForSelector('table.data');
    await shot('12-runs-dark-compact', false);
  }
  await page.goto(base + '#/settings');
  await page.click('button:has-text("System")');
  await page.click('button:has-text("Comfortable")');
});

await step('keyboard: shortcuts dialog and navigation', async () => {
  await page.goto(base + '#/runs');
  await page.waitForSelector('table.data');
  await page.keyboard.press('?');
  await page.waitForSelector('dialog[open] >> text=Keyboard shortcuts');
  await page.keyboard.press('Escape');
  await page.keyboard.press('n');
  await page.waitForURL(/#\/new$/);
});

if (problems.length) { failed++; console.log('FAIL console problems:\n  ' + problems.join('\n  ')); } else console.log('ok   no console errors');
if (external.length) { failed++; console.log('FAIL external requests: ' + external.join(', ')); } else console.log('ok   no external network requests');
await browser.close();
console.log(failed ? `${failed} failed` : 'all passed');
process.exit(failed ? 1 : 0);
