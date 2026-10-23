// Development helper for simulate-fresh-host.sh: open Ballast in headless Chromium through the access gate.
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
let pw;
try { pw = require('playwright'); } catch { pw = await import('/opt/node22/lib/node_modules/playwright/index.mjs'); }
const base = process.env.BASE;
const b = await pw.chromium.launch();
const ctx = await b.newContext({ ignoreHTTPSErrors: true, locale: 'en-US', viewport: { width: 1440, height: 900 } });
const p = await ctx.newPage();
await p.goto(base);
console.log('redirected to', new URL(p.url()).pathname);
await p.fill('#token', 'wrong-token');
await p.click('button[type=submit]');
console.log('wrong token shows:', (await p.textContent('.gate-error')).trim());
await p.fill('#token', process.env.TOKEN);
await p.click('button[type=submit]');
await p.waitForSelector('h1:has-text("Runs")');
const cookie = (await ctx.cookies()).find((c) => c.name === 'ballast_session');
console.log('runs page loaded; cookie httpOnly', cookie.httpOnly, 'secure', cookie.secure, 'sameSite', cookie.sameSite);
const rows = await p.$$eval('table.data tbody tr', (r) => r.length);
console.log('runs listed:', rows);
await b.close();
