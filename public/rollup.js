const _CTORD = { i8: Int8Array, i16: Int16Array, u16: Uint16Array, i32: Int32Array, u32: Uint32Array };
export function decodeBinary(buf) {
  const u8 = new Uint8Array(buf), dv = new DataView(buf);
  if (String.fromCharCode(u8[0], u8[1], u8[2], u8[3]) !== 'NBB1') throw new Error('bad dump magic');
  const headerLen = dv.getUint32(4, true);
  const header = JSON.parse(new TextDecoder().decode(u8.subarray(8, 8 + headerLen)));
  const bodyOffset = (8 + headerLen + 7) & ~7;
  const L = header.layout;
  const view = (name) => new _CTORD[L[name].type](buf, bodyOffset + L[name].off, L[name].len);
  const dump = {
    columnar: true, mode: header.mode, axes: header.axes, N: header.N, nAxes: header.nAxes,
    vNull: header.vNull, dict: header.dict,
    v: view('v'), n: view('n'), recOff: view('recOff'),
    q: null, w: null, qb: null, qp: null, po: null,
  };
  for (const f of header.flags || []) dump[f] = view(f);
  for (const [name] of header.cols) dump[name] = view(name);
  return dump;
}

export const cmpGame = (a, b) =>
  (a[5] < b[5] ? 1 : a[5] > b[5] ? -1 : 0) ||
  (Number(b[4] || 0) - Number(a[4] || 0)) || (b[0] - a[0]);
export const cmpSeason = (a, b) =>
  (a[2] < b[2] ? 1 : a[2] > b[2] ? -1 : 0) ||
  (a[1] < b[1] ? -1 : a[1] > b[1] ? 1 : 0) || (a[0] - b[0]);

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
  const po = d.po && c.tl != null ? d.po[c.tl] : null;
  return game
    ? { p: c.p, r: c.r, a: c.a, n: c.n, d: D.date[d.date[j]], pl: D.name[d.name[j]], t: D.team[d.team[j]],
        m: D.matchup[d.matchup[j]], g: D.gameid[d.gameid[j]], pid: D.pid[d.pid[j]], w: d.wl[j] === -1 ? null : d.wl[j], po }
    : { p: c.p, r: c.r, a: c.a, n: c.n, d: D.season[d.season[j]], pl: D.name[d.name[j]], t: null,
        m: D.season[d.season[j]] + ' season', g: null, gp: d.gp[j], pid: D.pid[d.pid[j]], po };
}

export function lineKept(d, i, wlFilter, qual, minGames, poFilter) {
  if (wlFilter != null && (!d.w || d.w[i] !== wlFilter)) return false;
  if (poFilter != null && (!d.po || d.po[i] !== poFilter)) return false;
  if (qual && ((qual.bat && (!d.qb || !d.qb[i])) || (qual.pit && (!d.qp || !d.qp[i])))) return false;
  if (minGames && (!d.q || d.q[i] !== 1)) return false;
  return true;
}

export function makeRecentCtx(dump, x, y, z, wlFilter, qual, minGames, poFilter, wKey) {
  const game = dump.mode === 'game';
  const ix = dump.axes.indexOf(x), iy = dump.axes.indexOf(y), iz = dump.axes.indexOf(z);
  return { columnar: !!dump.columnar, dump, ix, iy, iz, game,
           iw: wKey ? dump.axes.indexOf(wKey) : -1,
           cmp: game ? cmpGame : cmpSeason, wlFilter, qual, minGames, poFilter,
           dz0: x === 'p_np', dz1: y === 'p_np', dz2: z === 'p_np', dzw: wKey === 'p_np' };
}

function wRange(out) {
  let min = Infinity, max = -Infinity;
  for (const c of out) { if (c.w4 < min) min = c.w4; if (c.w4 > max) max = c.w4; }
  return out.length ? { min, max } : { min: 0, max: 0 };
}

function finishRollup(out, ctx, wKey) {
  const mx = out.reduce((m, c) => Math.max(m, c.p), 0);
  const my = out.reduce((m, c) => Math.max(m, c.r), 0);
  const mz = out.reduce((m, c) => Math.max(m, c.a), 0);
  const res = { axes: { x: { max: mx }, y: { max: my }, z: { max: mz } }, cells: out, recentCtx: ctx };
  if (wKey) res.w = wRange(out);
  return res;
}

function rollupColumnar(dump, x, y, z, wlFilter, qual, minGames, poFilter, wKey) {
  const ctx = makeRecentCtx(dump, x, y, z, wlFilter, qual, minGames, poFilter, wKey);
  const { ix, iy, iz, iw, game, dz0, dz1, dz2, dzw } = ctx;
  if (ix < 0 || iy < 0 || iz < 0 || (wKey && iw < 0)) return finishRollup([], ctx, wKey);
  const { v, n, nAxes, N, recOff, vNull } = dump;
  const cells = new Map();
  for (let i = 0; i < N; i++) {
    if (!lineKept(dump, i, wlFilter, qual, minGames, poFilter)) continue;
    const b = i * nAxes, vx = v[b + ix], vy = v[b + iy], vz = v[b + iz];
    if (vx === vNull || vy === vNull || vz === vNull) continue;
    if ((dz0 && vx === 0) || (dz1 && vy === 0) || (dz2 && vz === 0)) continue;
    let key = vx + ',' + vy + ',' + vz, vw = 0;
    if (iw >= 0) {
      vw = v[b + iw];
      if (vw === vNull || (dzw && vw === 0)) continue;
      key += ',' + vw;
    }
    let c = cells.get(key);
    if (!c) { c = { p: vx, r: vy, a: vz, n: 0, ti: -1, w4: iw >= 0 ? vw : undefined }; cells.set(key, c); }
    c.n += n[i];
    for (let j = recOff[i], e = recOff[i + 1]; j < e; j++) {
      if (c.ti < 0 || cmpOcc(dump, j, c.ti, game) < 0) { c.ti = j; c.tl = i; }
    }
  }
  const out = [];
  for (const c of cells.values()) {
    const cell = cellFromRep(dump, c, game);
    if (c.w4 !== undefined) cell.w4 = c.w4;
    out.push(cell);
  }
  return finishRollup(out, ctx, wKey);
}

export function rollupCube(dump, x, y, z, wlFilter, qual, minGames, poFilter, wKey) {
  if (dump.columnar) return rollupColumnar(dump, x, y, z, wlFilter, qual, minGames, poFilter, wKey);
  const ctx = makeRecentCtx(dump, x, y, z, wlFilter, qual, minGames, poFilter, wKey);
  const { ix, iy, iz, iw, game, cmp, dz0, dz1, dz2, dzw } = ctx;
  if (wKey && iw < 0) return finishRollup([], ctx, wKey);
  const cells = new Map();
  for (const ln of dump.lines) {
    if (wlFilter != null && ln.w !== wlFilter) continue;
    if (poFilter != null && ln.po !== poFilter) continue;
    if (qual && ((qual.bat && !ln.qb) || (qual.pit && !ln.qp))) continue;
    if (minGames && ln.q !== 1) continue;
    const vx = ln.v[ix], vy = ln.v[iy], vz = ln.v[iz];
    if (vx == null || vy == null || vz == null) continue;
    if ((dz0 && vx === 0) || (dz1 && vy === 0) || (dz2 && vz === 0)) continue;
    let key = vx + ',' + vy + ',' + vz, vw = 0;
    if (iw >= 0) {
      vw = ln.v[iw];
      if (vw == null || (dzw && vw === 0)) continue;
      key += ',' + vw;
    }
    let c = cells.get(key);
    if (!c) { c = { p: vx, r: vy, a: vz, n: 0, top: null, w4: iw >= 0 ? vw : undefined }; cells.set(key, c); }
    c.n += ln.n;
    for (const o of ln.r) { if (c.top === null || cmp(o, c.top) < 0) c.top = o; }
  }
  const out = [];
  for (const c of cells.values()) {
    const top = c.top;
    const cell = game
      ? { p: c.p, r: c.r, a: c.a, n: c.n, d: top[5], pl: top[1], t: top[2],
          m: top[3], g: top[4], pid: top[0], w: top[6] }
      : { p: c.p, r: c.r, a: c.a, n: c.n, d: top[2], pl: top[1], t: null,
          m: top[2] + ' season', g: null, gp: top[3], pid: top[0] };
    if (c.w4 !== undefined) cell.w4 = c.w4;
    out.push(cell);
  }
  return finishRollup(out, ctx, wKey);
}
