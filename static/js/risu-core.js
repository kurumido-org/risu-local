/*!
 * RISU core — UI（index.html）と node のテスト（tests/js）が共有する純粋関数．
 *
 * ここには DOM・グローバル状態・Canvas に触らない関数だけを置く．
 * index.html 側は薄いラッパー（グローバルを渡すだけ）にして，ロジックの検証は
 * `node --test tests/js/*.test.js` で行う（ブラウザ無しで走る）．
 *
 * 形式は UMD: ブラウザでは window.RisuCore，node では module.exports．
 * ビルド工程を増やさないため ES module にはしていない．
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.RisuCore = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const EMPTY_FRAME = Object.freeze({
    n: 0,
    ids: new Uint32Array(0), xs: new Float32Array(0), ys: new Float32Array(0),
    vs: new Float32Array(0), alphas: new Float32Array(0), li: new Uint16Array(0),
  });

  // ── フレームの復号 ──────────────────────────────────────────
  // サーバーの /results は columnar_v3（送出時の量子化 + 差分符号化，server.py の
  // _encode_frames_v3）で，ダウンロード済みの古い JSON は v2（素の値）か，さらに古い
  // array-of-objects．3 形式とも同じ TypedArray フレームに正規化する．
  // 精度を変えるときはサーバーの _encode_frames_v3 と**同時に**直すこと（CLAUDE.md §3.5）．
  function decodeFrame(raw, opts) {
    const isV3 = !!(opts && opts.isV3);
    if (raw && typeof raw === 'object' && !Array.isArray(raw) && Array.isArray(raw.ids)) {
      const n = raw.ids.length;
      if (isV3) {
        // v3: ids は差分符号化，vs は 0.1 m/s 単位，alphas は 0.001 単位の整数
        const ids = new Uint32Array(n), vs = new Float32Array(n), alphas = new Float32Array(n);
        const rid = raw.ids, rv = raw.vs, ra = raw.alphas;
        let acc = 0;
        for (let i = 0; i < n; i++) {
          acc += rid[i];
          ids[i] = acc;
          vs[i] = rv[i] * 0.1;
          alphas[i] = ra[i] * 0.001;
        }
        return { n, ids, vs, alphas,
                 xs: new Float32Array(raw.xs), ys: new Float32Array(raw.ys), li: new Uint16Array(raw.li) };
      }
      return { n,
               ids: new Uint32Array(raw.ids), xs: new Float32Array(raw.xs), ys: new Float32Array(raw.ys),
               vs: new Float32Array(raw.vs), alphas: new Float32Array(raw.alphas), li: new Uint16Array(raw.li) };
    }
    if (Array.isArray(raw)) {
      // 旧形式（array-of-objects）: link 名 → index は呼び出し側の linkIdxMap で引く
      const n = raw.length;
      const map = (opts && opts.linkIdxMap) || {};
      const ids = new Uint32Array(n), xs = new Float32Array(n), ys = new Float32Array(n);
      const vs = new Float32Array(n), alphas = new Float32Array(n), li = new Uint16Array(n);
      for (let i = 0; i < n; i++) {
        const v = raw[i];
        ids[i] = v.id | 0;
        xs[i] = v.x; ys[i] = v.y; vs[i] = v.v;
        alphas[i] = v.alpha != null ? v.alpha : 0;
        li[i] = map[v.link] | 0;
      }
      return { n, ids, xs, ys, vs, alphas, li };
    }
    return EMPTY_FRAME;
  }

  // rawFrames（{ "12.0": {...}, ... }）→ { "12": frame }．キーは JS の数値表記に正規化する
  // （Python は str(12.0) = "12.0"，JS は String(12) = "12"．CLAUDE.md §5）．
  // release=true なら復号済みの生データを null にして GC 対象にする（数十 MB の JSON 対策）．
  function decodeFrames(rawFrames, opts) {
    const out = {};
    const linkNames = (opts && opts.linkNames) || [];
    const linkIdxMap = {};
    for (let i = 0; i < linkNames.length; i++) linkIdxMap[linkNames[i]] = i;
    const o = { isV3: !!(opts && opts.isV3), linkIdxMap };
    for (const k of Object.keys(rawFrames || {})) {
      out[String(parseFloat(k))] = decodeFrame(rawFrames[k], o);
      if (opts && opts.release) rawFrames[k] = null;
    }
    return out;
  }

  // ── 時刻の検索 ──────────────────────────────────────────────
  // 昇順配列で t 以下の最後の index（t が先頭より前なら 0，空なら -1）
  function lowerBoundIndex(sorted, t) {
    if (!sorted || !sorted.length) return -1;
    let lo = 0, hi = sorted.length - 1;
    while (lo < hi) {
      const mid = (lo + hi + 1) >> 1;
      if (sorted[mid] <= t) lo = mid; else hi = mid - 1;
    }
    return lo;
  }

  // trip_series（{t[], entered[], completed[]}，サーバーが実イベントから集計）を時刻 t で引く
  function tripAt(series, t) {
    const i = lowerBoundIndex(series.t, t);
    if (i < 0) return { entered: 0, completed: 0 };
    return { entered: series.entered[i], completed: series.completed[i] };
  }

  // ── 信号現示 ────────────────────────────────────────────────
  // sig: {phases[], phase_log[]?, deltat?}．UXsim の実ログ（phase_log）があれば最優先．
  // ログ 1 件 = deltat 秒（deltan × reaction_time）．tmax / ログ件数 で逆算すると tmax が
  // deltat の整数倍でないときにずれる（1,000 秒中 97 秒で現示が違った）ので deltat を使う．
  function phaseIndexAt(sig, t, simTmax) {
    const phases = Array.isArray(sig) ? sig : (sig && sig.phases);
    const log = (sig && !Array.isArray(sig) && sig.phase_log) || null;
    if (log && log.length) {
      const dt = (sig.deltat > 0) ? sig.deltat : ((simTmax || log.length) / log.length);
      const idx = Math.max(0, Math.min(log.length - 1, Math.floor(t / dt)));
      return log[idx];
    }
    if (!phases || !phases.length) return -1;
    const cycle = phases.reduce((a, b) => a + b, 0);
    if (cycle <= 0) return -1;
    let x = t % cycle;
    for (let i = 0; i < phases.length; i++) {
      x -= phases[i];
      if (x < 0) return i;
    }
    return phases.length - 1;
  }

  // ── 統計系列 ────────────────────────────────────────────────
  // 台数の単位: フレームの点は UXsim のプラトン（deltan 台）で，描画用にさらに間引かれる
  // ことがある．分析用の値はサーバーが間引き前に数えたもの（vehicleCounts / tripSeries /
  // frameAvgSpeed）を優先し，無い古い結果だけ vehScale（deltan × 間引き間隔）で近似する．
  //   frameTimes     昇順のフレーム時刻
  //   framesData     { String(t): frame }
  //   features       GeoJSON features（properties.timeline / free_flow_speed）．混雑度の波形に使う
  //   vehicleCounts  フレームごとの実台数（任意）
  //   vehScale       近似用の係数（既定 1）
  //   tripSeries     {t, entered, completed}（任意）
  //   frameAvgSpeed  フレームごとの車両平均速度（任意）
  //   totalTrips     サーバー統計の total_trips（任意）
  function computeStats(input) {
    const frameTimes = input.frameTimes || [];
    const framesData = input.framesData || {};
    const N = frameTimes.length;
    if (!N) return null;
    const vehScale = input.vehScale || 1;
    const active = new Array(N), started = new Array(N), completed = new Array(N);
    const avgSpeed = new Array(N), avgRatio = new Array(N);

    const counts = input.vehicleCounts;
    const useCounts = !!(counts && counts.length === N);
    const firstSeen = new Map(), lastSeen = new Map();
    for (let i = 0; i < N; i++) {
      const frame = framesData[String(frameTimes[i])] || EMPTY_FRAME;
      active[i] = useCounts ? counts[i] : frame.n * vehScale;
      const ids = frame.ids;
      for (let j = 0; j < frame.n; j++) {
        const id = ids[j];
        if (!firstSeen.has(id)) firstSeen.set(id, i);
        lastSeen.set(id, i);
      }
    }
    // フレーム由来の近似（古い結果用）: 出現済み車両数 / 最後に見えたのが i より前の車両数
    const firstCnt = new Int32Array(N), lastCnt = new Int32Array(N);
    for (const i of firstSeen.values()) firstCnt[i]++;
    for (const i of lastSeen.values()) lastCnt[i]++;
    for (let i = 0, s = 0, c = 0; i < N; i++) {
      s += firstCnt[i]; started[i] = s * vehScale;
      completed[i] = c * vehScale; c += lastCnt[i];
    }
    // 実イベントの累積があればそれで上書き（フレームは走行車両がいる時刻にしか無いので，
    // 全車両到着後も「走行中 5 / 到着 45」のまま残る問題を避ける）
    const trips = input.tripSeries;
    if (trips && trips.t && trips.t.length) {
      for (let i = 0; i < N; i++) {
        const tr = tripAt(trips, frameTimes[i]);
        started[i] = tr.entered; completed[i] = tr.completed;
      }
    }

    // リンク別 timeline を 1 回だけ走査（フレーム時刻も timeline も昇順）．
    // 単純平均（リンク重み）は「平均速度」ではなく，混雑度の波形（avgRatio）と，
    // frameAvgSpeed が無い古い結果の代替にだけ使う．
    const features = input.features || [];
    const sum = new Float64Array(N), cnt = new Int32Array(N);
    const rsum = new Float64Array(N), rcnt = new Int32Array(N);
    for (const f of features) {
      const p = f.properties || {};
      const tl = p.timeline || [];
      if (!tl.length) continue;
      const ffs = p.free_flow_speed;
      let j = 0, cur = null;
      for (let i = 0; i < N; i++) {
        const t = frameTimes[i];
        while (j < tl.length && tl[j].t <= t) { cur = tl[j].speed; j++; }
        if (cur != null) {
          sum[i] += cur; cnt[i]++;
          if (ffs) { rsum[i] += Math.max(0, Math.min(1, cur / ffs)); rcnt[i]++; }
        }
      }
    }
    const fas = input.frameAvgSpeed;
    const useFas = !!(fas && fas.length === N);
    for (let i = 0; i < N; i++) {
      const v = useFas ? fas[i] : null;
      avgSpeed[i] = v != null ? v : (cnt[i] ? sum[i] / cnt[i] : 0);
      avgRatio[i] = rcnt[i] ? rsum[i] / rcnt[i] : 0;
    }

    const totalTrips = input.totalTrips ? input.totalTrips : lastSeen.size * vehScale;
    return {
      active, avgSpeed, avgRatio, completed, started, totalTrips,
      maxActive: Math.max(1, ...active),
      maxSpeed: Math.max(1, ...avgSpeed),
      maxCompleted: Math.max(1, ...completed),
      maxStarted: Math.max(1, ...started),
    };
  }

  // フレーム間隔の代表値（中央値）．フレームは走行車両がいる時刻にしか無いので，
  // 需要の空白時間や終了後は間隔が大きく開く．その検出に使う．
  function typicalFrameStep(frameTimes) {
    const n = frameTimes.length;
    if (n < 2) return 1;
    const d = new Array(n - 1);
    for (let i = 1; i < n; i++) d[i - 1] = frameTimes[i] - frameTimes[i - 1];
    d.sort((a, b) => a - b);
    return d[(n - 1) >> 1] || 1;
  }

  // 時刻 t がフレームで覆われていない（最寄りのフレームが通常間隔より離れている）か
  function isFrameGap(frameTimes, t) {
    const n = frameTimes.length;
    if (!n) return true;
    const i = lowerBoundIndex(frameTimes, t);
    const prev = frameTimes[i] <= t ? frameTimes[i] : null;
    const next = frameTimes[i] > t ? frameTimes[i] : (i + 1 < n ? frameTimes[i + 1] : null);
    const dPrev = prev == null ? Infinity : t - prev;
    const dNext = next == null ? Infinity : next - t;
    return Math.min(dPrev, dNext) > typicalFrameStep(frameTimes) * 1.5;
  }

  // 現在時刻 t で表示する値．フレームは走行車両がいる時刻にしか無いので，
  // フレームが無い時間帯（終了後・需要の空白時間・開始前）は 流入 − 到着 で走行中台数を出す．
  // 旧実装は最寄りのフレームを参照し続け，空白時間に「走行中 5 台」が残っていた．
  function statValuesAt(stats, frameTimes, tripSeries, t) {
    const idx = lowerBoundIndex(frameTimes, t);
    const i = Math.max(0, idx);
    let active = stats.active[i] || 0;
    let entered = stats.started[i] || 0;
    let completed = stats.completed[i] || 0;
    let avgSpeed = stats.avgSpeed[i] || 0;
    if (tripSeries && tripSeries.t && tripSeries.t.length) {
      const tr = tripAt(tripSeries, t);
      entered = tr.entered; completed = tr.completed;
      if (isFrameGap(frameTimes, t)) {
        active = Math.max(0, entered - completed);
        avgSpeed = 0;   // 走行車両がいない
      }
    }
    return { idx: i, active, entered, completed, avgSpeed };
  }

  return { EMPTY_FRAME, decodeFrame, decodeFrames, lowerBoundIndex, tripAt, phaseIndexAt,
           computeStats, statValuesAt, isFrameGap, typicalFrameStep };
});
