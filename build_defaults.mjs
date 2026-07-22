// Precompute each sport's default view: run the exact client rollup
// (public/rollup.js) over the full game dump and save the resulting cells as
// <base>/default.json.br. The page renders this small file instantly while the
// big dump streams in the background (see fastDefaultRender in index.html).
//
// Run after regenerating dumps:  node build_defaults.mjs
import { readFileSync, writeFileSync } from 'node:fs';
import { brotliCompressSync, brotliDecompressSync, constants } from 'node:zlib';
import { decodeBinary, rollupCube } from './public/rollup.js';

// Must match SPORTS defaults (and first group/position) in public/index.html
// and the bases in its early-fetch head script.
const TARGETS = [
  ['nba',     ['pts', 'reb', 'ast']],
  ['wnba',    ['pts', 'reb', 'ast']],
  ['nhl/sk',  ['goals', 'assists', 'sog']],
  ['nfl/off', ['rec_yds', 'rec', 'rec_td']],
  ['mlb/all', ['sb', 'rbi', 'hr']],
];

for (const [base, [x, y, z]] of TARGETS) {
  const raw = brotliDecompressSync(readFileSync(`public/${base}/game.bin.br`));
  const ab = raw.buffer.slice(raw.byteOffset, raw.byteOffset + raw.byteLength);
  const { axes, cells } = rollupCube(decodeBinary(ab), x, y, z, null, null, false);
  if (!cells.length) throw new Error(`${base}: default axes ${x}/${y}/${z} produced 0 cells`);
  const out = {
    axes: { x: { key: x, max: axes.x.max }, y: { key: y, max: axes.y.max }, z: { key: z, max: axes.z.max } },
    cells,
  };
  const br = brotliCompressSync(Buffer.from(JSON.stringify(out)),
    { params: { [constants.BROTLI_PARAM_QUALITY]: 11 } });
  writeFileSync(`public/${base}/default.json.br`, br);
  console.log(`${base}: ${cells.length} cells -> ${(br.length / 1024).toFixed(0)}KB`);
}
