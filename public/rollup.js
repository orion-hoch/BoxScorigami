// Shared dump decoding + cube rollup, used by the page (index.html) AND by
// build_defaults.mjs, which precomputes each sport's default view at build
// time. Keeping one copy is what guarantees the precomputed default.json.br
// is byte-identical to what the client would compute from the full dump.
// No DOM, no three.js — plain data in, plain data out.

// ---- Columnar binary dumps ----------------------------------------------
// decodeBinary turns *.bin.br (already inflated by the browser/node) into
// typed-array columns instead of a JSON array of line objects, skipping the
// ~800ms JSON.parse.
const _CTORD = { i8: Int8Array, i16: Int16Array, u16: Uint16Array, i32: Int32Array, u32: Uint32Array };
export function decodeBinary(buf) {
  const u8 = new Uint8Array(buf), dv = new DataView(buf);
  if (String.fromCharCode(u8[0], u8[1], u8[2], u8[3]) !== 'NBB1') throw new Error('bad dump magic');
  const headerLen = dv.getUint32(4, true);
  const header = JSON.parse(new TextDecoder().decode(u8.subarray(8, 8 + headerLen)));
  const bodyOffset = (8 + headerLen + 7) & ~7;   // align(8+headerLen, 8)
  const L = header.layout;
  const view = (name) => new _CTORD[L[name].type](buf, bodyOffset + L[name].off, L[name].len);
  const dump = {
    columnar: true, mode: header.mode, axes: header.axes, N: header.N, nAxes: header.nAxes,
    vNull: header.vNull, dict: header.dict,
    v: view('v'), n: view('n'), recOff: view('recOff'),
    q: null, w: null, qb: null, qp: null, po: null,
  };
  for (const f of header.flags || []) dump[f] = view(f);   // q / w / qb / qp / po
  for (const [name] of header.cols) dump[name] = view(name);
  return dump;
}

// Occurrence comparators (most-recent first). Shared with computeRecents so the
// lazily-rebuilt recent list orders exactly like the old eager one did.
export const cmpGame = (a, b) =>
  (a[5] < b[5] ? 1 : a[5] > b[5] ? -1 : 0) ||
  (Number(b[4] || 0) - Number(a[4] || 0)) || (b[0] - a[0]);
export const cmpSeason = (a, b) =>
  (a[2] < b[2] ? 1 : a[2] > b[2] ? -1 : 0) ||
  (a[1] < b[1] ? -1 : a[1] > b[1] ? 1 : 0) || (a[0] - b[0]);

// Compare two occurrences by flat recent-index, resolving dict values so the
// ordering is byte-identical to cmpGame/cmpSeason on the JSON occ arrays.
export function cmpOcc(d, j, k, game) {
  const D = d.dict;
  if (game) {
    const aD = D.date[d.date[j]], bD = D.date[d.date[k]];
    return (aD < bD ? 1 : aD > bD ? -1 : 0)
      || (Number(D.gameid[d.gameid[k]] || 0) - Number(D.gameid[d.gameid[j]] || 0))
      || (D.pid[d.pid[k]] - D.pid[d.pid[j]]);
  }
  const aS = D.season[d.season[j]], bS = D.season[d.season[k]];
  const aN = D.name[d.name[j]], bN = D.name[d.name[k]];
  return (aS < bS ? 1 : aS > bS ? -1 : 0)
    || (aN < bN ? -1 : aN > bN ? 1 : 0)
    || (D.pid[d.pid[j]] - D.pid[d.pid[k]]);
}
export function occToRecent(d, j, game) {
  const D = d.dict;
  return game
    ? { pl: D.name[d.name[j]], d: D.date[d.date[j]], t: D.team[d.team[j]], m: D.matchup[d.matchup[j]],
        g: D.gameid[d.gameid[j]], pid: D.pid[d.pid[j]], w: d.wl[j] === -1 ? null : d.wl[j] }
    : { pl: D.name[d.name[j]], d: D.season[d.season[j]], gp: d.gp[j], pid: D.pid[d.pid[j]] };
}
function cellFromRep(d, c, game) {
  const D = d.dict, j = c.ti;
  return game
    ? { p: c.p, r: c.r, a: c.a, n: c.n, d: D.date[d.date[j]], pl: D.name[d.name[j]], t: D.team[d.team[j]],
        m: D.matchup[d.matchup[j]], g: D.gameid[d.gameid[j]], pid: D.pid[d.pid[j]], w: d.wl[j] === -1 ? null : d.wl[j] }
    : { p: c.p, r: c.r, a: c.a, n: c.n, d: D.season[d.season[j]], pl: D.name[d.name[j]], t: null,
        m: D.season[d.season[j]] + ' season', g: null, gp: d.gp[j], pid: D.pid[d.pid[j]] };
}

// A line passes the same filters the JSON path applies, using the encoded
// flags. Missing-flag semantics match JSON exactly (undefined !== 1 -> skip,
// !undefined -> true), so a filter with no backing column drops every line.
export function lineKept(d, i, wlFilter, qual, minGames, poFilter) {
  if (wlFilter != null && (!d.w || d.w[i] !== wlFilter)) return false;
  if (poFilter != null && (!d.po || d.po[i] !== poFilter)) return false;
  if (qual && ((qual.bat && (!d.qb || !d.qb[i])) || (qual.pit && (!d.qp || !d.qp[i])))) return false;
  if (minGames && (!d.q || d.q[i] !== 1)) return false;
  return true;
}

// Everything computeRecents needs to reproduce a rollup's exact filtering,
// built once here so rollup and the lazy recent-list can never disagree.
export function makeRecentCtx(dump, x, y, z, wlFilter, qual, minGames, poFilter) {
  const game = dump.mode === 'game';
  const ix = dump.axes.indexOf(x), iy = dump.axes.indexOf(y), iz = dump.axes.indexOf(z);
  return { columnar: !!dump.columnar, dump, ix, iy, iz, game,
           cmp: game ? cmpGame : cmpSeason, wlFilter, qual, minGames, poFilter,
           dz0: x === 'p_np', dz1: y === 'p_np', dz2: z === 'p_np' };
}

function rollupColumnar(dump, x, y, z, wlFilter, qual, minGames, poFilter) {
  const ctx = makeRecentCtx(dump, x, y, z, wlFilter, qual, minGames, poFilter);
  const { ix, iy, iz, game, dz0, dz1, dz2 } = ctx;
  if (ix < 0 || iy < 0 || iz < 0)   // unknown axis -> no cells (matches JSON path)
    return { axes: { x: { max: 0 }, y: { max: 0 }, z: { max: 0 } }, cells: [], recentCtx: ctx };
  const { v, n, nAxes, N, recOff, vNull } = dump;
  const cells = new Map();
  for (let i = 0; i < N; i++) {
    if (!lineKept(dump, i, wlFilter, qual, minGames, poFilter)) continue;
    const b = i * nAxes, vx = v[b + ix], vy = v[b + iy], vz = v[b + iz];
    if (vx === vNull || vy === vNull || vz === vNull) continue;
    if ((dz0 && vx === 0) || (dz1 && vy === 0) || (dz2 && vz === 0)) continue;
    const key = vx + ',' + vy + ',' + vz;
    let c = cells.get(key);
    if (!c) { c = { p: vx, r: vy, a: vz, n: 0, ti: -1 }; cells.set(key, c); }
    c.n += n[i];
    for (let j = recOff[i], e = recOff[i + 1]; j < e; j++) {
      if (c.ti < 0 || cmpOcc(dump, j, c.ti, game) < 0) c.ti = j;
    }
  }
  const out = [];
  for (const c of cells.values()) out.push(cellFromRep(dump, c, game));
  const mx = out.reduce((m, c) => Math.max(m, c.p), 0);
  const my = out.reduce((m, c) => Math.max(m, c.r), 0);
  const mz = out.reduce((m, c) => Math.max(m, c.a), 0);
  return { axes: { x: { max: mx }, y: { max: my }, z: { max: mz } }, cells: out, recentCtx: ctx };
}

export function rollupCube(dump, x, y, z, wlFilter, qual, minGames, poFilter) {
  if (dump.columnar) return rollupColumnar(dump, x, y, z, wlFilter, qual, minGames, poFilter);
  const ctx = makeRecentCtx(dump, x, y, z, wlFilter, qual, minGames, poFilter);
  const { ix, iy, iz, game, cmp, dz0, dz1, dz2 } = ctx;
  const cells = new Map();
  for (const ln of dump.lines) {
    if (wlFilter != null && ln.w !== wlFilter) continue;
    if (poFilter != null && ln.po !== poFilter) continue;
    if (qual && ((qual.bat && !ln.qb) || (qual.pit && !ln.qp))) continue;
    if (minGames && ln.q !== 1) continue;
    const vx = ln.v[ix], vy = ln.v[iy], vz = ln.v[iz];
    if (vx == null || vy == null || vz == null) continue;
    if ((dz0 && vx === 0) || (dz1 && vy === 0) || (dz2 && vz === 0)) continue;
    const key = vx + ',' + vy + ',' + vz;
    let c = cells.get(key);
    if (!c) { c = { p: vx, r: vy, a: vz, n: 0, top: null }; cells.set(key, c); }
    c.n += ln.n;
    // Keep only the representative (most-recent) occurrence in one pass. The
    // full top-5 recent list is rebuilt lazily on click (computeRecents) -- it
    // was ~73% of this function's cost and is only shown for one clicked cell,
    // never for a boxscorigami (n===1, whose lone game IS the representative).
    for (const o of ln.r) { if (c.top === null || cmp(o, c.top) < 0) c.top = o; }
  }
  const out = [];
  for (const c of cells.values()) {
    const top = c.top;
    out.push(game
      ? { p: c.p, r: c.r, a: c.a, n: c.n, d: top[5], pl: top[1], t: top[2],
          m: top[3], g: top[4], pid: top[0], w: top[6] }
      : { p: c.p, r: c.r, a: c.a, n: c.n, d: top[2], pl: top[1], t: null,
          m: top[2] + ' season', g: null, gp: top[3], pid: top[0] });
  }
  const mx = out.reduce((m, c) => Math.max(m, c.p), 0);
  const my = out.reduce((m, c) => Math.max(m, c.r), 0);
  const mz = out.reduce((m, c) => Math.max(m, c.a), 0);
  return { axes: { x: { max: mx }, y: { max: my }, z: { max: mz } }, cells: out, recentCtx: ctx };
}
