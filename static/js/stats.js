// RISU UI — 統計パネル・スパークライン・起動時処理．
// index.html の <script> から切り出したもの（内容・順序は同じ）．classic script として
// 同じグローバルスコープを共有するので，index.html の読み込み順を変えないこと．
// DOM に触らないロジックは risu-core.js（node でテスト）へ．
// ══════════════════════════════════════════════════════════════════
// Progressive stat charts
// ══════════════════════════════════════════════════════════════════
let statsSeries = null; // { active[], avgSpeed[], completed[], started[], totalTrips }

function computeStatsSeries() {
  // 集計本体は RisuCore.computeStats（純粋関数，tests/js で検証）．ここはグローバルを渡すだけ．
  statsSeries = RisuCore.computeStats({
    frameTimes, framesData,
    features: (geoData && geoData.features) || [],
    vehicleCounts, vehScale, tripSeries, frameAvgSpeed,
    totalTrips: simStats && simStats.total_trips,
  });
  if (!statsSeries) return;
  const totalLabel = document.getElementById('sv-total-trips');
  if (totalLabel) totalLabel.textContent = statsSeries.totalTrips;
}

function currentTimeSec() {
  const slider = document.getElementById('time-slider');
  const ratio = (parseFloat(slider.value) || 0) / 1000;
  return (simTmax || frameTimes[frameTimes.length - 1] || 0) * ratio;
}

function currentFrameIdx() {
  if (!frameTimes.length) return 0;
  return findNearestFrameIdx(currentTimeSec());
}

// tripSeries を時刻 t で引く（t 以下の最後の点）
function tripAt(t) {
  return RisuCore.tripAt(tripSeries, t);
}

// テーマ色のキャッシュ（getComputedStyle は 60fps 描画では高コスト）．
// テーマ切替（data-theme 変更）で自動的に再取得する．
let _themeColorCache = { key: null, accent: '#dc4a1c', soft: '#d4d4d0', ink: '#171717' };
function _getThemeColors() {
  const key = document.documentElement.dataset.theme || '';
  if (_themeColorCache.key !== key) {
    const cs = getComputedStyle(document.documentElement);
    _themeColorCache = {
      key,
      accent: (cs.getPropertyValue('--accent') || '#dc4a1c').trim(),
      soft:   (cs.getPropertyValue('--line-soft') || '#d4d4d0').trim(),
      ink:    (cs.getPropertyValue('--ink') || '#171717').trim(),
    };
  }
  return _themeColorCache;
}

function drawStatChart(canvas, series, maxY, idx) {
  if (!canvas || !series || !series.length) return;
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth || canvas.offsetWidth || 100;
  const h = canvas.clientHeight || canvas.offsetHeight || 32;
  if (w < 1 || h < 1) return;
  // バッファ再確保はサイズ変更時のみ（width 代入は毎回フルリセットになる）
  const bw = Math.round(w * dpr), bh = Math.round(h * dpr);
  if (canvas.width !== bw || canvas.height !== bh) {
    canvas.width = bw; canvas.height = bh;
  }
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);

  const N = series.length;
  if (N < 2) return;
  const { accent, soft, ink } = _getThemeColors();
  const pad = 2;
  const innerH = h - pad * 2;
  const x = (i) => i * (w / (N - 1));
  const y = (v) => pad + innerH - (Math.max(0, v) / maxY) * innerH;

  // Future portion (light outline of full trajectory)
  ctx.strokeStyle = soft;
  ctx.lineWidth = 1.2;
  ctx.beginPath();
  for (let i = 0; i < N; i++) {
    if (i === 0) ctx.moveTo(x(i), y(series[i]));
    else ctx.lineTo(x(i), y(series[i]));
  }
  ctx.stroke();

  // Past portion (accent filled area + solid line)
  const fillEnd = Math.min(idx, N - 1);
  if (fillEnd >= 1) {
    ctx.fillStyle = accent + '22';
    ctx.beginPath();
    ctx.moveTo(x(0), y(0));
    for (let i = 0; i <= fillEnd; i++) ctx.lineTo(x(i), y(series[i]));
    ctx.lineTo(x(fillEnd), y(0));
    ctx.closePath();
    ctx.fill();

    ctx.strokeStyle = accent;
    ctx.lineWidth = 1.8;
    ctx.beginPath();
    for (let i = 0; i <= fillEnd; i++) {
      if (i === 0) ctx.moveTo(x(i), y(series[i]));
      else ctx.lineTo(x(i), y(series[i]));
    }
    ctx.stroke();
  }

  // Cursor dot
  if (idx >= 0 && idx < N) {
    const cx = x(idx);
    const cy = y(series[idx]);
    ctx.fillStyle = ink;
    ctx.beginPath(); ctx.arc(cx, cy, 3, 0, Math.PI * 2); ctx.fill();
    ctx.strokeStyle = accent;
    ctx.lineWidth = 2;
    ctx.beginPath(); ctx.arc(cx, cy, 3, 0, Math.PI * 2); ctx.stroke();
  }
}

function drawAllStatCharts() {
  if (!statsSeries) return;
  const idx = currentFrameIdx();
  const canvases = document.querySelectorAll('#stats-panel .stat-chart');
  canvases.forEach(card => {
    const metric = card.dataset.metric;
    const canvas = card.querySelector('.sc-canvas');
    if (metric === 'active') drawStatChart(canvas, statsSeries.active, statsSeries.maxActive, idx);
    else if (metric === 'avgSpeed') drawStatChart(canvas, statsSeries.avgSpeed, statsSeries.maxSpeed, idx);
    else if (metric === 'completed') drawStatChart(canvas, statsSeries.completed, statsSeries.maxCompleted, idx);
    else if (metric === 'inflow') drawStatChart(canvas, statsSeries.started, statsSeries.maxStarted, idx);
  });
}

function updateStatValues() {
  if (!statsSeries) return;
  const fmt = (v) => Number.isFinite(v) ? (v >= 100 ? Math.round(v) : v.toFixed(1)) : '—';
  const setText = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
  // 表示値は現在時刻で引く（フレームは走行車両がいる時刻にしか無い）．RisuCore.statValuesAt
  const v = RisuCore.statValuesAt(statsSeries, frameTimes, tripSeries, currentTimeSec());
  setText('sv-active', fmt(v.active));
  setText('sv-speed', fmt(v.avgSpeed));
  setText('sv-completed', fmt(v.completed));
  setText('sv-inflow', fmt(v.entered));
  const totalEl = document.getElementById('sv-total-trips');
  if (totalEl && statsSeries.totalTrips) totalEl.textContent = statsSeries.totalTrips;
}

// Slider → update charts & values
document.getElementById('time-slider').addEventListener('input', () => {
  drawAllStatCharts();
  updateStatValues();
});
// Resize
window.addEventListener('resize', () => {
  drawAllStatCharts();
  drawSparkline();
});

// ── Time label をトータル時間含めて更新 ──
function updateTimeLabel() {
  const slider = document.getElementById('time-slider');
  const label = document.getElementById('time-label');
  if (!label || !frameTimes.length) return;
  const maxT = simTmax || frameTimes[frameTimes.length - 1] || 0;
  const ratio = (parseFloat(slider.value) || 0) / 1000;
  const curT = Math.round(maxT * ratio);
  const pct = maxT ? Math.round(ratio * 100) : 0;
  label.textContent = `${curT}s / ${Math.round(maxT)}s · ${pct}%`;
}
document.getElementById('time-slider').addEventListener('input', updateTimeLabel);

// ── Sparkline: 各時刻のネットワーク平均速度 ──
function drawSparkline() {
  const c = document.getElementById('time-sparkline');
  if (!c || !geoData) return;
  const dpr = window.devicePixelRatio || 1;
  const w = c.clientWidth || c.offsetWidth;
  const h = 16;
  c.width = w * dpr; c.height = h * dpr;
  const ctx = c.getContext('2d');
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, w, h);
  // Aggregate: for each frame time, compute avg link speed from timelines
  const features = geoData.features || [];
  if (!features.length || !frameTimes.length) return;
  // Build per-time avg speed ratio（computeStatsSeries が計算済みの avgRatio を間引いて使う）
  const N = Math.min(120, frameTimes.length);
  const step = Math.max(1, Math.floor(frameTimes.length / N));
  if (!statsSeries || !statsSeries.avgRatio || statsSeries.avgRatio.length !== frameTimes.length) {
    computeStatsSeries();
  }
  const ratios = (statsSeries && statsSeries.avgRatio) || [];
  const pts = [];
  for (let i = 0; i < frameTimes.length; i += step) pts.push(ratios[i] || 0);
  if (!pts.length) return;
  // Draw as bars (congestion height = 1 - ratio)
  ctx.fillStyle = getComputedStyle(document.documentElement).getPropertyValue('--accent').trim() || '#dc4a1c';
  const bw = w / pts.length;
  for (let i = 0; i < pts.length; i++) {
    const congestion = 1 - pts[i]; // 0=free, 1=jam
    const bh = congestion * h;
    ctx.fillRect(i * bw, h - bh, Math.max(1, bw - .5), bh);
  }
}
window.addEventListener('resize', drawSparkline);

// ── ?replay=<name>: 記録済みのチャットターンを再現表示する（開発時のスクリーンショット用）．
//    /demo/replay.js があるときだけ動く．公開配布物には含まれないので通常は何も起きない．
//    添付ファイル名は ?file= で渡す（省略時は replay.js 側の既定値）．
(function () {
  const rp = new URLSearchParams(location.search).get('replay');
  if (!rp) return;
  const s = document.createElement('script'); s.src = '/demo/replay.js?ts=' + Date.now();
  const att = new URLSearchParams(location.search).get('file') || undefined;
  s.onload = () => { if (typeof replayTurn === 'function') replayTurn(rp, { attachment: att }); };
  document.head.appendChild(s);
})();

// ── 起動時: 履歴からの復元オプションは表示しない．手動のみ ──

