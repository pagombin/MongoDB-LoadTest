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
  await page.click('text=Next: review');
  await page.waitForSelector('#run-name', { timeout: 30000 });
  await page.fill('#run-name', 'resize rehearsal');
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

await step('compare page', async () => {
  await page.goto(base + `#/compare?ids=${runId}`);
  await page.waitForSelector('h1:has-text("Compare")');
  await shot('05-compare', false);
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
