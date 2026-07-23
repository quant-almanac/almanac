/* ALMANAC Phase 0.5+1 レビュー用: ヘッドレスChrome(CDP)で6幅スクショ+操作検証 */
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');

const CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const OUT = process.argv[2];
// 対象URL: 第2引数 > REVIEW_BASE > :3001。build成果物の検証は `npx next start -p 3001` を推奨
// （devサーバは .next を production ビルドごと書き換えるため、本番復旧前は使わないこと）
const BASE = process.argv[3] || process.env.REVIEW_BASE || 'http://localhost:3001';
fs.mkdirSync(OUT, { recursive: true });

const sleep = ms => new Promise(r => setTimeout(r, ms));

function launch() {
  return new Promise((resolve, reject) => {
    const p = spawn(CHROME, [
      '--headless=new', '--remote-debugging-port=0', '--no-first-run',
      '--no-default-browser-check', '--hide-scrollbars',
      // レビュー専用: 3002→8000 のCORSを回避（隔離プロファイルのため安全）
      '--disable-web-security',
      '--user-data-dir=' + path.join(OUT, 'chrome-profile'),
    ]);
    let buf = '';
    p.stderr.on('data', d => {
      buf += d;
      const m = buf.match(/DevTools listening on (ws:\/\/\S+)/);
      if (m) resolve({ proc: p, wsUrl: m[1] });
    });
    setTimeout(() => reject(new Error('chrome launch timeout\n' + buf)), 15000);
  });
}

async function main() {
  const { proc, wsUrl } = await launch();
  const ws = new WebSocket(wsUrl);
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });

  let nextId = 1;
  const pending = new Map();
  const exceptions = [];
  ws.onmessage = ev => {
    const msg = JSON.parse(ev.data);
    if (msg.id && pending.has(msg.id)) {
      const { res, rej } = pending.get(msg.id);
      pending.delete(msg.id);
      msg.error ? rej(new Error(JSON.stringify(msg.error))) : res(msg.result);
    } else if (msg.method === 'Runtime.exceptionThrown') {
      exceptions.push(msg.params?.exceptionDetails?.exception?.description || JSON.stringify(msg.params).slice(0, 300));
    }
  };
  const send = (method, params = {}, sessionId) => new Promise((res, rej) => {
    const id = nextId++;
    pending.set(id, { res, rej });
    ws.send(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) }));
  });

  const { targetId } = await send('Target.createTarget', { url: 'about:blank' });
  const { sessionId } = await send('Target.attachToTarget', { targetId, flatten: true });
  await send('Page.enable', {}, sessionId);
  await send('Runtime.enable', {}, sessionId);

  const evalJs = async expr => {
    const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true }, sessionId);
    return r.result ? r.result.value : undefined;
  };
  const setSize = (w, h) => send('Emulation.setDeviceMetricsOverride',
    { width: w, height: h, deviceScaleFactor: 1, mobile: w < 600 }, sessionId);
  const waitFor = async (expr, timeout = 25000) => {
    const t0 = Date.now();
    while (Date.now() - t0 < timeout) {
      try { if (await evalJs(expr)) return true; } catch {}
      await sleep(500);
    }
    return false;
  };
  const shot = async (name, full = false) => {
    const r = await send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: full }, sessionId);
    fs.writeFileSync(path.join(OUT, name + '.png'), Buffer.from(r.data, 'base64'));
    console.log('shot', name);
  };
  const metrics = async label => {
    const m = await evalJs(`(() => {
      const doc = document.documentElement;
      const layout = document.querySelector('.almanac-layout');
      const shell = document.querySelector('.ops-shell-content');
      return {
        label: ${JSON.stringify('')} + ${JSON.stringify('')},
        innerWidth: window.innerWidth,
        scrollWidth: doc.scrollWidth,
        clientWidth: doc.clientWidth,
        pageHScroll: doc.scrollWidth > doc.clientWidth + 1,
        shellMaxWidth: shell ? getComputedStyle(shell).maxWidth : null,
        shellActualWidth: shell ? shell.getBoundingClientRect().width : null,
        almanacCols: layout ? getComputedStyle(layout).gridTemplateColumns : null,
      };
    })()`);
    m.label = label;
    return m;
  };

  const allMetrics = [];

  // ── ホーム: ロード → 幅を変えながら計測（リロード不要: メディア/コンテナクエリは即応） ──
  await setSize(1440, 1000);
  await send('Page.navigate', { url: BASE + '/' }, sessionId);
  const ready = await waitFor(`document.body.innerText.includes('今週の計画')`);
  console.log('home ready:', ready);
  await sleep(1200);

  for (const [w, h] of [[390, 844], [1024, 900], [1440, 1000], [1920, 1080], [2560, 1200], [3840, 1400]]) {
    await setSize(w, h);
    await sleep(700);
    allMetrics.push(await metrics(`home-${w}`));
    await shot(`home-${w}`);
  }
  // 全体像（1440 フルページ）
  await setSize(1440, 1000);
  await sleep(500);
  await shot('home-1440-full', true);

  // ── レール操作: 予定 / 記録 / 計画全文モーダル ──
  const clickByText = (selector, text) => evalJs(
    `(() => { const el = [...document.querySelectorAll('${selector}')].find(e => e.textContent.trim().includes('${text}')); if (el) { el.click(); return true; } return false; })()`
  );
  console.log('tab schedule:', await clickByText('[role="tab"]', '予定')); await sleep(600); await shot('home-1440-tab-schedule');
  console.log('tab record:', await clickByText('[role="tab"]', '記録')); await sleep(600); await shot('home-1440-tab-record');
  console.log('tab plan:', await clickByText('[role="tab"]', '計画')); await sleep(400);
  console.log('modal:', await clickByText('button', '計画の全文')); await sleep(800); await shot('home-1440-plan-modal');
  await evalJs(`document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))`);

  // ── 検証ページ: 主判定カードを同じ6幅で確認 ──
  await send('Page.navigate', { url: BASE + '/performance' }, sessionId);
  await waitFor(`document.body.innerText.includes('365日目標判定')`);
  await sleep(1000);
  for (const [w, h] of [[390, 844], [1024, 900], [1440, 1000], [1920, 1080], [2560, 1200], [3840, 1400]]) {
    await setSize(w, h);
    await sleep(500);
    allMetrics.push(await metrics(`performance-${w}`));
    await shot(`performance-${w}`);
  }

  // ── 他ページ ──
  const pages = [
    ['/portfolio', 'portfolio'], ['/nisa', 'nisa'], ['/risk', 'risk'], ['/screening', 'screening'],
    ['/margin', 'margin'], ['/executions', 'executions'], ['/decision', 'decision'], ['/agent', 'agent'],
  ];
  const reviewWidths = [[390, 844], [1024, 900], [1440, 1000], [1920, 1080], [2560, 1200], [3840, 1400]];
  for (const [p2, pageName] of pages) {
    await send('Page.navigate', { url: BASE + p2 }, sessionId);
    await waitFor(`document.body.innerText.length > 150`);
    await sleep(1000);
    for (const [w, h] of reviewWidths) {
      await setSize(w, h);
      await sleep(400);
      const name = `${pageName}-${w}`;
      allMetrics.push(await metrics(name));
      await shot(name);
    }
  }

  fs.writeFileSync(path.join(OUT, 'metrics.json'), JSON.stringify({ metrics: allMetrics, exceptions }, null, 2));
  console.log(JSON.stringify({ metrics: allMetrics, exceptions }, null, 1));

  ws.close();
  proc.kill();
}

main().catch(e => { console.error('FATAL', e); process.exit(1); });
