"use strict";
// Driver for agent-chat-bench.html (docs/agent-chat.md §16.10). Serve the repository root, then:
//   node docs/mockups/agent-chat-bench.cjs http://127.0.0.1:8931/docs/mockups/agent-chat-bench.html 16000 0 [&ui=<dir>|&bigmsg=1500|...]
// Prints per 1000 events: wall time, main-thread task / layout / style / script time per event, CDP node count, heap
// and long tasks; then the cost of 5 s idle (IDLE=ms to change it) and the document's size.
const { chromium } = require('playwright');
const [,, base, live = '6000', hist = '0', extra = ''] = process.argv;
const ui = (extra.match(/ui=([^&]+)/) || [])[1] || 'repo';
(async () => {
  const browser = await chromium.launch({ args: ['--enable-precise-memory-info', '--js-flags=--expose-gc'] });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  page.on('pageerror', (e) => console.log('PAGEERROR', e.message));
  const cdp = await page.context().newCDPSession(page);
  await cdp.send('Performance.enable');
  await page.addInitScript(() => { window.__lt = { n: 0, ms: 0, max: 0 }; new PerformanceObserver((l) => { for (const e of l.getEntries()) { __lt.n++; __lt.ms += e.duration; __lt.max = Math.max(__lt.max, e.duration); } }).observe({ entryTypes: ['longtask'] }); });
  const url = `${base}?live=${live}&hist=${hist}${extra}`;
  const start = Date.now();
  await page.goto(url);
  await page.waitForFunction(() => window.__bench && window.__bench.ready, null, { timeout: 120000 });
  const metric = async () => Object.fromEntries((await cdp.send('Performance.getMetrics')).metrics.map((m) => [m.name, m.value]));
  const openMs = await page.evaluate(() => __bench.openMs);
  let m0 = await metric();
  const lt0 = await page.evaluate(() => ({ ...__lt }));
  console.log(`ui=${ui} hist=${hist} live=${live} ${extra} open=${openMs.toFixed(0)}ms nodes=${m0.Nodes} heap=${(m0.JSHeapUsedSize/1e6).toFixed(1)}MB longtasks(open) n=${lt0.n} max=${lt0.max.toFixed(0)}ms`);
  const step = 1000; let next = step; let prev = { d: 0, sync: 0, m: m0, lt: lt0, t: Date.now() };
  while (Number(live) > 0) {
    const b = await page.evaluate(() => ({ d: __bench.delivered, sync: __bench.syncMs, done: __bench.done, lt: { ...__lt } }));
    if (b.d >= next || b.done) {
      const m = await metric();
      const dE = b.d - prev.d || 1;
      console.log([`ev=${b.d}`, `wall/1k=${((Date.now()-prev.t)/dE*1000/1000).toFixed(1)}s`, `sync/ev=${((b.sync-prev.sync)/dE).toFixed(2)}ms`,
        `script/ev=${((m.ScriptDuration-prev.m.ScriptDuration)*1000/dE).toFixed(2)}ms`, `layout/ev=${((m.LayoutDuration-prev.m.LayoutDuration)*1000/dE).toFixed(2)}ms`,
        `style/ev=${((m.RecalcStyleDuration-prev.m.RecalcStyleDuration)*1000/dE).toFixed(2)}ms`, `task/ev=${((m.TaskDuration-prev.m.TaskDuration)*1000/dE).toFixed(2)}ms`,
        `nodes=${m.Nodes}`, `heap=${(m.JSHeapUsedSize/1e6).toFixed(1)}MB`, `lt n=${b.lt.n-prev.lt.n} max=${b.lt.max.toFixed(0)}`].join(' '));
      prev = { d: b.d, sync: b.sync, m, lt: b.lt, t: Date.now() }; next += step;
      if (b.done) break;
    }
    await new Promise((r) => setTimeout(r, 200));
  }
  // idle cost: 5 s with nothing arriving (conversation ticks, timers)
  const mi = await metric(); await new Promise((r) => setTimeout(r, Number(process.env.IDLE||5000))); const mj = await metric();
  console.log(`idle task=${((mj.TaskDuration-mi.TaskDuration)*1000).toFixed(0)}ms script=${((mj.ScriptDuration-mi.ScriptDuration)*1000).toFixed(0)}ms layout=${((mj.LayoutDuration-mi.LayoutDuration)*1000).toFixed(0)}ms nodes=${mj.Nodes} listeners=${mj.JSEventListeners} docNodes=${await page.evaluate(() => document.querySelectorAll('*').length)} streamKids=${await page.evaluate(() => document.querySelector('.chat-stream').children.length)} lt=${JSON.stringify(await page.evaluate(() => __lt))}`);
  await page.evaluate(() => window.gc && gc());
  const mf = await metric();
  console.log(`afterGC heap=${(mf.JSHeapUsedSize/1e6).toFixed(1)}MB nodes=${mf.Nodes} docs=${mf.Documents} total=${((Date.now()-start)/1000).toFixed(0)}s`);
  await browser.close();
})();
