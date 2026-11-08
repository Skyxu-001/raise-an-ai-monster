// Small helper for recording demo videos: drives headless Edge over CDP, with requestAnimationFrame / performance.now replaced by a virtual clock.
// Each frame we advance 1/30 s and then take a screenshot, so 3D animation and turntables are exactly 30 fps no matter how slow screenshots are.
// Frames are written as JPEG, plus an ffmpeg concat list (display time per frame). A still image stores one frame with a long duration.
import { writeFileSync, mkdirSync, appendFileSync } from "fs";

export const FPS = 30;
const VCLOCK = `(() => {
  let cbs = new Map(), id = 0, vnow = 0;
  const realNow = performance.now.bind(performance);
  const base = realNow();
  performance.now = () => base + vnow;
  window.requestAnimationFrame = cb => { cbs.set(++id, cb); return id; };
  window.cancelAnimationFrame = i => { cbs.delete(i); };
  window.__step = ms => {
    vnow += ms;
    const list = [...cbs.values()]; cbs = new Map();
    for (const cb of list) { try { cb(base + vnow); } catch (e) { console.error(e); } }
    return list.length;
  };
})();`;

export async function open(port, url, { width = 1280, height = 720, scale = 1.5 } = {}) {
  const t = await (await fetch(`http://127.0.0.1:${port}/json/new?about:blank`, { method: "PUT" })).json();
  const ws = new WebSocket(t.webSocketDebuggerUrl);
  let id = 0; const pending = new Map(); const errors = [];
  ws.onmessage = e => { const m = JSON.parse(e.data);
    if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
    if (m.method === "Runtime.exceptionThrown") errors.push(m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text);
    if (m.method === "Runtime.consoleAPICalled" && ["error"].includes(m.params.type)) errors.push(m.params.args.map(a => a.value ?? a.description).join(" ")); };
  await new Promise(r => ws.onopen = r);
  const send = (method, params = {}) => new Promise(r => { const i = ++id; pending.set(i, r); ws.send(JSON.stringify({ id: i, method, params })); });
  await send("Runtime.enable"); await send("Page.enable");
  await send("Emulation.setDeviceMetricsOverride", { width, height, deviceScaleFactor: scale, mobile: false });
  await send("Page.addScriptToEvaluateOnNewDocument", { source: VCLOCK });
  await send("Page.navigate", { url });
  const ev = async (x, awaitPromise = true) => {
    const r = await send("Runtime.evaluate", { expression: x, returnByValue: true, awaitPromise });
    if (r.result?.exceptionDetails) throw new Error("eval failed: " + x.slice(0, 80) + " → " + JSON.stringify(r.result.exceptionDetails).slice(0, 400));
    return r.result?.result?.value;
  };
  return { send, ev, errors, close: () => send("Target.closeTarget", { targetId: t.id }).catch(() => {}) };
}

// One scene = one folder + a concat list
export function scene(page, dir) {
  mkdirSync(dir, { recursive: true });
  const list = `${dir}/list.txt`;
  writeFileSync(list, "");
  let n = 0, last = null, total = 0;
  const self = {
    frames: () => n,
    elapsed: () => total,
    // Advance dtFrames frames of virtual time (no screenshots) so the page gets drawn first
    async step(frames = 1) { for (let i = 0; i < frames; i++) await page.ev(`window.__step(${1000 / FPS})`); },
    // Capture one frame, shown for dur seconds
    async shot(dur = 1 / FPS, { step = true } = {}) {
      if (step) await page.ev(`window.__step(${1000 / FPS})`);
      const r = await page.send("Page.captureScreenshot", { format: "jpeg", quality: 92 });
      const f = `f${String(n++).padStart(5, "0")}.jpg`;
      writeFileSync(`${dir}/${f}`, Buffer.from(r.result.data, "base64"));
      appendFileSync(list, `file '${f}'\nduration ${dur.toFixed(5)}\n`);
      last = f;
      total += dur;
    },
    // Continuous animation for sec seconds (each frame advances 1/30 s)
    async run(sec, each) { const k = Math.round(sec * FPS); for (let i = 0; i < k; i++) { if (each) await each(i, k); await self.shot(); } },
    // Finish: the concat demuxer needs the last frame written once more
    end() { if (last) appendFileSync(list, `file '${last}'\n`); return list; },
  };
  return self;
}

export const sleep = ms => new Promise(r => setTimeout(r, ms));
export async function until(page, expr, { timeout = 600000, every = 1000, label = expr } = {}) {
  const t0 = Date.now();
  for (;;) {
    const v = await page.ev(expr).catch(() => null);
    if (v) return v;
    if (Date.now() - t0 > timeout) throw new Error("timeout waiting for " + label);
    await page.ev("window.__step(33)").catch(() => {});
    await sleep(every);
  }
}
