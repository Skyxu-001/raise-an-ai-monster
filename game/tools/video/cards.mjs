// Records the title and end cards: cards.html is driven frame by frame through at(t).
//   node cards.mjs <port> <out dir>     VO_TIMES=<json of scene -> seconds> makes the cards fit their narration
import { readFileSync } from "fs";
import { fileURLToPath } from "url";
import { open, scene, sleep, FPS } from "./rec-lib.mjs";
const [port, out] = process.argv.slice(2);
const VO = process.env.VO_TIMES ? JSON.parse(readFileSync(process.env.VO_TIMES, "utf8")) : {};
const here = fileURLToPath(new URL(".", import.meta.url));
for (const [name, t, sec] of [["00-title", "title", Math.max(3.5, (VO.title || 0) + 0.4)], ["99-end", "end", Math.max(5, (VO.end || 0) + 2.2)]]) {
  const page = await open(port, `file://${here}cards.html?t=${t}`);
  await sleep(1200);
  const s = scene(page, `${out}/${name}`);
  const k = Math.round(sec * FPS);
  for (let i = 0; i < k; i++) { await page.ev(`at(${i / FPS})`); await s.shot(); }
  s.end(); await page.close();
  console.log(name, s.frames());
}
process.exit(0);
