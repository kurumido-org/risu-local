// RISU UI — 結果の読み込みと Canvas 描画（静的レイヤー・車両・信号）．
// index.html の <script> から切り出したもの（内容・順序は同じ）．classic script として
// 同じグローバルスコープを共有するので，index.html の読み込み順を変えないこと．
// DOM に触らないロジックは risu-core.js（node でテスト）へ．
// ── 結果ロード＆描画 ──
async function loadResult(simId) {
  try {
    // 前回の状態を解放（新規データ構築中のピーク消費を抑える）
    releasePrevResult();

    const res  = await fetch(`${API}/results/${simId}`);
    let data = await res.json();
    // 新スキーマ (risu_schema_version 1.0): { scenario, source, result: {...} }
    let r = data.result || data;  // 旧形式互換
    geoData    = r.geojson;
    signalsData = r.signals || [];
    scenarioData = data.scenario || null;
    // 台数換算（vehicle_counts が無い古い JSON は プラトン数 × deltan × 間引き で近似）
    vehicleCounts = Array.isArray(r.vehicle_counts) ? r.vehicle_counts : null;
    vehScale = ((scenarioData && scenarioData.deltan) || 1) * (r.vehicle_sample_step || 1);
    tripSeries = (r.trip_series && Array.isArray(r.trip_series.t)) ? r.trip_series : null;
    frameAvgSpeed = Array.isArray(r.frame_avg_speed) ? r.frame_avg_speed : null;
    frameIntervalS = (r.frame_interval_s > 0) ? r.frame_interval_s : null;
    simStats = r.stats || null;
    // 座標→ノード名のルックアップを構築
    nodeByCoord = {};
    if (scenarioData && scenarioData.nodes) {
      for (const n of scenarioData.nodes) {
        nodeByCoord[`${n.x},${n.y}`] = n;
      }
    }
    resetCamera();
    console.log(`[RISU] features: ${r.geojson?.features?.length}, signals: ${signalsData.length}`);
    frameTimes = r.frame_times || [];

    // link_names テーブルとリンクあたりの ffs ルックアップを構築
    linkNames = r.link_names || (geoData?.features || []).map(f => f.properties.name);
    linkFfsByIdx = new Float32Array(linkNames.length);
    {
      const ffsMap = {};
      for (const f of (geoData?.features || [])) ffsMap[f.properties.name] = f.properties.free_flow_speed || 0;
      for (let i = 0; i < linkNames.length; i++) linkFfsByIdx[i] = ffsMap[linkNames[i]] || 0;
    }

    // フレームを列指向 → TypedArray に変換（大幅なメモリ削減）．復号は RisuCore.decodeFrames
    // （v3 / v2 / 旧 array-of-objects の 3 形式．node のテスト tests/js で検証）
    const rawFrames = r.frames || {};
    // frame_format が無くても，raw.ids が存在すれば columnar 形式とみなす．
    // v3 は送出時に量子化・差分符号化された形式（サーバーの _encode_frames_v3）．
    // ダウンロード済みの古い JSON は v2 のままなので両方を読めるようにしておく．
    const isV3 = r.frame_format === 'columnar_v3';
    framesData = RisuCore.decodeFrames(rawFrames, { isV3, linkNames, release: true });
    simTmax    = r.tmax;
    lastSimId  = simId;

    const s = r.stats || {};
    document.getElementById('s-trips').textContent = s.total_trips ?? '--';
    document.getElementById('s-comp').textContent  = s.completed_trips ?? '--';
    document.getElementById('s-tt').textContent    = s.average_travel_time_s ? s.average_travel_time_s + 's' : '--';
    document.getElementById('s-calc').textContent  = s.simulation_time_s ?? '--';
    document.getElementById('sim-id-label').textContent = simId;
    const nFeatures = r.geojson?.features?.length || 0;
    document.getElementById('link-count-label').textContent = `(${nFeatures} links)`;
    // 結果の前提（集計単位・車頭時間・乱数シード・需要の出所）を画面で確認できるようにする
    {
      const sc = scenarioData || {};
      const src = data.source || {};
      const parts = [`deltan ${sc.deltan ?? '?'}`,
                     `反応時間 ${sc.reaction_time ?? '既定 1.0'} s`,
                     `seed ${sc.random_seed ?? 'なし（実行ごとに変動）'}`];
      if (src.demand && src.demand.note) parts.push('需要: ' + src.demand.note);
      else if (src.type === 'osm') parts.push('需要: OSM 取込時の自動生成（周縁ノード間の仮定値）');
      else if (src.type) parts.push(`出所: ${src.type}`);
      const el = document.getElementById('scn-params-label');
      if (el) { el.textContent = parts.slice(0, 3).join(' · '); el.title = parts.join('\n'); }
    }
    document.getElementById('dl-btn').style.display = 'inline-block';
    document.getElementById('dl-scn-btn').style.display = 'inline-block';

    timeSlider.value = 0;
    document.getElementById('time-control').classList.add('visible');
    document.getElementById('legend').classList.add('visible');
    document.getElementById('canvas-placeholder').style.display = 'none';

    canvasDirty = true;
    drawFrame();
    if (playing) stopPlay();
    setTimeout(startPlay, 100);

    // JSON 全体への参照を切る
    r = null; data = null;
  } catch(e) {
    console.error(e);
  }
}

// 前回結果を明示解放 — 新規ロード直前に呼ぶことで peak memory を抑える
function releasePrevResult() {
  geoData = null;
  framesData = null;
  frameTimes = [];
  linkNames = [];
  linkFfsByIdx = null;
  signalsData = [];
  scenarioData = null;
  vehicleCounts = null;
  vehScale = 1;
  tripSeries = null;
  frameAvgSpeed = null;
  frameIntervalS = null;
  simStats = null;
  nodeByCoord = {};
  hitTargets = { nodes: [], links: [] };
  statsSeries = null;
  if (playing && typeof stopPlay === 'function') stopPlay();
}

// 現在時刻での信号現示 index を計算
// sig: 信号オブジェクト全体（phase_log があれば優先使用）
function currentPhaseIdx(sig, t) {
  // UXsim の実ログ（phase_log，1 件 = deltat 秒）を最優先．ロジックは RisuCore.phaseIndexAt
  return RisuCore.phaseIndexAt(sig, t, simTmax);
}

// 空フレーム（undefined アクセスのフォールバック用）
const EMPTY_FRAME = { n: 0, ids: new Uint32Array(0), xs: new Float32Array(0),
  ys: new Float32Array(0), vs: new Float32Array(0), alphas: new Float32Array(0),
  li: new Uint16Array(0) };

// ホバー検出用ヒットターゲット（drawFrame ごとに更新）
let hitTargets = { nodes: [], links: [] };

// TypedArray フレームから i 番目の車両を一時オブジェクトとして取得
function vehAt(frame, i) {
  const lIdx = frame.li[i];
  return {
    id:    frame.ids[i],
    x:     frame.xs[i],
    y:     frame.ys[i],
    v:     frame.vs[i],
    alpha: frame.alphas[i],
    li:    lIdx,
    link:  linkNames[lIdx],
    ffs:   linkFfsByIdx ? linkFfsByIdx[lIdx] : 0,
  };
}

// ── 速度→色変換 ──
function speedToColor(speed, ffs) {
  const ratio = Math.max(0, Math.min(speed / ffs, 1));
  return `hsl(${ratio * 120}, 85%, ${40 + ratio * 15}%)`;
}

// ── フレーム時刻の最近傍インデックスを見つける ──
function findNearestFrameIdx(tCurrent) {
  return RisuCore.lowerBoundIndex(frameTimes, tCurrent);
}

// ── 車両のスクリーン座標を計算 ──
// info: linkDrawInfo[link]（null なら生座標を投影），t: alpha (0~1)，
// out: [sx, sy] を書き込む配列（毎車両の配列割り当てを避けるため呼び出し側で使い回す）
function _posOnLink(info, t, x, y, px, py, out) {
  if (!info) { out[0] = px(x); out[1] = py(y); return; }

  // 多点パス上の位置（OSM 道路形状）: 静的レイヤー構築時に計算した累積長 segCum を
  // 二分探索（毎車両・毎フレームで全セグメント長を再計算しない）
  if (info.multipoint && info.screenCoords) {
    const sc = info.screenCoords, cum = info.segCum, total = info.totalLen;
    if (!cum || !(total > 0)) { out[0] = sc[0][0]; out[1] = sc[0][1]; return; }
    const target = t * total;
    let lo = 1, hi = cum.length - 1;
    while (lo < hi) { const m = (lo + hi) >> 1; if (cum[m] >= target) hi = m; else lo = m + 1; }
    const segLen = cum[lo] - cum[lo - 1];
    const frac = Math.min(1, (target - cum[lo - 1]) / (segLen || 1));
    out[0] = sc[lo - 1][0] * (1 - frac) + sc[lo][0] * frac;
    out[1] = sc[lo - 1][1] * (1 - frac) + sc[lo][1] * frac;
    return;
  }

  // 双方向: ベジェ曲線上
  if (info.bidir && info.cpx !== undefined) {
    const u = 1 - t;
    out[0] = u * u * info.ax + 2 * u * t * info.cpx + t * t * info.bx;
    out[1] = u * u * info.ay + 2 * u * t * info.cpy + t * t * info.by;
    return;
  }

  // 直線
  out[0] = info.ax * (1 - t) + info.bx * t;
  out[1] = info.ay * (1 - t) + info.by * t;
}

// 互換 API（vehAt() の一時オブジェクトを受け取る版）
function vehicleScreenPos(veh, linkDrawInfo, px, py) {
  const out = [0, 0];
  _posOnLink(veh.link ? linkDrawInfo[veh.link] : null, veh.alpha, veh.x, veh.y, px, py, out);
  return out;
}

// ── 静的レイヤー（リンク・ノード・ラベル）のキャッシュ ──
// 再生中 drawFrame は 60fps で呼ばれるが，リンク・ノードの描画結果はカメラ・キャンバス
// サイズ・描画モード（LINK モードでは時刻フレーム）が変わらない限り同一なので，
// オフスクリーンキャンバスに 1 回描き，以後は drawImage で貼る．
// （OSM の数千リンク × 多点パスを毎フレーム stroke するのが従来の最大コストだった）
// linkDrawInfo（車両位置計算用）と hitTargets（ホバー用）も同時にキャッシュする．
let _staticLayer = { key: '', geo: null, canvas: null, linkDrawInfo: null, hitTargets: null };

function _getStaticLayer(features, geoStatic, px, py, isLinkMode, colorKey, denseNetwork, dpr, tCurrent) {
  const key = `${canvasEl.width}x${canvasEl.height}|${canvasW}x${canvasH}|${dpr}|` +
              `${camZoom},${camPanX},${camPanY}|${drawMode}|${colorKey}`;
  if (_staticLayer.geo === geoData && _staticLayer.key === key && _staticLayer.canvas) {
    return _staticLayer;
  }

  let cv = _staticLayer.canvas;
  if (!cv) cv = document.createElement('canvas');
  if (cv.width !== canvasEl.width || cv.height !== canvasEl.height) {
    cv.width = canvasEl.width; cv.height = canvasEl.height;
  }
  const ctx = cv.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, canvasW, canvasH);

  const hits = { nodes: [], links: [] };
  const edgeSet = geoStatic.edgeSet;
  const ARC_OFFSET = 18;
  const linkDrawInfo = {};

  // ── パスを描く汎用関数（多点 LineString 対応）──
  function traceLinkPath(coords) {
    ctx.beginPath();
    ctx.moveTo(px(coords[0][0]), py(coords[0][1]));
    for (let i = 1; i < coords.length; i++) {
      ctx.lineTo(px(coords[i][0]), py(coords[i][1]));
    }
  }

  // ── リンク描画 ──
  for (const f of features) {
    const coords = f.geometry.coordinates;
    const linkName = f.properties.name;
    const ffs = f.properties.free_flow_speed || 20;
    // 始点・終点のスクリーン座標
    const ax = px(coords[0][0]), ay = py(coords[0][1]);
    const bx = px(coords[coords.length-1][0]), by = py(coords[coords.length-1][1]);
    const isMultiPoint = coords.length > 2;

    // LINKモード: 速度で色分け / それ以外: グレー（建物あり時は白い道路）
    let color = '#d0d4dc';
    if (isLinkMode) {
      const tl = f.properties.timeline;
      let speed = ffs;
      if (tl && tl.length) {
        let lo = 0, hi = tl.length - 1;
        while (lo < hi) { const m = (lo+hi+1)>>1; tl[m].t <= tCurrent ? lo=m : hi=m-1; }
        speed = tl[lo].speed ?? ffs;
      }
      color = speedToColor(speed, ffs);
    }

    const [x0,y0] = coords[0], [x1,y1] = coords[coords.length - 1];
    const bidir = edgeSet.has(`${x1},${y1}->${x0},${y0}`);

    if (isMultiPoint || denseNetwork) {
      // 多点パス描画（OSM道路形状，または密なネットワーク）
      traceLinkPath(coords);
      ctx.strokeStyle = color;
      ctx.lineWidth = isLinkMode ? 5 : 4;
      ctx.lineCap = 'round';
      ctx.lineJoin = 'round';
      ctx.stroke();

      if (!denseNetwork) {
        // ラベル（中間点付近）
        const midIdx = Math.floor(coords.length / 2);
        const mx = px(coords[midIdx][0]), my = py(coords[midIdx][1]);
        ctx.fillStyle = 'rgba(100,110,130,0.4)';
        ctx.font = '9px "IBM Plex Mono"';
        ctx.textAlign = 'center';
        ctx.fillText(linkName, mx, my - 8);
      }

      // linkDrawInfo: 多点パスのスクリーン座標と累積長を保存（車両描画用）
      const screenCoords = coords.map(([cx,cy]) => [px(cx), py(cy)]);
      const segCum = new Float64Array(screenCoords.length);
      let acc = 0;
      for (let i = 1; i < screenCoords.length; i++) {
        acc += Math.hypot(screenCoords[i][0] - screenCoords[i-1][0],
                          screenCoords[i][1] - screenCoords[i-1][1]);
        segCum[i] = acc;
      }
      linkDrawInfo[linkName] = { bidir, multipoint: true, screenCoords, segCum, totalLen: acc, ax, ay, bx, by };
    } else if (bidir) {
      // 2点 + 双方向: 円弧描画
      const dx = bx - ax, dy = by - ay;
      const len = Math.hypot(dx, dy) || 1;
      const nx = -dy / len, ny = dx / len;
      const cpx = (ax+bx)/2 + nx*ARC_OFFSET, cpy = (ay+by)/2 + ny*ARC_OFFSET;

      ctx.beginPath();
      ctx.moveTo(ax, ay);
      ctx.quadraticCurveTo(cpx, cpy, bx, by);
      ctx.strokeStyle = color;
      ctx.lineWidth = isLinkMode ? 5 : 4;
      ctx.lineCap = 'round';
      ctx.stroke();

      // 矢頭
      const t2 = 0.6;
      const mx = (1-t2)*(1-t2)*ax + 2*(1-t2)*t2*cpx + t2*t2*bx;
      const my = (1-t2)*(1-t2)*ay + 2*(1-t2)*t2*cpy + t2*t2*by;
      const tdx = 2*(1-t2)*(cpx-ax) + 2*t2*(bx-cpx);
      const tdy = 2*(1-t2)*(cpy-ay) + 2*t2*(by-cpy);
      const ang = Math.atan2(tdy, tdx);
      ctx.beginPath();
      ctx.moveTo(mx+Math.cos(ang)*6,     my+Math.sin(ang)*6);
      ctx.lineTo(mx+Math.cos(ang-2.5)*5, my+Math.sin(ang-2.5)*5);
      ctx.lineTo(mx+Math.cos(ang+2.5)*5, my+Math.sin(ang+2.5)*5);
      ctx.closePath();
      ctx.fillStyle = isLinkMode ? color : '#bbc0ca';
      ctx.fill();

      // ラベル
      const lmx = 0.25*ax + 0.5*cpx + 0.25*bx;
      const lmy = 0.25*ay + 0.5*cpy + 0.25*by;
      ctx.fillStyle = 'rgba(100,110,130,0.4)';
      ctx.font = '9px "IBM Plex Mono"';
      ctx.textAlign = 'center';
      ctx.fillText(linkName, lmx + nx*10, lmy + ny*10);

      linkDrawInfo[linkName] = { bidir: true, ax, ay, bx, by, cpx, cpy };
    } else {
      // 2点 + 一方向: 直線描画
      ctx.beginPath();
      ctx.moveTo(ax, ay);
      ctx.lineTo(bx, by);
      ctx.strokeStyle = color;
      ctx.lineWidth = isLinkMode ? 7 : 5;
      ctx.lineCap = 'round';
      ctx.stroke();

      const ang = Math.atan2(by-ay, bx-ax);
      const mx = (ax+bx)/2, my = (ay+by)/2;
      ctx.beginPath();
      ctx.moveTo(mx+Math.cos(ang)*7,     my+Math.sin(ang)*7);
      ctx.lineTo(mx+Math.cos(ang-2.5)*6, my+Math.sin(ang-2.5)*6);
      ctx.lineTo(mx+Math.cos(ang+2.5)*6, my+Math.sin(ang+2.5)*6);
      ctx.closePath();
      ctx.fillStyle = isLinkMode ? color : '#bbc0ca';
      ctx.fill();

      ctx.fillStyle = 'rgba(100,110,130,0.4)';
      ctx.font = '10px "IBM Plex Mono"';
      ctx.textAlign = 'center';
      ctx.fillText(linkName, mx, my - 10);

      linkDrawInfo[linkName] = { bidir: false, ax, ay, bx, by };
    }
  }

  // ── ヒットターゲット蓄積（リンク） ──
  for (const f of features) {
    const linkName = f.properties.name;
    const info = linkDrawInfo[linkName];
    if (!info) continue;
    hits.links.push({
      name: linkName,
      properties: f.properties,
      info,  // ax/ay/bx/by/cpx/cpy/multipoint/screenCoords
    });
  }

  // ── ノード描画 ──
  if (!denseNetwork) {
    // 座標 → 元ノード情報の辞書（nodeByCoord は元シナリオ座標で構築済）
    const nodes = {};
    for (const f of features) {
      const c = f.geometry.coordinates;
      const [x0,y0] = c[0], [x1,y1] = c[c.length - 1];
      nodes[`${x0},${y0}`] = { sx: px(x0), sy: py(y0), x0, y0 };
      nodes[`${x1},${y1}`] = { sx: px(x1), sy: py(y1), x0: x1, y0: y1 };
    }
    // ヒットターゲットに登録（元ノード情報があれば名前と signal も）
    for (const np of Object.values(nodes)) {
      const meta = nodeByCoord[`${np.x0},${np.y0}`];
      hits.nodes.push({
        sx: np.sx, sy: np.sy,
        name: meta ? meta.name : null,
        x: np.x0, y: np.y0,
        signal: meta && meta.signal ? meta.signal : null,
      });
    }
    for (const np of Object.values(nodes)) {
      const nx = np.sx, ny = np.sy;
      ctx.beginPath();
      ctx.arc(nx, ny, 6, 0, Math.PI*2);
      ctx.fillStyle = '#ffffff';
      ctx.strokeStyle = '#0d9668';
      ctx.lineWidth = 2;
      ctx.fill();
      ctx.stroke();
    }
  }

  _staticLayer = { key, geo: geoData, canvas: cv, linkDrawInfo, hitTargets: hits };
  return _staticLayer;
}

// ── 車両位置の補間バッファ（フレームごとの割り当てを避けるため使い回す） ──
// _vbuf: 補間済み車両の {ids, sx, sy, v, ffs}，n 台分が有効
const _vbuf = { cap: 0, ids: null, sx: null, sy: null, v: null, ffs: null };
function _ensureVbuf(n) {
  if (_vbuf.cap >= n) return;
  const cap = Math.max(1024, n * 2);
  _vbuf.cap = cap;
  _vbuf.ids = new Uint32Array(cap);
  _vbuf.sx = new Float32Array(cap); _vbuf.sy = new Float32Array(cap);
  _vbuf.v = new Float32Array(cap);  _vbuf.ffs = new Float32Array(cap);
}
const _posScratch = [0, 0];

// フレーム内の ids が昇順か（サーバーは車両登録順に出力するので通常は昇順）．
// 昇順なら 2 フレーム間の車両対応をマージ結合で O(nA+nB)・割り当てなしで取れる．
function _frameIdsSorted(frame) {
  if (frame._sorted === undefined) {
    const ids = frame.ids; let ok = true;
    for (let i = 1; i < frame.n; i++) if (ids[i] < ids[i-1]) { ok = false; break; }
    frame._sorted = ok;
  }
  return frame._sorted;
}
function _frameIdIndex(frame) {
  if (!frame._idIndex) {
    const m = new Map();
    for (let i = 0; i < frame.n; i++) m.set(frame.ids[i], i);
    frame._idIndex = m;
  }
  return frame._idIndex;
}

// 時刻 tNow の車両位置をフレーム間線形補間して _vbuf に書き込み，台数を返す．
function _interpolateVehicles(tNow, linkDrawInfo, px, py) {
  // どのフレームで描くかは統計と同じ判定（RisuCore.frameWindow）．
  // 旧実装は最寄りのフレームを補間し続け，需要の空白時間や全車両到着後にも点が残っていた．
  const w = RisuCore.frameWindow(frameTimes, tNow, frameIntervalS);
  if (w.mode === 'none') return 0;
  const idxA = w.i;
  const tA = frameTimes[idxA];
  const frameA = framesData[String(tA)] || EMPTY_FRAME;
  const out = _posScratch;
  const B = _vbuf;

  const writeVeh = (n, frame, i, sx, sy, v) => {
    B.ids[n] = frame.ids[i]; B.sx[n] = sx; B.sy[n] = sy; B.v[n] = v;
    B.ffs[n] = linkFfsByIdx ? linkFfsByIdx[frame.li[i]] : 0;
  };
  const posOf = (frame, i) => {
    const li = frame.li[i];
    _posOnLink(linkDrawInfo[linkNames[li]] || null, frame.alphas[i], frame.xs[i], frame.ys[i], px, py, out);
  };

  // 厳密一致 / 直後の据え置き → 補間不要
  if (w.mode !== 'interp') {
    _ensureVbuf(frameA.n);
    for (let i = 0; i < frameA.n; i++) {
      posOf(frameA, i);
      writeVeh(i, frameA, i, out[0], out[1], frameA.vs[i]);
    }
    return frameA.n;
  }

  const tB = frameTimes[w.j];
  const frameB = framesData[String(tB)] || EMPTY_FRAME;
  const frac = w.frac;
  const emitBOnly = frac > 0.5;  // B にだけいる車両（新規流入）は後半だけ描く
  _ensureVbuf(frameA.n + frameB.n);
  let n = 0;
  const idsA = frameA.ids, idsB = frameB.ids;

  const emitPair = (i, j) => {
    posOf(frameA, i); const sxA = out[0], syA = out[1];
    posOf(frameB, j);
    writeVeh(n, frameA, i, sxA + (out[0] - sxA) * frac, syA + (out[1] - syA) * frac,
             frameA.vs[i] + (frameB.vs[j] - frameA.vs[i]) * frac);
    n++;
  };
  const emitA = (i) => { posOf(frameA, i); writeVeh(n, frameA, i, out[0], out[1], frameA.vs[i]); n++; };
  const emitB = (j) => { posOf(frameB, j); writeVeh(n, frameB, j, out[0], out[1], frameB.vs[j]); n++; };

  if (_frameIdsSorted(frameA) && _frameIdsSorted(frameB)) {
    // マージ結合
    let j = 0;
    for (let i = 0; i < frameA.n; i++) {
      const id = idsA[i];
      while (j < frameB.n && idsB[j] < id) { if (emitBOnly) emitB(j); j++; }
      if (j < frameB.n && idsB[j] === id) { emitPair(i, j); j++; }
      else emitA(i);
    }
    if (emitBOnly) for (; j < frameB.n; j++) emitB(j);
  } else {
    // 非整列データ（旧形式）: id → index マップ
    const mapB = _frameIdIndex(frameB);
    const seen = new Uint8Array(frameB.n);
    for (let i = 0; i < frameA.n; i++) {
      const j = mapB.get(idsA[i]);
      if (j !== undefined) { seen[j] = 1; emitPair(i, j); } else emitA(i);
    }
    if (emitBOnly) for (let j = 0; j < frameB.n; j++) if (!seen[j]) emitB(j);
  }
  return n;
}

// 車両ドットの描画: 色（速度比）を NB 段階に量子化し，同色をまとめて 1 パスで fill/stroke．
// 1 台ごとに beginPath/fill/stroke すると数千台で描画コストの大半を占めるため．
const _DOT_NB = 24;
const _DOT_COLORS = Array.from({ length: _DOT_NB }, (_, b) => speedToColor(b / (_DOT_NB - 1), 1));
const _dotBuckets = Array.from({ length: _DOT_NB }, () => []);
function _drawVehicleDots(ctx, n, r) {
  if (!n) return;
  const B = _vbuf;
  for (let b = 0; b < _DOT_NB; b++) _dotBuckets[b].length = 0;
  for (let k = 0; k < n; k++) {
    const ffs = B.ffs[k];
    let ratio = ffs > 0 ? B.v[k] / ffs : 1;
    ratio = ratio > 1 ? 1 : (ratio >= 0 ? ratio : 0);
    _dotBuckets[Math.round(ratio * (_DOT_NB - 1))].push(k);
  }
  ctx.lineWidth = 1;
  ctx.strokeStyle = 'rgba(255,255,255,0.7)';
  const TAU = Math.PI * 2;
  for (let b = 0; b < _DOT_NB; b++) {
    const list = _dotBuckets[b];
    if (!list.length) continue;
    ctx.beginPath();
    for (let q = 0; q < list.length; q++) {
      const k = list[q], x = B.sx[k], y = B.sy[k];
      ctx.moveTo(x + r, y);
      ctx.arc(x, y, r, 0, TAU);
    }
    ctx.fillStyle = _DOT_COLORS[b];
    ctx.fill();
    ctx.stroke();
  }
}

// ── 描画フレーム ──
// geoData ごとに不変な計算（座標バウンド・双方向判定）のキャッシュ．
// 再生中は drawFrame が 60fps で呼ばれるため，毎フレームの全リンク走査を避ける．
let _geoStaticCache = { ref: null, bbox: null, edgeSet: null };
function _getGeoStatic(features) {
  if (_geoStaticCache.ref !== geoData) {
    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
    for (const f of features) {
      for (const [x, y] of f.geometry.coordinates) {
        if (x < minX) minX = x; if (x > maxX) maxX = x;
        if (y < minY) minY = y; if (y > maxY) maxY = y;
      }
    }
    const edgeSet = new Set();
    for (const f of features) {
      const c = f.geometry.coordinates;
      const [x0,y0] = c[0], [x1,y1] = c[c.length - 1];
      edgeSet.add(`${x0},${y0}->${x1},${y1}`);
    }
    _geoStaticCache = { ref: geoData, bbox: { minX, maxX, minY, maxY }, edgeSet };
  }
  return _geoStaticCache;
}

function drawFrame() {
  if (editMode) { drawEditFrame(); return; }  // 編集モード中は全呼び出し元をエディタ描画に委譲
  if (!geoData) return;
  if (canvasDirty) resizeCanvas();

  const dpr = Math.min(1.5, window.devicePixelRatio || 1);
  const ctx = canvasEl.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, canvasW, canvasH);

  const tRatio   = timeSlider.value / 1000;
  const tCurrent = Math.round(simTmax * tRatio);
  timeLabel.textContent = `t = ${tCurrent} s`;

  const features = geoData.features;
  if (!features.length) return;

  const _geoStatic = _getGeoStatic(features);

  // 座標バウンド（道路座標のみでスケール決定）
  let { minX, maxX, minY, maxY } = _geoStatic.bbox;
  // 少しマージンを追加（道路範囲の10%）
  const marginX = (maxX - minX) * 0.1 || 100;
  const marginY = (maxY - minY) * 0.1 || 100;
  minX -= marginX; maxX += marginX;
  minY -= marginY; maxY += marginY;

  const pad = 60;
  const rangeX = maxX - minX || 1, rangeY = maxY - minY || 1;
  const baseScale = Math.min((canvasW - pad*2) / rangeX, (canvasH - pad*2) / rangeY);
  const baseOffX = pad + (canvasW - pad*2 - rangeX * baseScale) / 2;
  const baseOffY = pad + (canvasH - pad*2 - rangeY * baseScale) / 2;
  // カメラ変換を適用
  const scaleV = baseScale * camZoom;
  const offX = baseOffX * camZoom + camPanX;
  const px = x => offX + (x - minX) * scaleV;
  const py = y => (canvasH - baseOffY) * camZoom + camPanY - (y - minY) * scaleV;

  const isLinkMode = drawMode === 'link';
  const denseNetwork = features.length > 30; // OSM等の密なネットワーク

  // ── 静的レイヤー（リンク・ノード）: キャッシュから貼る ──
  // LINK モードの色は timeline の時刻グリッド（= frame_times）単位でしか変わらないので，
  // 該当フレーム index をキーにする（フレーム情報が無ければ秒単位）．
  const colorKey = isLinkMode ? (frameTimes.length ? findNearestFrameIdx(tCurrent) : tCurrent) : -1;
  const layer = _getStaticLayer(features, _geoStatic, px, py, isLinkMode, colorKey, denseNetwork, dpr, tCurrent);
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.drawImage(layer.canvas, 0, 0);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  const linkDrawInfo = layer.linkDrawInfo;
  hitTargets = layer.hitTargets;

  // ── 車両描画 (vehicle / trail モード) ── フレーム間線形補間で滑らかに描画 ──
  if ((drawMode === 'vehicle' || drawMode === 'trail') && framesData && frameTimes.length) {
    const curIdx = findNearestFrameIdx(tCurrent);
    if (curIdx < 0) return;

    // TRAIL モード: 過去フレームの軌跡を描画（TypedArray フレーム対応）
    if (drawMode === 'trail') {
      const startIdx = Math.max(0, curIdx - TRAIL_LENGTH);
      const trailMap = new Map();
      const out = _posScratch;

      for (let fi = startIdx; fi <= curIdx; fi++) {
        const tKey = String(frameTimes[fi]);
        const frame = framesData[tKey];
        if (!frame || frame.n === 0) continue;
        for (let i = 0; i < frame.n; i++) {
          const id = frame.ids[i], li = frame.li[i];
          let tr = trailMap.get(id);
          if (!tr) { tr = []; trailMap.set(id, tr); }
          _posOnLink(linkDrawInfo[linkNames[li]] || null, frame.alphas[i], frame.xs[i], frame.ys[i], px, py, out);
          tr.push(out[0], out[1], frame.vs[i], linkFfsByIdx ? linkFfsByIdx[li] : 0);  // 4 要素/点（オブジェクト割り当て回避）
        }
      }

      // 現在の補間位置もトレイル末尾に追加
      const nNow = _interpolateVehicles(tCurrent, linkDrawInfo, px, py);
      for (let k = 0; k < nNow; k++) {
        const id = _vbuf.ids[k];
        let tr = trailMap.get(id);
        if (!tr) { tr = []; trailMap.set(id, tr); }
        tr.push(_vbuf.sx[k], _vbuf.sy[k], _vbuf.v[k], _vbuf.ffs[k]);
      }

      // 軌跡セグメントを (色バケット, 位置 i, 軌跡長) ごとにまとめて 1 回で stroke する．
      // 線種は i / trail.length だけで決まるので，同じ (i, len) のセグメントは同一スタイル．
      // （1 セグメントごとの beginPath/stroke は 1 万台 × 8 フレームで数百 ms かかった）
      const groups = new Map();
      for (const trail of trailMap.values()) {
        const len = trail.length >> 2;  // 点数（[sx, sy, v, ffs] × len）
        if (len < 2) continue;
        const lastV = trail[(len - 1) * 4 + 2], lastFfs = trail[(len - 1) * 4 + 3];
        let ratio = lastFfs > 0 ? lastV / lastFfs : 1;
        ratio = ratio > 1 ? 1 : (ratio >= 0 ? ratio : 0);
        const bucket = Math.round(ratio * (_DOT_NB - 1));
        for (let i = 1; i < len; i++) {
          const key = (bucket * 64 + len) * 64 + i;
          let g = groups.get(key);
          if (!g) { g = { bucket, i, len, pts: [] }; groups.set(key, g); }
          const a = (i - 1) * 4, b = i * 4;
          g.pts.push(trail[a], trail[a+1], trail[b], trail[b+1]);
        }
      }
      ctx.lineCap = 'round';
      for (const g of groups.values()) {
        const pts = g.pts;
        ctx.beginPath();
        for (let k = 0; k < pts.length; k += 4) {
          ctx.moveTo(pts[k], pts[k+1]);
          ctx.lineTo(pts[k+2], pts[k+3]);
        }
        ctx.strokeStyle = _DOT_COLORS[g.bucket];
        ctx.globalAlpha = (g.i / g.len) * 0.6 + 0.1;
        ctx.lineWidth = (g.i / g.len) * 2.5 + 0.5;
        ctx.stroke();
      }
      ctx.globalAlpha = 1.0;
    }

    // 現在位置の車両ドット（補間済み，色ごとにバッチ描画）
    const nVeh = _interpolateVehicles(tCurrent, linkDrawInfo, px, py);
    _drawVehicleDots(ctx, nVeh, drawMode === 'trail' ? 3.5 : 4);
  }

  // ── 信号現示の可視化 ──
  // 旧実装は流入リンクごとに固定サイズの信号機筐体（約 30×12 px）を停止線から 10 px
  // 手前に描いていたため，4 枝交差点や双方向円弧の末端で必ず重なっていた．
  // 現在は
  //  (a) 停止線バー: 各流入リンクの停止位置に道路を横切る短い線（赤/緑）．
  //      自分のリンク上にしか置かれないので枝数に関係なく重ならない
  //  (b) LOD: 流入リンクの画面長が SIGNAL_LOD_PX 未満（ズームアウト時）なら描かない
  //      （OSM の密な網で破綻しない．信号の有無はノードのツールチップで分かる）
  //  交差点ノードのリングや現在位置の印は試したが，回って見える・情報が重複するので置かない
  if (signalsData && signalsData.length) _drawSignals(ctx, tCurrent, linkDrawInfo, px, py, denseNetwork);
}

const SIGNAL_LOD_PX = 44;   // 最短の流入リンクがこの画面長未満なら停止線バーを省く
// 青と赤の 2 色だけ．UXsim に黄現示は無く，車両は青か赤かでしか動かない．
// 以前あった「青の残り 3 秒を黄にする」演出は，残り時間を公称サイクル（t=0 起点の設定値）
// から計算していたため，UXsim の実際の切り替え（deltat 刻みで遅れが累積する）と食い違い，
// 現示ログ上はまだ青なのに黄になる誤表示が大半だった（1,200 秒中 320 秒）．
const SIGNAL_COLORS = { red: '#dc3545', green: '#2d9a4a' };

// 流入リンクの停止位置（終端）と進行方向の単位ベクトル，画面上の長さを返す
function _approachGeom(info) {
  let endX, endY, dx, dy, len;
  if (info.multipoint && info.screenCoords) {
    const sc = info.screenCoords;
    const p1 = sc[sc.length - 2], p2 = sc[sc.length - 1];
    endX = p2[0]; endY = p2[1]; dx = p2[0] - p1[0]; dy = p2[1] - p1[1];
    len = info.totalLen || Math.hypot(info.bx - info.ax, info.by - info.ay);
  } else if (info.bidir && info.cpx !== undefined) {
    endX = info.bx; endY = info.by;
    dx = 2 * (info.bx - info.cpx); dy = 2 * (info.by - info.cpy);   // 終点での接線
    len = Math.hypot(info.bx - info.ax, info.by - info.ay);
  } else {
    endX = info.bx; endY = info.by;
    dx = info.bx - info.ax; dy = info.by - info.ay;
    len = Math.hypot(dx, dy);
  }
  const d = Math.hypot(dx, dy) || 1;
  return { endX, endY, tx: dx / d, ty: dy / d, len };
}

function _drawSignals(ctx, tCurrent, linkDrawInfo, px, py, denseNetwork) {
  const C = SIGNAL_COLORS;
  for (const sig of signalsData) {
    const phases = sig.phases || [];
    const cycle = phases.reduce((a, b) => a + b, 0);
    if (!phases.length || cycle <= 0) continue;
    // どの流入が青かは UXsim の実際の現示ログ（phase_log）から取る（currentPhaseIdx）
    const activePhase = currentPhaseIdx(sig, tCurrent);
    if (activePhase < 0) continue;

    // 流入リンクの幾何を集め，最短の画面長で LOD を決める
    const approaches = [];
    let minLen = Infinity;
    for (const [linkName, groupIdx] of Object.entries(sig.groups || {})) {
      const info = linkDrawInfo[linkName];
      if (!info) continue;
      const g = _approachGeom(info);
      // groups の値は実際に適用された現示番号のリスト（省略時は全現示 = 常時青）．旧形式の int も許容
      const isActive = Array.isArray(groupIdx) ? groupIdx.includes(activePhase) : groupIdx === activePhase;
      approaches.push({ g, isActive });
      if (g.len < minLen) minLen = g.len;
    }
    if (!approaches.length || minLen < SIGNAL_LOD_PX) continue;

    // ── 停止線バー（流入リンクごと） ──
    const backOff = denseNetwork ? 7 : 11;  // ノード円（半径 6）の外側に置く
    const halfLen = denseNetwork ? 4.5 : 6; // 道路幅（4〜5 px）より少し長く
    for (const { g, isActive } of approaches) {
      if (g.len < backOff * 2) continue;   // 短すぎてリンクからはみ出すなら省く
      const cx = g.endX - g.tx * backOff, cy = g.endY - g.ty * backOff;
      const nxv = -g.ty, nyv = g.tx;       // 進行方向に垂直
      const color = isActive ? C.green : C.red;
      ctx.save();
      ctx.lineCap = 'round';
      // 縁取り（明るい道路色の上でも読めるように）
      ctx.beginPath();
      ctx.moveTo(cx - nxv * halfLen, cy - nyv * halfLen);
      ctx.lineTo(cx + nxv * halfLen, cy + nyv * halfLen);
      ctx.strokeStyle = 'rgba(0,0,0,0.45)'; ctx.lineWidth = 5;
      ctx.stroke();
      ctx.strokeStyle = color; ctx.lineWidth = 3;
      ctx.stroke();
      // 赤のときは停止位置側に小さな三角（進入不可の向きが分かる）
      if (!isActive) {
        ctx.beginPath();
        ctx.moveTo(cx + g.tx * 2.5, cy + g.ty * 2.5);
        ctx.lineTo(cx - g.tx * 2 + nxv * 2.5, cy - g.ty * 2 + nyv * 2.5);
        ctx.lineTo(cx - g.tx * 2 - nxv * 2.5, cy - g.ty * 2 - nyv * 2.5);
        ctx.closePath();
        ctx.fillStyle = color; ctx.fill();
      }
      ctx.restore();
    }
  }
}

