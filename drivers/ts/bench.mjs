// LightPHE TypeScript (compiled JS, Node.js) benchmark driver. Usage: node bench.mjs key=value ...
// Emits one JSON event per line prefixed with "@@BENCH " on stdout.
import { fileURLToPath, pathToFileURL } from 'node:url';
import path from 'node:path';

// lightphe-ts build: LIGHTPHE_TS_DIST, else <LIGHTPHE_GITHUB or ../github>/lightphe-ts/dist/esm/index.js
const HERE = path.dirname(fileURLToPath(import.meta.url));
const GITHUB = process.env.LIGHTPHE_GITHUB || path.resolve(HERE, '../../../github');
const LIB = pathToFileURL(
  process.env.LIGHTPHE_TS_DIST || path.join(GITHUB, 'lightphe-ts/dist/esm/index.js')
).href;

const a = {};
for (const kv of process.argv.slice(2)) {
  const p = kv.indexOf('=');
  a[kv.slice(0, p)] = kv.slice(p + 1);
}
const int = (k) => parseInt(a[k], 10);
const num = (k) => parseFloat(a[k]);

const emit = (ev) => process.stdout.write('@@BENCH ' + JSON.stringify(ev) + '\n');
const now = () => process.hrtime.bigint();
const secondsSince = (t0) => Number(now() - t0) / 1e9;
const trim = (s) => String(s).slice(0, 300);

function measure(fn) {
  let start = now();
  let warm = 0;
  for (;;) {
    fn();
    warm++;
    if (secondsSince(start) >= num('warm_s') || warm >= int('warm_max')) break;
  }
  const samples = [];
  start = now();
  while (samples.length < int('op_reps')) {
    const t0 = now();
    fn();
    samples.push(Number(now() - t0));
    if (samples.length >= int('op_min') && secondsSince(start) >= num('op_budget')) break;
  }
  return { warm, samples };
}

const mod = (x, m) => ((x % m) + m) % m;
function check(got, expected, pm) {
  return expected.some((e) => got === e || (pm && pm > 0n && mod(got, pm) === mod(e, pm)));
}

const { LightPHE, VERSION } = await import(LIB);
emit({ ev: 'start', lang: 'ts', runtime: process.version, library: VERSION });
if (a.mode === 'baseline') {
  emit({ ev: 'done' });
  process.exit(0);
}

const ks = a.key_size;
const keySize = !ks || ks === 'none' ? null : parseInt(ks, 10);
let phe = null;
const kgStart = now();
for (let r = 0; r < int('kg_reps'); r++) {
  const t0 = now();
  try {
    const built = new LightPHE({
      algorithmName: a.algorithm,
      keySize,
      form: a.form || null,
      curve: a.curve || null,
      maxTries: int('max_tries'),
    });
    phe = built;
  } catch (err) {
    emit({ ev: 'keygen_error', msg: trim(err && err.message), ns: Number(now() - t0) });
    if (secondsSince(kgStart) >= num('kg_budget')) break;
    continue;
  }
  emit({ ev: 'keygen', ns: Number(now() - t0) });
  if (secondsSince(kgStart) >= num('kg_budget')) break;
}
if (phe === null) {
  emit({ ev: 'done' });
  process.exit(0);
}

const cs = phe.cs;
const m1 = BigInteger(int('m1'));
const m2 = BigInteger(int('m2'));
const k = BigInteger(int('k'));
function BigInteger(v) {
  return BigInt(v);
}
const pm = cs.plaintextModulo;

const bitLen = (x) => (typeof x === 'bigint' ? (x === 0n ? 0 : x.toString(2).length) : null);
emit({ ev: 'params', pm_bits: bitLen(pm), cm_bits: bitLen(cs.ciphertextModulo) });

const c1 = cs.encrypt(m1);
const c2 = cs.encrypt(m2);

let m = measure(() => cs.encrypt(m1));
emit({ ev: 'op', op: 'encrypt', warmup: m.warm, ns: m.samples });
m = measure(() => cs.decrypt(c1));
emit({ ev: 'op', op: 'decrypt', warmup: m.warm, ns: m.samples });
emit({ ev: 'check', op: 'decrypt', ok: check(cs.decrypt(c1), [m1], pm) });

const ops = {
  add: () => cs.add(c1, c2),
  multiply: () => cs.multiply(c1, c2),
  multiply_by_constant: () => cs.multiplyByConstant(c1, k),
  xor: () => cs.xor(c1, c2),
  and: () => cs.homomorphicAnd(c1, c2),
  reencrypt: () => cs.reencrypt(c1),
};
const expected = {
  add: [m1 + m2],
  multiply: [m1 * m2],
  multiply_by_constant: [m1 * k, m1 ** k],
  xor: [m1 ^ m2],
  and: [m1 & m2],
  reencrypt: [m1],
};

for (const op of (a.ops || '').split(',').filter(Boolean)) {
  const fn = ops[op];
  let res;
  try {
    res = fn();
  } catch (err) {
    emit({ ev: 'op_unsupported', op, msg: trim(err && err.message) });
    continue;
  }
  try {
    const got = cs.decrypt(res);
    emit({ ev: 'check', op, ok: check(got, expected[op], pm), got: got.toString() });
  } catch (err) {
    emit({ ev: 'check', op, ok: false, got: 'error: ' + trim(err && err.message) });
  }
  m = measure(fn);
  emit({ ev: 'op', op, warmup: m.warm, ns: m.samples });
}
emit({ ev: 'done' });
