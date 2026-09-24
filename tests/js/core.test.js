// RISU フロントの純粋ロジック（static/js/risu-core.js）の単体テスト．
// 実行: node --test tests/js/*.test.js   （依存なし．Node 18 以上の組み込みテストランナー）
'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

const core = require(path.join(__dirname, '..', '..', 'static', 'js', 'risu-core.js'));

// ── decodeFrame ──────────────────────────────────────────────
test('decodeFrame: v3 は差分符号化された ids と量子化された vs / alphas を復元する', () => {
  // サーバーの _encode_frames_v3: ids=差分 int32, vs=0.1 m/s 単位, alphas=0.001 単位
  const raw = { ids: [3, 2, 5], xs: [0, 10, 20], ys: [1, 1, 1], vs: [123, 0, 200], alphas: [500, 0, 1000], li: [0, 1, 1] };
  const f = core.decodeFrame(raw, { isV3: true });
  assert.equal(f.n, 3);
  assert.deepEqual(Array.from(f.ids), [3, 5, 10]);
  assert.ok(Math.abs(f.vs[0] - 12.3) < 1e-5 && f.vs[1] === 0 && Math.abs(f.vs[2] - 20) < 1e-5);
  assert.ok(Math.abs(f.alphas[0] - 0.5) < 1e-6 && Math.abs(f.alphas[2] - 1.0) < 1e-6);
  assert.ok(f.ids instanceof Uint32Array && f.li instanceof Uint16Array);
});

test('decodeFrame: v2 は値をそのまま TypedArray にする', () => {
  const raw = { ids: [7, 9], xs: [1.5, 2.5], ys: [0, 0], vs: [12.34, 0], alphas: [0.25, 1], li: [2, 0] };
  const f = core.decodeFrame(raw, { isV3: false });
  assert.deepEqual(Array.from(f.ids), [7, 9]);
  assert.ok(Math.abs(f.vs[0] - 12.34) < 1e-5);
  assert.ok(Math.abs(f.alphas[0] - 0.25) < 1e-6);
});

test('decodeFrame: 旧 array-of-objects 形式は linkIdxMap でリンク index を引く', () => {
  const raw = [{ id: 1, x: 5, y: 6, v: 7, alpha: 0.5, link: 'B' }, { id: 2, x: 0, y: 0, v: 0, link: 'zzz' }];
  const f = core.decodeFrame(raw, { linkIdxMap: { A: 0, B: 1 } });
  assert.equal(f.n, 2);
  assert.equal(f.li[0], 1);
  assert.equal(f.li[1], 0);           // 未知のリンク名は 0
  assert.equal(f.alphas[1], 0);       // alpha 省略は 0
});

test('decodeFrame: 不正な入力は空フレーム', () => {
  assert.equal(core.decodeFrame(null).n, 0);
  assert.equal(core.decodeFrame({ foo: 1 }).n, 0);
});

test('decodeFrames: キーを JS の数値表記に正規化し，release で生データを手放す', () => {
  const rawFrames = { '12.0': { ids: [1], xs: [0], ys: [0], vs: [1], alphas: [0], li: [0] }, '12.5': [] };
  const out = core.decodeFrames(rawFrames, { isV3: false, linkNames: ['L'], release: true });
  assert.deepEqual(Object.keys(out).sort(), ['12', '12.5']);
  assert.equal(out['12'].n, 1);
  assert.equal(rawFrames['12.0'], null);
});

// ── lowerBoundIndex / tripAt ─────────────────────────────────
test('lowerBoundIndex: t 以下の最後の index，先頭より前は 0，空は -1', () => {
  const ts = [10, 20, 30];
  assert.equal(core.lowerBoundIndex(ts, 25), 1);
  assert.equal(core.lowerBoundIndex(ts, 30), 2);
  assert.equal(core.lowerBoundIndex(ts, 99), 2);
  assert.equal(core.lowerBoundIndex(ts, 5), 0);
  assert.equal(core.lowerBoundIndex([], 5), -1);
});

test('tripAt: 累積系列を時刻で引く', () => {
  const s = { t: [0, 100, 1000], entered: [0, 25, 50], completed: [0, 0, 50] };
  assert.deepEqual(core.tripAt(s, 0), { entered: 0, completed: 0 });
  assert.deepEqual(core.tripAt(s, 150), { entered: 25, completed: 0 });
  assert.deepEqual(core.tripAt(s, 1000), { entered: 50, completed: 50 });
});

// ── phaseIndexAt ─────────────────────────────────────────────
test('phaseIndexAt: phase_log は実 deltat で引く（tmax/件数 の逆算はずれる）', () => {
  // deltat=8.5, tmax=1000 → ログ 117 件．t=1000 秒は index 117 → clamp で 116
  const log = Array.from({ length: 117 }, (_, i) => (Math.floor(i * 8.5 / 30) % 2));
  const sig = { phases: [30, 30], phase_log: log, deltat: 8.5 };
  for (const t of [0, 42, 59, 60, 500, 999, 1000]) {
    const expected = log[Math.min(116, Math.floor(t / 8.5))];
    assert.equal(core.phaseIndexAt(sig, t, 1000), expected, `t=${t}`);
  }
  // deltat が無い古いデータは tmax / 件数 に落ちる
  const legacy = { phases: [30, 30], phase_log: [0, 1, 0, 1] };
  assert.equal(core.phaseIndexAt(legacy, 0, 400), 0);
  assert.equal(core.phaseIndexAt(legacy, 150, 400), 1);
});

test('phaseIndexAt: ログが無ければ公称サイクルで判定，信号なしは -1', () => {
  assert.equal(core.phaseIndexAt({ phases: [40, 20] }, 39, 0), 0);
  assert.equal(core.phaseIndexAt({ phases: [40, 20] }, 40, 0), 1);
  assert.equal(core.phaseIndexAt({ phases: [40, 20] }, 60, 0), 0);
  assert.equal(core.phaseIndexAt([40, 20], 45, 0), 1);          // 旧シグネチャ（配列）
  assert.equal(core.phaseIndexAt({ phases: [] }, 5, 0), -1);
});

// ── computeStats / statValuesAt ──────────────────────────────
function frame(ids, vs) {
  const n = ids.length;
  return { n, ids: Uint32Array.from(ids), xs: new Float32Array(n), ys: new Float32Array(n),
           vs: Float32Array.from(vs), alphas: new Float32Array(n), li: new Uint16Array(n) };
}

test('computeStats: サーバー系列（実台数・実イベント・車両平均速度）を優先する', () => {
  const frameTimes = [10, 20, 30];
  const framesData = { '10': frame([1, 2], [10, 20]), '20': frame([2, 3], [5, 5]), '30': frame([3], [1]) };
  const features = [{ properties: { free_flow_speed: 20, timeline: [{ t: 10, speed: 20 }, { t: 20, speed: 10 }] } }];
  const s = core.computeStats({
    frameTimes, framesData, features,
    vehicleCounts: [10, 10, 5],                 // プラトン 2,2,1 × deltan 5
    tripSeries: { t: [0, 10, 20, 30, 100], entered: [0, 10, 15, 15, 15], completed: [0, 0, 5, 10, 15] },
    frameAvgSpeed: [15, 5, 1],
    totalTrips: 15,
  });
  assert.deepEqual(s.active, [10, 10, 5]);
  assert.deepEqual(s.started, [10, 15, 15]);
  assert.deepEqual(s.completed, [0, 5, 10]);
  assert.deepEqual(s.avgSpeed, [15, 5, 1]);
  assert.equal(s.totalTrips, 15);
  // 混雑度はリンク timeline から（20/20=1.0, 10/20=0.5, 以降 0.5）
  assert.deepEqual(s.avgRatio, [1, 0.5, 0.5]);
});

test('computeStats: 古い結果はフレームから近似し，vehScale（deltan × 間引き）を掛ける', () => {
  const frameTimes = [10, 20, 30];
  const framesData = { '10': frame([1, 2], [10, 20]), '20': frame([2, 3], [5, 5]), '30': frame([3], [1]) };
  const features = [{ properties: { free_flow_speed: 20, timeline: [{ t: 10, speed: 20 }, { t: 20, speed: 10 }] } }];
  const s = core.computeStats({ frameTimes, framesData, features, vehScale: 5 });
  assert.deepEqual(s.active, [10, 10, 5]);
  assert.deepEqual(s.started, [10, 15, 15]);    // 出現済み車両 2,3,3 × 5
  assert.deepEqual(s.completed, [0, 5, 10]);    // 最後に見えたのが前のフレームの車両 0,1,2 × 5
  assert.deepEqual(s.avgSpeed, [20, 10, 10]);   // リンク単純平均への代替
  assert.equal(s.totalTrips, 15);
  assert.equal(core.computeStats({ frameTimes: [], framesData: {} }), null);
});

test('statValuesAt: 最後のフレームより後は 流入 − 到着 で走行中台数を出す', () => {
  const frameTimes = [10, 20, 30];
  const stats = { active: [10, 10, 5], started: [10, 15, 15], completed: [0, 5, 10], avgSpeed: [15, 5, 1] };
  const trips = { t: [0, 10, 20, 30, 100], entered: [0, 10, 15, 15, 15], completed: [0, 0, 5, 10, 15] };
  assert.deepEqual(core.statValuesAt(stats, frameTimes, trips, 25),
                   { idx: 1, active: 10, entered: 15, completed: 5, avgSpeed: 5, mode: 'interp' });
  // t=100: 全車両到着後．フレームは 30 秒で終わっているが 到着 15 / 走行中 0 になる
  const end = core.statValuesAt(stats, frameTimes, trips, 100);
  assert.equal(end.completed, 15);
  assert.equal(end.active, 0);
  // trip_series が無い古い結果は最寄りフレームの値
  assert.equal(core.statValuesAt(stats, frameTimes, null, 100).active, 5);
});


test('statValuesAt: 需要の空白時間（フレームが無い区間）は 流入 − 到着 = 0 になる', () => {
  // 0〜100 秒と 700〜800 秒に需要．フレームは走行中の時刻にしか無い
  const frameTimes = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100, 110, 700, 710, 720, 730, 740, 750, 760, 770, 780, 790, 800, 810];
  const N = frameTimes.length;
  const stats = { active: new Array(N).fill(5), started: new Array(N).fill(50), completed: new Array(N).fill(45), avgSpeed: new Array(N).fill(10) };
  const trips = { t: [0, 100, 110, 700, 810, 1000], entered: [0, 50, 50, 50, 100, 100], completed: [0, 45, 50, 50, 95, 100] };
  assert.equal(core.typicalFrameStep(frameTimes), 10);
  assert.equal(core.isFrameGap(frameTimes, 500), true);
  assert.equal(core.isFrameGap(frameTimes, 55), false);
  const mid = core.statValuesAt(stats, frameTimes, trips, 500);
  assert.equal(mid.active, 0);
  assert.equal(mid.entered, 50);
  assert.equal(mid.completed, 50);
  assert.equal(mid.avgSpeed, 0);
  // 走行中の区間は従来どおりフレームの値
  assert.equal(core.statValuesAt(stats, frameTimes, trips, 55).active, 5);
});


test('frameWindow: サーバーの間隔があれば，フレームが 2 個でも空白時間と終了後を判定できる', () => {
  // 100 m の道路に需要を 0〜10 秒と 700〜710 秒．フレームは 10 秒と 710 秒だけ（deltat 5 秒）
  const frameTimes = [10, 710];
  const interval = 5;
  assert.equal(core.frameWindow(frameTimes, 10, interval).mode, 'exact');
  assert.equal(core.frameWindow(frameTimes, 13, interval).mode, 'hold');
  assert.equal(core.frameWindow(frameTimes, 500, interval).mode, 'none');
  assert.equal(core.frameWindow(frameTimes, 1000, interval).mode, 'none');
  assert.equal(core.frameWindow(frameTimes, 5, interval).mode, 'none');     // 開始前
  // 間隔が無い古い JSON は並びから推定するので，この形は判定できない（既知の限界）
  assert.equal(core.frameWindow(frameTimes, 500, null).mode, 'interp');
  // 通常の連続フレームは補間
  const reg = core.frameWindow([10, 15, 20, 25], 17, interval);
  assert.equal(reg.mode, 'interp'); assert.equal(reg.i, 1); assert.equal(reg.j, 2);
  assert.ok(Math.abs(reg.frac - 0.4) < 1e-9);
  // 最後のフレームの直後は据え置き，その後は none
  assert.equal(core.frameWindow([10, 15, 20, 25], 29, interval).mode, 'hold');
  assert.equal(core.frameWindow([10, 15, 20, 25], 31, interval).mode, 'none');
});

test('statValuesAt: フレーム 2 個のケースで 500 秒・1000 秒の走行中が 0 になる', () => {
  const frameTimes = [10, 710];
  const stats = { active: [5, 5], started: [5, 10], completed: [0, 5], avgSpeed: [10, 10] };
  const trips = { t: [0, 10, 15, 700, 710, 715, 1000], entered: [0, 5, 5, 5, 10, 10, 10], completed: [0, 0, 5, 5, 5, 10, 10] };
  assert.equal(core.statValuesAt(stats, frameTimes, trips, 500, 5).active, 0);
  assert.equal(core.statValuesAt(stats, frameTimes, trips, 1000, 5).active, 0);
  assert.equal(core.statValuesAt(stats, frameTimes, trips, 12, 5).active, 5);   // 直後は据え置き
  assert.equal(core.statValuesAt(stats, frameTimes, trips, 710, 5).active, 5);
});
