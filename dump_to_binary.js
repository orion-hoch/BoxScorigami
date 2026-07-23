const { brotliCompressSync, brotliDecompressSync, constants } = require('zlib');
const fs = require('fs');

const SPECS = {
  game:   [['pid','dict',0],['name','dict',1],['team','dict',2],['matchup','dict',3],
           ['gameid','dict',4],['date','dict',5],['wl','i8',6]],
  season: [['pid','dict',0],['name','dict',1],['season','dict',2],['gp','i32',3]],
};
SPECS['season-avg'] = SPECS.season;
const LINE_FLAGS = ['q', 'w', 'qb', 'qp', 'po'];

function encodeDump(d) {
  const mode = d.mode, axes = d.axes, N = d.lines.length, nAxes = axes.length;
  const spec = SPECS[mode];
  if (!spec) throw new Error('no column spec for mode ' + mode);

  let vmax = 0;
  for (const l of d.lines) for (const x of l.v) if (x != null) { const a = Math.abs(x); if (a > vmax) vmax = a; }
  const vType = vmax < 32000 ? 'i16' : 'i32';
  const vNull = vType === 'i16' ? -32768 : -2147483648;
  const VArr = vType === 'i16' ? Int16Array : Int32Array;

  const flags = LINE_FLAGS.filter(f => d.lines.some(l => l[f] != null));
  let totalRec = 0; for (const l of d.lines) totalRec += l.r.length;

  const v = new VArr(N * nAxes);
  const n = new Int32Array(N);
  const recOff = new Uint32Array(N + 1);
  const flagArr = {};
  for (const f of flags) flagArr[f] = new Int8Array(N);

  const dicts = {}, dictMap = {}, rawIdx = {}, typed = {};
  for (const [name, kind] of spec) {
    if (kind === 'dict') { dicts[name] = []; dictMap[name] = new Map(); rawIdx[name] = new Int32Array(totalRec); }
    else typed[name] = kind === 'i8' ? new Int8Array(totalRec) : new Int32Array(totalRec);
  }
  const intern = (name, s) => {
    const m = dictMap[name]; let i = m.get(s);
    if (i === undefined) { i = dicts[name].length; m.set(s, i); dicts[name].push(s); }
    return i;
  };

  let ri = 0;
  for (let i = 0; i < N; i++) {
    const l = d.lines[i];
    for (let a = 0; a < nAxes; a++) v[i * nAxes + a] = l.v[a] == null ? vNull : l.v[a];
    n[i] = l.n;
    for (const f of flags) flagArr[f][i] = l[f] == null ? -1 : l[f];
    recOff[i] = ri;
    for (const o of l.r) {
      for (const [name, kind, idx] of spec) {
        const val = o[idx];
        if (kind === 'dict') rawIdx[name][ri] = intern(name, val);
        else if (kind === 'i8') typed[name][ri] = val == null ? -1 : val;
        else typed[name][ri] = val == null ? 0 : val;
      }
      ri++;
    }
  }
  recOff[N] = ri;

  const colType = {};
  for (const [name, kind] of spec) {
    if (kind !== 'dict') { colType[name] = kind; continue; }
    const t = dicts[name].length <= 0xffff ? 'u16' : 'u32';
    colType[name] = t;
    typed[name] = t === 'u16' ? Uint16Array.from(rawIdx[name]) : Uint32Array.from(rawIdx[name]);
  }

  const sections = [['v', v, vType], ['n', n, 'i32'], ['recOff', recOff, 'u32']];
  for (const f of flags) sections.push([f, flagArr[f], 'i8']);
  for (const [name] of spec) sections.push([name, typed[name], colType[name]]);
  const align = (x, a) => (x + a - 1) & ~(a - 1);
  const layout = {}; let off = 0;
  for (const [name, arr, type] of sections) { off = align(off, 4); layout[name] = { off, len: arr.length, type }; off += arr.byteLength; }
  const body = new Uint8Array(align(off, 4));
  for (const [name, arr] of sections) body.set(new Uint8Array(arr.buffer, arr.byteOffset, arr.byteLength), layout[name].off);

  const header = { mode, axes, N, nAxes, totalRec, vType, vNull, flags, dict: dicts, cols: spec.map(([nm, k]) => [nm, k]), layout };
  const headerBuf = Buffer.from(JSON.stringify(header), 'utf8');
  const lenBuf = Buffer.alloc(4); lenBuf.writeUInt32LE(headerBuf.length, 0);
  const preLen = 8 + headerBuf.length;
  const pad = Buffer.alloc(align(preLen, 8) - preLen);
  return Buffer.concat([Buffer.from('NBB1', 'ascii'), lenBuf, headerBuf, pad, Buffer.from(body.buffer, 0, body.byteLength)]);
}

if (require.main === module) {
  const files = process.argv.slice(2);
  if (!files.length) { console.error('usage: node dump_to_binary.js <dump.json.br> ...'); process.exit(1); }
  for (const f of files) {
    const dump = JSON.parse(brotliDecompressSync(fs.readFileSync(f)).toString());
    const bin = encodeDump(dump);
    const br = brotliCompressSync(bin, { params: { [constants.BROTLI_PARAM_QUALITY]: 9, [constants.BROTLI_PARAM_SIZE_HINT]: bin.length } });
    const out = f.replace(/\.json\.br$/, '.bin.br');
    fs.writeFileSync(out, br);
    console.log(`${out}  ${(fs.statSync(f).size / 1e6).toFixed(1)} -> ${(br.length / 1e6).toFixed(1)} MB`);
  }
}

module.exports = { encodeDump, SPECS };
