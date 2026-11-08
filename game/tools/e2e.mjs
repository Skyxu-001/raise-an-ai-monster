// Run through the real game page: set player → start battle → wait for the 3D stage to load → first move → burst of screenshots. Usage: node e2e.mjs <Edge debug port> <game url> <player> <opponent id> <output prefix> (this really plays a turn, so use a test account)
import { writeFileSync } from "fs";
const [port, url, player, foe, out] = process.argv.slice(2);
const t = await (await fetch(`http://127.0.0.1:${port}/json/new?${encodeURIComponent(url)}`, { method: "PUT" })).json();
const ws = new WebSocket(t.webSocketDebuggerUrl);
let id = 0; const pending = new Map(); const errors = [];
ws.onmessage = e => { const m = JSON.parse(e.data);
  if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
  if (m.method === "Runtime.exceptionThrown") errors.push(m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text);
  if (m.method === "Runtime.consoleAPICalled" && ["error", "warning"].includes(m.params.type)) errors.push(m.params.args.map(a => a.value ?? a.description).join(" ")); };
await new Promise(r => ws.onopen = r);
const send = (method, params = {}) => new Promise(r => { const i = ++id; pending.set(i, r); ws.send(JSON.stringify({ id: i, method, params })); });
const ev = async (x, awaitPromise = false) => (await send("Runtime.evaluate", { expression: x, returnByValue: true, awaitPromise })).result?.result?.value;
const sleep = ms => new Promise(r => setTimeout(r, ms));
const shot = async name => { const s = await send("Page.captureScreenshot", { format: "png" }); writeFileSync(`${out}_${name}.png`, Buffer.from(s.result.data, "base64")); };
const until = async (x, ms = 60000) => { const t0 = Date.now(); while (!(await ev(x))) { if (Date.now() - t0 > ms) return false; await sleep(300); } return true; };
await send("Runtime.enable");
await send("Emulation.setDeviceMetricsOverride", { width: 1280, height: 1000, deviceScaleFactor: 1, mobile: false });
await sleep(1500);
await ev(`localStorage.setItem("monster_player", ${JSON.stringify(player)}); location.reload()`);
await sleep(2500);
console.log("battle open already:", await ev(`!document.querySelector("#battleCard").classList.contains("hidden")`));
if (!(await ev(`!document.querySelector("#battleCard").classList.contains("hidden")`))) {
  console.log("fight button:", await ev(`!!document.querySelector('[data-fight="${foe}"]')`));
  await ev(`document.querySelector('[data-fight="${foe}"]').click()`);
}
console.log("stage canvas:", await until(`!!document.querySelector("#stage canvas")`));
console.log("models loaded:", await until(`!document.querySelector("#stage .loading") && !!document.querySelector("#stage canvas")`, 90000));
await ev(`document.querySelector("#battleCard").scrollIntoView()`);
await sleep(800); await shot("0_ready");
const skills = await ev(`[...document.querySelectorAll("#bskills button")].map(b => b.textContent)`);
console.log("skills:", JSON.stringify(skills));
await ev(`document.querySelector('#bskills button[data-move="0"]').click()`);
for (let i = 1; i <= 12; i++) { await sleep(350); await shot(String(i).padStart(2, "0")); }
await until(`!document.querySelector('#bskills button[disabled]')`, 20000);
console.log("log:", JSON.stringify(await ev(`[...document.querySelectorAll("#blog p")].map(p => p.textContent)`)));
console.log("hp:", JSON.stringify(await ev(`[...document.querySelectorAll(".hpnum")].map(p => p.textContent)`)));
await send("Target.closeTarget", { targetId: t.id }).catch(() => {});
console.log("errors:", JSON.stringify(errors));
process.exit(0);
