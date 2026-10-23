// Browser test for Atlas analytics nodes: route analytics queries to nodeType:ANALYTICS, restart the
// analytics node during the run, and check the live member panel and the report's member and route figures.
// Needs the replica set from scripts/dev-replset-atlas.sh.
// Usage: BASE=https://127.0.0.1:8443/ ATLAS_URI=mongodb://ra1:27041,.../?replicaSet=rs0 E2E_DISRUPT_CMD='docker restart ra4' SHOTS=docs/design/after node tests/e2e/atlas.mjs
import { createRequire } from 'node:module';
import { mkdirSync } from 'node:fs';
import { spawn } from 'node:child_process';

const require = createRequire(import.meta.url);
let playwright;
try { playwright = require('playwright'); } catch { playwright = await import('/opt/node22/lib/node_modules/playwright/index.mjs'); }
const { chromium } = playwright;

const base = process.env.BASE || 'https://127.0.0.1:8443/';
const uri = process.env.ATLAS_URI || 'mongodb://ra1:27041,ra2:27042,ra3:27043/?replicaSet=rs0';
const analytics = process.env.ATLAS_ANALYTICS || 'ra4:27044';
const shots = process.env.SHOTS || '';
const out = process.env.OUT || 'test-results';
mkdirSync(out, { recursive: true });

const browser = await chromium.launch();
const ctx = await browser.newContext({ ignoreHTTPSErrors: true, viewport: { width: 1440, height: 900 }, locale: 'en-US' });
const page = await ctx.newPage();
const problems = [];
page.on('console', (m) => { if (m.type() === 'error') problems.push(`console: ${m.text()}`); });
page.on('pageerror', (e) => problems.push(`pageerror: ${e.message}`));

let failed = 0;
async function step(name, fn) {
  try { await fn(); console.log(`ok   ${name}`); } catch (e) {
    failed++;
    console.log(`FAIL ${name}: ${e.message.split('\n')[0]}`);
    await page.screenshot({ path: `${out}/fail-atlas-${name.replace(/\W+/g, '-')}.png`, fullPage: true });
  }
}
async function shot(name, full = true) {
  if (!shots) return;
  await page.waitForTimeout(400);
  await page.evaluate(() => document.querySelectorAll('.toast').forEach((t) => t.remove()));
  if (full) {
    const height = await page.evaluate(() => document.documentElement.scrollHeight);
    await page.setViewportSize({ width: 1440, height: Math.max(900, height) });
    await page.waitForTimeout(500);
  }
  await page.screenshot({ path: `${shots}/${name}.png` });
  await page.setViewportSize({ width: 1440, height: 900 });
}

const stamp = Date.now();
let runId = null;

await step('target shows Atlas node types', async () => {
  await page.goto(base + '#/targets');
  await page.waitForSelector('h1:has-text("Targets")');
  if (!(await page.$('#t-name'))) await page.click('.page-head button:has-text("Add target")');
  await page.fill('#t-name', `atlas-like-${stamp}`);
  await page.fill('#t-uri', uri);
  await page.selectOption('#t-tls', 'off');
  await page.click('.panel button:has-text("Add target")');
  await page.waitForSelector('text=Connected: MongoDB', { timeout: 30000 });
  await page.waitForSelector('.node-type:has-text("ANALYTICS")');
  await page.waitForSelector('.node-type:has-text("READ_ONLY")');
  await shot('21-target-node-types', false);
});

await step('wizard routes analytics queries and lists the members they reach', async () => {
  await page.goto(base + '#/new');
  await page.waitForSelector('#db-name');
  await page.click(`label:has-text("atlas-like-${stamp}")`);
  await page.fill('#db-name', `ballast_atlas_e2e_${stamp}`);
  await page.click('text=Next: workload');
  await page.waitForSelector('#profile-select');
  const id = await page.$eval('#profile-select', (s) => [...s.options].find((o) => o.text === 'Atlas analytics nodes').value);
  await page.selectOption('#profile-select', id);
  await page.waitForSelector('#route-preset');
  if ((await page.$eval('#route-preset', (s) => s.value)) !== 'analytics') throw new Error('profile did not select the analytics preset');
  await page.fill('#rate', '200');
  await page.locator('section:has-text("Where reads go")').scrollIntoViewIfNeeded();
  await shot('18-new-run-routing', false);
  await page.click('text=Next: schedule');
  await page.fill('#warmup', '3');
  await page.fill('#load-docs', '3000');
  await page.click('text=Next: review');
  await page.waitForSelector('section[aria-label="Read routes"]', { timeout: 30000 });
  const reach = await page.$eval('section[aria-label="Read routes"]', (s) => s.textContent);
  if (!reach.includes(analytics)) throw new Error('route table does not list the analytics node: ' + reach);
  await page.fill('#run-name', 'analytics tier resize');
});

await step('live view shows member types and what each serves', async () => {
  const ack = await page.$('#ack-foreign');
  if (ack) await ack.check();
  await page.click('button:has-text("Start run")');
  await page.waitForURL(/\/live$/, { timeout: 30000 });
  runId = page.url().match(/runs\/(\w+)\/live/)[1];
  await page.waitForSelector('.run-head .chip.running', { timeout: 60000 });
  await page.waitForSelector('.member .node-type:has-text("ANALYTICS")', { timeout: 20000 });
  await page.waitForFunction((a) => [...document.querySelectorAll('.member')].some((m) => m.textContent.includes(a) && /ops\/s served/.test(m.textContent)), analytics, { timeout: 20000 });
  await page.waitForTimeout(8000);
  await page.keyboard.press('m');
  await page.fill('#marker-label', 'analytics resize started');
  await page.keyboard.press('Enter');
  if (process.env.E2E_DISRUPT_CMD) spawn('sh', ['-c', process.env.E2E_DISRUPT_CMD], { stdio: 'ignore' });
  await page.waitForFunction((a) => [...document.querySelectorAll('.member.down')].some((m) => m.textContent.includes(a)), analytics, { timeout: 30000 }).catch(() => {});
  await shot('19-live-members');
  await page.waitForTimeout(14000);
  await page.keyboard.press('m');
  await page.fill('#marker-label', 'analytics resize finished');
  await page.keyboard.press('Enter');
  await page.waitForTimeout(10000);
});

await step('report shows the analytics gap and reads per member', async () => {
  await page.keyboard.press('s');
  await page.click('dialog button:has-text("Stop run")');
  await page.waitForURL(new RegExp(`#/runs/${runId}$`), { timeout: 90000 });
  await page.waitForSelector('section[aria-label="Members"] .node-type:has-text("ANALYTICS")', { timeout: 30000 });
  await page.click('.impacts button:has-text("Details")');
  await page.waitForSelector('th:has-text("Longest ANALYTICS read gap")');
  await page.waitForSelector('.impact-members');
  await page.waitForSelector('text=Primary and member changes');
  await shot('20-report-members');
});

if (problems.length) { failed++; console.log('FAIL console problems:\n  ' + problems.join('\n  ')); } else console.log('ok   no console errors');
await browser.close();
console.log(failed ? `${failed} failed` : 'all passed');
process.exit(failed ? 1 : 0);
