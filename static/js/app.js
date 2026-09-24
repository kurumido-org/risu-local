// RISU UI — アプリの状態（グローバル）・Canvas サイズ・カメラ．
// index.html の <script> から切り出したもの（内容・順序は同じ）．classic script として
// 同じグローバルスコープを共有するので，index.html の読み込み順を変えないこと．
// DOM に触らないロジックは risu-core.js（node でテスト）へ．
const API = '';
let chatHistory = [];
let geoData    = null;
// framesData: {str(t): {n, ids:Uint32, xs:Float32, ys:Float32, vs:Float32, alphas:Float32, li:Uint16}}
let framesData = null;
let frameTimes = [];     // sorted float array
let linkNames  = [];     // li インデックス → リンク名
let linkFfsByIdx = null; // Float32Array: li → ffs
let signalsData = [];    // [{node,x,y,phases[],groups:{link_name:phase_idx}}]
let scenarioData = null; // {nodes:[{name,x,y,signal,...}], links:[...], demands:[...]}
let nodeByCoord = {};    // "x,y" → node name (高速ルックアップ)
// 台数の単位換算．フレームの 1 点は UXsim のプラトン（deltan 台）で，さらに描画用に
// vehicle_sample_step 個に 1 個へ間引かれていることがある．
//  - vehicleCounts: サーバーが間引き前の全点から数えた実台数（frameTimes と同じ長さ）
//  - vehScale: 古い結果（vehicle_counts なし）用の近似係数 = deltan × vehicle_sample_step
let vehicleCounts = null;
let vehScale = 1;
// サーバーが実イベントから集計した系列（描画用フレームとは独立）
//  - tripSeries: {t[], entered[], completed[]} 流入・到着の累積台数．時間軸は 0〜tmax
//  - frameAvgSpeed: フレームごとの車両平均速度（台数重み，間引き前）．LLM と同じ定義
//  - simStats: サーバーの統計（total_trips 等）
let tripSeries = null;
let frameAvgSpeed = null;
let simStats = null;
let frameIntervalS = null;   // 連続フレームの間隔（秒）．空白時間・終了後の判定（RisuCore.frameWindow）
let simTmax    = 3600;
let lastSimId  = null;
// lastResult は廃止（メモリ節約）— ダウンロードはサーバーから直接取得

// 描画モード: "link" | "vehicle" | "trail"
let drawMode = 'vehicle';
const TRAIL_LENGTH = 8; // 何フレーム分の軌跡を表示するか

function setDrawMode(mode) {
  drawMode = mode;
  document.querySelectorAll('#mode-toggle button').forEach(b => {
    const active = b.dataset.mode === mode;
    b.classList.toggle('active', active);
    b.setAttribute('aria-pressed', active ? 'true' : 'false');
  });
  drawFrame();
}

// ── Canvas サイズ管理 ──
const canvasEl   = document.getElementById('net-canvas');
const canvasWrap = document.getElementById('canvas-wrap');
let canvasW = 0, canvasH = 0, canvasDirty = true;

function resizeCanvas() {
  // DPR は 1.5 でキャップ（Retina での過大なバッキングストアを回避）
  const dpr = Math.min(1.5, window.devicePixelRatio || 1);
  canvasW = canvasWrap.clientWidth;
  canvasH = canvasWrap.clientHeight;
  canvasEl.width  = Math.round(canvasW * dpr);
  canvasEl.height = Math.round(canvasH * dpr);
  canvasEl.style.width  = canvasW + 'px';
  canvasEl.style.height = canvasH + 'px';
  canvasDirty = false;
}
// ResizeObserver は登録直後（次の描画機会）に発火し，後続の JS ファイルの読み込み待ちの間に
// 呼ばれることがある．drawFrame（render.js）が揃う DOMContentLoaded 後に登録する．
document.addEventListener('DOMContentLoaded', () => {
  new ResizeObserver(() => { canvasDirty = true; drawFrame(); }).observe(canvasWrap);
});

// ── カメラ制御（パン・ズーム） ──
let camZoom = 1, camPanX = 0, camPanY = 0;
let isDragging = false, dragStartX = 0, dragStartY = 0, dragPanX = 0, dragPanY = 0;

function resetCamera() { camZoom = 1; camPanX = 0; camPanY = 0; }

canvasEl.addEventListener('wheel', (e) => {
  e.preventDefault();
  const rect = canvasEl.getBoundingClientRect();
  const mx = e.clientX - rect.left;
  const my = e.clientY - rect.top;
  // マウス位置を中心にズーム
  const oldZoom = camZoom;
  const factor = e.deltaY < 0 ? 1.12 : 1 / 1.12;
  camZoom = Math.max(0.2, Math.min(20, camZoom * factor));
  // ズーム時にマウス位置がずれないようパンを補正
  camPanX = mx - (mx - camPanX) * (camZoom / oldZoom);
  camPanY = my - (my - camPanY) * (camZoom / oldZoom);
  drawFrame();
}, { passive: false });

canvasEl.addEventListener('mousedown', (e) => {
  if (e.button !== 0) return;
  // 編集モード: エディタが消費したらパンしない（空き地 + 選択ツールはパンにフォールスルー）
  if (editMode && handleEditMouseDown(e)) return;
  isDragging = true;
  dragStartX = e.clientX;
  dragStartY = e.clientY;
  dragPanX = camPanX;
  dragPanY = camPanY;
  canvasEl.style.cursor = 'grabbing';
});

window.addEventListener('mousemove', (e) => {
  if (!isDragging) return;
  camPanX = dragPanX + (e.clientX - dragStartX);
  camPanY = dragPanY + (e.clientY - dragStartY);
  drawFrame();
});

window.addEventListener('mouseup', () => {
  if (isDragging) {
    isDragging = false;
    canvasEl.style.cursor = 'grab';
  }
});

// タッチ対応（ピンチズーム + パン）
let lastTouchDist = 0, lastTouchMid = null;
canvasEl.addEventListener('touchstart', (e) => {
  if (e.touches.length === 1) {
    isDragging = true;
    dragStartX = e.touches[0].clientX;
    dragStartY = e.touches[0].clientY;
    dragPanX = camPanX;
    dragPanY = camPanY;
  } else if (e.touches.length === 2) {
    isDragging = false;
    const dx = e.touches[1].clientX - e.touches[0].clientX;
    const dy = e.touches[1].clientY - e.touches[0].clientY;
    lastTouchDist = Math.hypot(dx, dy);
    lastTouchMid = { x: (e.touches[0].clientX + e.touches[1].clientX) / 2,
                     y: (e.touches[0].clientY + e.touches[1].clientY) / 2 };
  }
}, { passive: true });

canvasEl.addEventListener('touchmove', (e) => {
  e.preventDefault();
  if (e.touches.length === 1 && isDragging) {
    camPanX = dragPanX + (e.touches[0].clientX - dragStartX);
    camPanY = dragPanY + (e.touches[0].clientY - dragStartY);
    drawFrame();
  } else if (e.touches.length === 2 && lastTouchDist > 0) {
    const dx = e.touches[1].clientX - e.touches[0].clientX;
    const dy = e.touches[1].clientY - e.touches[0].clientY;
    const dist = Math.hypot(dx, dy);
    const rect = canvasEl.getBoundingClientRect();
    const mx = (e.touches[0].clientX + e.touches[1].clientX) / 2 - rect.left;
    const my = (e.touches[0].clientY + e.touches[1].clientY) / 2 - rect.top;
    const oldZoom = camZoom;
    camZoom = Math.max(0.2, Math.min(20, camZoom * (dist / lastTouchDist)));
    camPanX = mx - (mx - camPanX) * (camZoom / oldZoom);
    camPanY = my - (my - camPanY) * (camZoom / oldZoom);
    lastTouchDist = dist;
    drawFrame();
  }
}, { passive: false });

canvasEl.addEventListener('touchend', () => { isDragging = false; lastTouchDist = 0; });

// ダブルクリックでリセット
canvasEl.addEventListener('dblclick', () => { resetCamera(); drawFrame(); });

canvasEl.style.cursor = 'grab';

