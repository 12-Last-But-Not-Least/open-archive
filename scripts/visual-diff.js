import fs from 'node:fs';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import {PNG} from 'pngjs';
import pixelmatch from 'pixelmatch';

const MAX_PIXELS = 20_000_000;
const slug = /^[a-z0-9][a-z0-9-]{0,119}$/;
function inventory(root) {
  const images = new Map();
  for (const device of ['desktop', 'mobile']) {
    const dir = path.join(root, device);
    if (!fs.existsSync(dir)) continue;
    for (const entry of fs.readdirSync(dir, {withFileTypes: true})) {
      if (!entry.isFile() || !entry.name.endsWith('.png')) continue;
      const name = entry.name.slice(0, -4);
      if (!slug.test(name)) throw new Error('Invalid screenshot name');
      images.set(`${device}/${name}`, path.join(dir, entry.name));
    }
  }
  if (!images.size || images.size > 300) throw new Error('Missing or excessive screenshot inventory');
  return images;
}
function readBytes(file) {
  const bytes = fs.readFileSync(file);
  if (bytes.length < 24 || bytes.length > 15_000_000 || bytes.subarray(0, 8).toString('hex') !== '89504e470d0a1a0a')
    throw new Error('Invalid or oversized PNG');
  if (bytes.readUInt32BE(16) * bytes.readUInt32BE(20) > MAX_PIXELS) throw new Error('PNG dimensions too large');
  return bytes;
}
function pad(image, width, height) {
  const output = new PNG({width, height});
  output.data.fill(255);
  if (image) PNG.bitblt(image, output, 0, 0, image.width, image.height, 0, 0);
  return output;
}

export function compare(beforeDir, afterDir, outputDir) {
  const before = inventory(beforeDir), after = inventory(afterDir);
  fs.mkdirSync(outputDir, {recursive: true});
  const report = {total: 0, unchanged: 0, changes: []};
  for (const key of [...new Set([...before.keys(), ...after.keys()])].sort()) {
    report.total++;
    const oldBytes = before.has(key) ? readBytes(before.get(key)) : null;
    const currentBytes = after.has(key) ? readBytes(after.get(key)) : null;
    // Most PRs leave most screens untouched. Avoid decoding/comparing identical PNGs.
    if (oldBytes && currentBytes && oldBytes.equals(currentBytes)) { report.unchanged++; continue; }
    const old = oldBytes ? PNG.sync.read(oldBytes) : null;
    const current = currentBytes ? PNG.sync.read(currentBytes) : null;
    const width = Math.max(old?.width || 0, current?.width || 0);
    const height = Math.max(old?.height || 0, current?.height || 0);
    if (width * height > MAX_PIXELS) throw new Error('Combined canvas too large');
    const a = pad(old, width, height), b = pad(current, width, height);
    const diff = new PNG({width, height});
    const pixels = pixelmatch(a.data, b.data, diff.data, width, height,
      {threshold: 0.1, includeAA: false, diffColor: [255, 0, 80], alpha: 0.25});
    const resized = old?.width !== current?.width || old?.height !== current?.height;
    if (!pixels && !resized) { report.unchanged++; continue; }
    const [device, name] = key.split('/');
    const id = `${device}--${name}`;
    const status = !old ? 'added' : !current ? 'removed' : resized ? 'resized' : 'changed';
    for (const [side, image] of [['before', a], ['after', b], ['diff', diff]])
      fs.writeFileSync(path.join(outputDir, `${id}-${side}.png`), PNG.sync.write(image));
    report.changes.push({id, device, name, status, pixels, percent: Number((100 * pixels / (width * height)).toFixed(2))});
  }
  report.changes.sort((a, b) => b.pixels - a.pixels || a.id.localeCompare(b.id));
  fs.writeFileSync(path.join(outputDir, 'report.json'), JSON.stringify(report, null, 2));
  const cards = report.changes.map(c => `<section><h2>${c.name} — ${c.device}</h2><p>${c.status}: ${c.pixels} changed pixels (${c.percent}%). Pink marks differences. Blank panels represent missing screenshots.</p><div>${['before', 'after', 'diff'].map(side => `<figure><figcaption>${side}</figcaption><a href="${c.id}-${side}.png"><img src="${c.id}-${side}.png" loading="lazy" alt="${side}"></a></figure>`).join('')}</div></section>`).join('');
  fs.writeFileSync(path.join(outputDir, 'index.html'), `<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Visual changes</title><style>body{font:16px system-ui;margin:24px;background:#fafafa}section{margin:32px 0}section div{display:flex;gap:12px}figure{margin:0;flex:1;min-width:0}img{width:100%;border:1px solid #ccc}figcaption{font-weight:bold;margin:8px 0}@media(max-width:700px){section div{display:block}}</style><h1>Visual changes</h1><p>${report.changes.length} changed; ${report.unchanged} unchanged; ${report.total} compared.</p>${cards || '<p>No visual changes detected in covered screenshots.</p>'}</html>`);
  return report;
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  if (process.argv.length !== 5) throw new Error('Usage: node scripts/visual-diff.js BEFORE AFTER OUTPUT');
  const r = compare(...process.argv.slice(2));
  console.log(`${r.changes.length} changed, ${r.unchanged} unchanged (${r.total} screenshots)`);
}
