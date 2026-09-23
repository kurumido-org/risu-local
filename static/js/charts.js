// RISU UI — チャット内のチャート（Chart.js）．
// index.html の <script> から切り出したもの（内容・順序は同じ）．classic script として
// 同じグローバルスコープを共有するので，index.html の読み込み順を変えないこと．
// DOM に触らないロジックは risu-core.js（node でテスト）へ．
// ── インラインチャート描画 ──
function renderChart(msgEl, chartSpec) {
  const body = msgEl.querySelector('.body');
  if (!body) { console.warn('[RISU] renderChart: .body not found'); return; }
  if (!chartSpec) { console.warn('[RISU] renderChart: chartSpec is empty'); return; }

  // Chart.js が読み込まれているか確認
  if (typeof Chart === 'undefined') {
    console.error('[RISU] renderChart: Chart.js が読み込まれていません（CDN/ネットワーク/CSP を確認）');
    const err = document.createElement('div');
    err.className = 'chart-wrap';
    err.textContent = 'グラフ描画ライブラリが読み込まれていません';
    body.appendChild(err);
    return;
  }

  // LLM が data ラッパーを忘れて {type, labels, datasets} と書くことがあるので救済
  if (!chartSpec.data && (chartSpec.labels || chartSpec.datasets)) {
    chartSpec = { ...chartSpec, data: { labels: chartSpec.labels, datasets: chartSpec.datasets } };
  }
  if (!chartSpec.data || !chartSpec.data.datasets) {
    console.warn('[RISU] renderChart: chartSpec.data.datasets が無い', chartSpec);
    return;
  }

  const wrap = document.createElement('div');
  wrap.className = 'chart-wrap';

  // タイトル（chartSpec.options.plugins.title.text または chartSpec.title）
  const titleText = chartSpec.title
    || (chartSpec.options && chartSpec.options.plugins && chartSpec.options.plugins.title && chartSpec.options.plugins.title.text)
    || '';
  if (titleText) {
    const title = document.createElement('div');
    title.className = 'chart-title';
    title.textContent = titleText;
    wrap.appendChild(title);
  }

  // canvas は専用の高さ固定ボックスに入れる（responsive 描画の高さ 0 問題を回避）
  const canvasBox = document.createElement('div');
  canvasBox.className = 'chart-canvas-box';
  const canvas = document.createElement('canvas');
  canvasBox.appendChild(canvas);
  wrap.appendChild(canvasBox);
  body.appendChild(wrap);

  // デフォルトオプション（LLM のオプションとマージ）
  const defaultOpts = {
    responsive: true,
    maintainAspectRatio: false,
    plugins: {
      legend: {
        display: chartSpec.data.datasets && chartSpec.data.datasets.length > 1,
        position: 'top',
        labels: { font: { size: 10, family: "'IBM Plex Mono', monospace" } },
      },
      title: { display: false },
    },
    scales: {
      x: { ticks: { font: { size: 9 }, maxTicksLimit: 12 } },
      y: { ticks: { font: { size: 9 } }, beginAtZero: true },
    },
  };

  // LLM が指定した options をマージ
  const userOpts = chartSpec.options || {};
  const mergedOpts = deepMerge(defaultOpts, userOpts);

  let chartInstance;
  try {
    chartInstance = new Chart(canvas.getContext('2d'), {
      type: chartSpec.type || 'line',
      data: chartSpec.data,
      options: mergedOpts,
    });
  } catch (err) {
    console.error('[RISU] renderChart: Chart.js 初期化エラー', err, chartSpec);
    canvasBox.remove();
    const errEl = document.createElement('div');
    errEl.style.fontSize = '11px';
    errEl.style.color = 'var(--accent)';
    errEl.textContent = 'グラフ描画エラー: ' + (err && err.message ? err.message : String(err));
    wrap.appendChild(errEl);
    return;
  }
  // 後で newChat() でまとめて破棄できるよう参照を保持
  if (!window._chartInstances) window._chartInstances = [];
  window._chartInstances.push(chartInstance);

  const messages = document.getElementById('messages');
  messages.scrollTop = messages.scrollHeight;
}

// 深いマージ（LLM のオプションでデフォルトを上書き）
function deepMerge(target, source) {
  const out = { ...target };
  for (const key of Object.keys(source)) {
    if (source[key] && typeof source[key] === 'object' && !Array.isArray(source[key])
        && target[key] && typeof target[key] === 'object') {
      out[key] = deepMerge(target[key], source[key]);
    } else {
      out[key] = source[key];
    }
  }
  return out;
}

// ── ユーティリティ ──
function setExample(el) {
  document.getElementById('user-input').value = el.textContent;
  document.getElementById('user-input').focus();
}
document.getElementById('user-input').addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
});
document.getElementById('user-input').addEventListener('input', function() {
  this.style.height = '';
  this.style.height = Math.min(this.scrollHeight, 120) + 'px';
});

