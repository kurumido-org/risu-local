// RISU UI — Toast・UI 拡張（履歴・ズーム・ショートカット・スプリッタ）．
// index.html の <script> から切り出したもの（内容・順序は同じ）．classic script として
// 同じグローバルスコープを共有するので，index.html の読み込み順を変えないこと．
// DOM に触らないロジックは risu-core.js（node でテスト）へ．
// ══════════════════════════════════════════════════════════════════
// Toast 通知
// ══════════════════════════════════════════════════════════════════
(function () {
  let _toastTimer = null;
  window.showToast = function (msg, kind = "", ms = 3000) {
    const t = document.getElementById('toast');
    if (!t) return;
    t.textContent = msg;
    t.className = 'visible' + (kind ? ' ' + kind : '');
    clearTimeout(_toastTimer);
    _toastTimer = setTimeout(() => { t.classList.remove('visible'); }, ms);
  };
})();

// ══════════════════════════════════════════════════════════════════
// UI 拡張機能
// ══════════════════════════════════════════════════════════════════

// ── Empty-state 管理 ──
const emptyState = document.getElementById('empty-state');
function hideEmptyState() { if (emptyState) emptyState.style.display = 'none'; }
function showEmptyState() { if (emptyState && !document.querySelectorAll('#messages .msg').length) emptyState.style.display = ''; }

// addMsg をラップして empty-state を自動で隠す
const _origAddMsg = addMsg;
addMsg = function(role, text, thinking = false) {
  hideEmptyState();
  return _origAddMsg(role, text, thinking);
};

// ── New Chat / Clear history ──
function newChat() {
  if (chatHistory.length > 0) {
    saveHistoryEntry();
  }
  // Chart.js インスタンスを確実に破棄（メモリリーク防止）
  if (window._chartInstances) {
    for (const c of window._chartInstances) {
      try { c.destroy(); } catch {}
    }
    window._chartInstances = [];
  }
  chatHistory = [];
  document.getElementById('messages').innerHTML = '';
  document.getElementById('messages').appendChild(emptyState);
  emptyState.style.display = '';
  // reset viz
  releasePrevResult();
  lastSimId = null;
  document.getElementById('stats-panel').hidden = true;
  document.getElementById('canvas-controls').hidden = true;
  document.getElementById('dl-btn').style.display = 'none';
  document.getElementById('dl-scn-btn').style.display = 'none';
  document.getElementById('time-control').classList.remove('visible');
  document.getElementById('legend').classList.remove('visible');
  document.getElementById('canvas-placeholder').style.display = '';
  document.getElementById('sim-id-label').textContent = '—';
  document.getElementById('link-count-label').textContent = '';
  resetCamera(); drawFrame();
  if (typeof stopPlay === 'function' && playing) stopPlay();
}
document.getElementById('new-chat-btn').addEventListener('click', () => {
  if (chatHistory.length > 0 && !confirm('現在の会話をリセットしますか？')) return;
  newChat();
});

// ── Cancel (AbortController) ──
let currentAbortController = null;
function cancelSend() {
  if (currentAbortController) currentAbortController.abort();
}

// sendMessage を拡張してキャンセル対応
const _origSendMessage = sendMessage;
sendMessage = async function() {
  currentAbortController = new AbortController();
  const origFetch = window.fetch;
  window.fetch = (url, opts = {}) => origFetch(url, { ...opts, signal: currentAbortController.signal });
  try {
    await _origSendMessage();
    saveHistoryEntry();
  } finally {
    window.fetch = origFetch;
    currentAbortController = null;
  }
};

// ── sim-id クリックでコピー ──
document.getElementById('sim-id-label').addEventListener('click', () => {
  if (!lastSimId) return;
  navigator.clipboard.writeText(lastSimId).then(() => {
    const el = document.getElementById('sim-id-label');
    el.classList.add('copied');
    const orig = el.textContent;
    el.textContent = 'COPIED';
    setTimeout(() => { el.classList.remove('copied'); el.textContent = orig; }, 1200);
    if (window.showToast) showToast(`sim_id をコピーしました: ${lastSimId}`);
  }).catch(() => {
    if (window.showToast) showToast('コピーに失敗しました', 'error');
  });
});

// ── Canvas ズームコントロール ──
function zoomCanvas(factor) {
  const rect = canvasEl.getBoundingClientRect();
  const mx = rect.width / 2, my = rect.height / 2;
  const oldZoom = camZoom;
  camZoom = Math.max(0.2, Math.min(20, camZoom * factor));
  camPanX = mx - (mx - camPanX) * (camZoom / oldZoom);
  camPanY = my - (my - camPanY) * (camZoom / oldZoom);
  updateZoomLabel();
  drawFrame();
}
function updateZoomLabel() {
  const el = document.getElementById('cc-zoom');
  if (el) el.textContent = Math.round(camZoom * 100) + '%';
}
// ホイール/ドラッグ後にもラベル更新
(function wrapDrawFrame() {
  const _orig = drawFrame;
  window.drawFrame = function() { updateZoomLabel(); return _orig.apply(this, arguments); };
})();

// ── Canvas hint (初回表示, 3 秒で消える) ──
(function showCanvasHint() {
  const h = document.getElementById('canvas-hint');
  canvasEl.addEventListener('mouseenter', () => {
    if (!geoData) return;
    if (sessionStorage.getItem('canvas-hint-dismissed')) return;
    h.hidden = false;
    h.classList.add('visible');
    setTimeout(() => { h.classList.remove('visible'); setTimeout(() => h.hidden = true, 400); sessionStorage.setItem('canvas-hint-dismissed', '1'); }, 3000);
  }, { once: true });
})();

// ── Help / History モーダル ──
function toggleHelp() {
  const m = document.getElementById('help-modal');
  m.classList.toggle('visible');
}
function toggleHistory() {
  const d = document.getElementById('history-drawer');
  d.classList.toggle('visible');
  if (d.classList.contains('visible')) renderHistoryList();
}
document.getElementById('help-btn').addEventListener('click', toggleHelp);
document.getElementById('history-btn').addEventListener('click', toggleHistory);

// ── キーボードショートカット ──
document.addEventListener('keydown', e => {
  // Esc = close modal / drawer
  if (e.key === 'Escape') {
    document.getElementById('help-modal').classList.remove('visible');
    document.getElementById('history-drawer').classList.remove('visible');
  }
  // Ctrl+K = new chat
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
    e.preventDefault();
    if (chatHistory.length === 0 || confirm('現在の会話をリセットしますか？')) newChat();
  }
  // Space = play/pause (フォーカスが textarea 以外)
  if (e.code === 'Space' && document.activeElement.tagName !== 'TEXTAREA' && document.activeElement.tagName !== 'INPUT') {
    if (frameTimes.length) { e.preventDefault(); document.getElementById('play-btn').click(); }
  }
});

// ── LocalStorage 履歴 ──
const HISTORY_KEY = 'risu_history_v1';
function loadHistoryIndex() {
  try { return JSON.parse(localStorage.getItem(HISTORY_KEY) || '[]'); }
  catch { return []; }
}
function saveHistoryEntry() {
  if (chatHistory.length === 0) return;
  const idx = loadHistoryIndex();
  const firstUser = chatHistory.find(m => m.role === 'user');
  const title = firstUser ? firstUser.content.slice(0, 80) : 'untitled';
  const id = 'h_' + Date.now();
  // Avoid dup of very recent save
  if (idx.length && idx[0].title === title && Date.now() - idx[0].ts < 5000) return;
  idx.unshift({ id, title, ts: Date.now(), history: chatHistory, simId: lastSimId });
  const trimmed = idx.slice(0, 30);
  try { localStorage.setItem(HISTORY_KEY, JSON.stringify(trimmed)); } catch {}
}
function renderHistoryList() {
  const list = document.getElementById('history-list');
  const items = loadHistoryIndex();
  if (!items.length) {
    list.innerHTML = '<div class="hist-empty">履歴はまだありません</div>';
    return;
  }
  list.innerHTML = '';
  for (const it of items) {
    const el = document.createElement('div');
    el.className = 'hist-item';
    const d = new Date(it.ts);
    const dateStr = `${d.getMonth()+1}/${d.getDate()} ${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}`;
    el.innerHTML = `<div class="t">${dateStr}${it.simId ? ' · ' + it.simId : ''}</div><div class="q"></div>`;
    el.querySelector('.q').textContent = it.title;
    el.addEventListener('click', () => restoreHistory(it));
    list.appendChild(el);
  }
}
function restoreHistory(entry) {
  chatHistory = [...entry.history];
  document.getElementById('messages').innerHTML = '';
  document.getElementById('messages').appendChild(emptyState);
  emptyState.style.display = 'none';
  for (const m of chatHistory) {
    if (m.role === 'user' || m.role === 'assistant') addMsg(m.role, m.content);
  }
  if (entry.simId) loadResult(entry.simId);
  toggleHistory();
}

// ── Splitter ドラッグ ──
(function enableSplitter() {
  const sp = document.getElementById('splitter');
  let dragging = false;
  sp.addEventListener('mousedown', (e) => {
    dragging = true;
    document.body.classList.add('resizing');
    sp.classList.add('dragging');
    e.preventDefault();
  });
  window.addEventListener('mousemove', (e) => {
    if (!dragging) return;
    const w = window.innerWidth;
    const min = 280, max = w - 320;
    const x = Math.max(min, Math.min(max, e.clientX));
    document.body.style.setProperty('--split', x + 'px');
    canvasDirty = true; drawFrame();
  });
  window.addEventListener('mouseup', () => {
    if (!dragging) return;
    dragging = false;
    document.body.classList.remove('resizing');
    sp.classList.remove('dragging');
  });
})();

// ── モバイルタブ切替 ──
function switchPane(paneId) {
  document.querySelectorAll('#mobile-tabs .mt-btn').forEach(b => {
    b.classList.toggle('active', b.dataset.pane === paneId);
  });
  document.getElementById('chat-col').classList.toggle('hidden-pane', paneId !== 'chat-col');
  document.getElementById('viz-col').classList.toggle('hidden-pane', paneId !== 'viz-col');
  canvasDirty = true; drawFrame();
}

// ── チャートにダウンロード/拡大ボタンを追加 ──
const _origRenderChart = renderChart;
renderChart = function(msgEl, chartSpec) {
  _origRenderChart(msgEl, chartSpec);
  const wraps = msgEl.querySelectorAll('.chart-wrap');
  const wrap = wraps[wraps.length - 1];
  if (!wrap || wrap.querySelector('.chart-actions')) return;
  const actions = document.createElement('div');
  actions.className = 'chart-actions';
  const btnDl = document.createElement('button');
  btnDl.textContent = 'PNG';
  btnDl.title = 'PNG をダウンロード';
  btnDl.onclick = () => {
    const canvas = wrap.querySelector('canvas');
    const link = document.createElement('a');
    link.download = 'risu_chart.png';
    link.href = canvas.toDataURL('image/png');
    link.click();
  };
  const btnExpand = document.createElement('button');
  btnExpand.textContent = 'OPEN';
  btnExpand.title = '新しいタブで開く';
  btnExpand.onclick = () => {
    const canvas = wrap.querySelector('canvas');
    const w = window.open();
    w.document.write('<img src="' + canvas.toDataURL('image/png') + '" style="max-width:100%">');
  };
  actions.appendChild(btnDl);
  actions.appendChild(btnExpand);
  wrap.appendChild(actions);
};

// ── loadResult ラップ: stats-panel / canvas-controls を表示 ──
const _origLoadResult = loadResult;
loadResult = async function(simId) {
  await _origLoadResult(simId);
  document.getElementById('stats-panel').hidden = false;
  document.getElementById('canvas-controls').hidden = false;
  updateZoomLabel();
  computeStatsSeries();
  drawSparkline();
  drawAllStatCharts();
  updateStatValues();
  updateTimeLabel();
};

