// RISU UI — GUI ネットワークエディタ．
// index.html の <script> から切り出したもの（内容・順序は同じ）．classic script として
// 同じグローバルスコープを共有するので，index.html の読み込み順を変えないこと．
// DOM に触らないロジックは risu-core.js（node でテスト）へ．
// ══════════════════════════════════════════════════════════════════
// ネットワークエディタ
// ══════════════════════════════════════════════════════════════════
let editMode = false;
let editTool = 'select';
let editNet = null;          // {nodes:[], links:[], demands:[]}
let editSel = null;          // {type:'node'|'link', idx}
let editLinkStart = null;    // リンク作成の始点ノード idx
let editDragIdx = null;      // ドラッグ中ノード idx
let editMouse = null;        // ラバーバンド用スクリーン座標
let editView = null;         // 固定ワールド bbox（編集中に視点が飛ばないよう固定）

const _etToolbar = document.getElementById('edit-toolbar');
const _etProps   = document.getElementById('edit-props');
const _etDemands = document.getElementById('edit-demands');
const _etHint    = document.getElementById('edit-hint');

function toggleEditMode() {
  if (editMode) { exitEditMode(); return; }
  editMode = true;
  stopPlay();
  // 現在のシナリオを複製して編集対象に（なければ空から）
  const src = scenarioData;
  editNet = {
    nodes:   src && src.nodes   ? JSON.parse(JSON.stringify(src.nodes))   : [],
    links:   src && src.links   ? JSON.parse(JSON.stringify(src.links))   : [],
    demands: src && src.demands ? JSON.parse(JSON.stringify(src.demands)) : [],
  };
  document.getElementById('et-tmax').value = (src && src.tmax) || 3600;
  // 元シナリオの全体パラメータを引き継ぐ（落とすと UXsim 既定に戻り，容量など比較条件が変わる）
  document.getElementById('et-rt').value   = (src && src.reaction_time != null) ? src.reaction_time : '';
  document.getElementById('et-seed').value = (src && src.random_seed != null) ? src.random_seed : '';
  editSel = null; editLinkStart = null; editTool = 'select';
  _etToolbar.querySelectorAll('.et-tool').forEach(b => b.classList.toggle('active', b.dataset.tool === 'select'));
  // ワールド bbox を固定（ノードがなければ 0..2000）
  editView = computeEditBBox();
  resetCamera();
  document.getElementById('edit-btn').classList.add('active');
  document.getElementById('canvas-placeholder').style.display = 'none';
  document.getElementById('canvas-controls').hidden = false;
  _etToolbar.hidden = false;
  setEditHint('ノード: 空き地をクリック / リンク: ノードを順にクリック / ドラッグでパン・移動');
  drawEditFrame();
}

function exitEditMode() {
  editMode = false;
  editSel = null; editLinkStart = null; editDragIdx = null;
  document.getElementById('edit-btn').classList.remove('active');
  _etToolbar.hidden = true; _etProps.hidden = true; _etDemands.hidden = true; _etHint.hidden = true;
  if (!geoData) {
    document.getElementById('canvas-placeholder').style.display = '';
    const ctx = canvasEl.getContext('2d');
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, canvasEl.width, canvasEl.height);
  } else {
    drawFrame();
  }
}

function setEditHint(msg) {
  if (!msg) { _etHint.hidden = true; return; }
  _etHint.textContent = msg;
  _etHint.hidden = false;
}

function computeEditBBox() {
  if (!editNet.nodes.length) return { minX: 0, maxX: 2000, minY: 0, maxY: 2000 };
  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
  for (const n of editNet.nodes) {
    if (n.x < minX) minX = n.x; if (n.x > maxX) maxX = n.x;
    if (n.y < minY) minY = n.y; if (n.y > maxY) maxY = n.y;
  }
  if (minX === maxX) { minX -= 500; maxX += 500; }
  if (minY === maxY) { minY -= 500; maxY += 500; }
  return { minX, maxX, minY, maxY };
}

// drawFrame と同じ座標系（カメラ込み）+ 逆変換
function editTransform() {
  let { minX, maxX, minY, maxY } = editView;
  const marginX = (maxX - minX) * 0.1 || 100;
  const marginY = (maxY - minY) * 0.1 || 100;
  minX -= marginX; maxX += marginX; minY -= marginY; maxY += marginY;
  const pad = 60;
  const rangeX = maxX - minX || 1, rangeY = maxY - minY || 1;
  const baseScale = Math.min((canvasW - pad*2) / rangeX, (canvasH - pad*2) / rangeY);
  const baseOffX = pad + (canvasW - pad*2 - rangeX * baseScale) / 2;
  const baseOffY = pad + (canvasH - pad*2 - rangeY * baseScale) / 2;
  const scaleV = baseScale * camZoom;
  const offX = baseOffX * camZoom + camPanX;
  const topY = (canvasH - baseOffY) * camZoom + camPanY;
  return {
    px: x => offX + (x - minX) * scaleV,
    py: y => topY - (y - minY) * scaleV,
    invX: sx => (sx - offX) / scaleV + minX,
    invY: sy => (topY - sy) / scaleV + minY,
    scaleV,
  };
}

function drawEditFrame() {
  if (canvasDirty) resizeCanvas();
  const dpr = Math.min(1.5, window.devicePixelRatio || 1);
  const ctx = canvasEl.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, canvasW, canvasH);
  const T = editTransform();
  const { accent, soft, ink } = _getThemeColors();

  // 逆方向リンク存在チェック（オフセット描画用）
  const pairSet = new Set(editNet.links.map(l => `${l.start}->${l.end}`));
  const nodeByName = {};
  for (const n of editNet.nodes) nodeByName[n.name] = n;

  // ── リンク ──
  editNet.links.forEach((lk, i) => {
    const a = nodeByName[lk.start], b = nodeByName[lk.end];
    if (!a || !b) return;
    let ax = T.px(a.x), ay = T.py(a.y), bx = T.px(b.x), by = T.py(b.y);
    // 双方向ペアは進行方向右側に 4px オフセット
    if (pairSet.has(`${lk.end}->${lk.start}`)) {
      const dx = bx - ax, dy = by - ay, len = Math.hypot(dx, dy) || 1;
      const nx = -dy / len * 4, ny = dx / len * 4;
      ax += nx; ay += ny; bx += nx; by += ny;
    }
    const selected = editSel && editSel.type === 'link' && editSel.idx === i;
    ctx.strokeStyle = selected ? accent : '#9aa3b2';
    ctx.lineWidth = selected ? 4 : 2.5;
    ctx.lineCap = 'round';
    ctx.beginPath(); ctx.moveTo(ax, ay); ctx.lineTo(bx, by); ctx.stroke();
    // 矢印（60% 位置）
    const t = 0.6, mx = ax + (bx - ax) * t, my = ay + (by - ay) * t;
    const ang = Math.atan2(by - ay, bx - ax);
    ctx.fillStyle = selected ? accent : '#7a8494';
    ctx.beginPath();
    ctx.moveTo(mx + Math.cos(ang) * 6, my + Math.sin(ang) * 6);
    ctx.lineTo(mx + Math.cos(ang + 2.5) * 5, my + Math.sin(ang + 2.5) * 5);
    ctx.lineTo(mx + Math.cos(ang - 2.5) * 5, my + Math.sin(ang - 2.5) * 5);
    ctx.closePath(); ctx.fill();
  });

  // ── リンク作成のラバーバンド ──
  if (editTool === 'addLink' && editLinkStart != null && editMouse) {
    const s = editNet.nodes[editLinkStart];
    if (s) {
      ctx.strokeStyle = accent;
      ctx.lineWidth = 1.5;
      ctx.setLineDash([5, 4]);
      ctx.beginPath(); ctx.moveTo(T.px(s.x), T.py(s.y)); ctx.lineTo(editMouse.x, editMouse.y); ctx.stroke();
      ctx.setLineDash([]);
    }
  }

  // ── ノード ──
  editNet.nodes.forEach((n, i) => {
    const x = T.px(n.x), y = T.py(n.y);
    const selected = (editSel && editSel.type === 'node' && editSel.idx === i) || editLinkStart === i;
    ctx.fillStyle = selected ? accent : '#4a5568';
    ctx.beginPath(); ctx.arc(x, y, 7, 0, Math.PI * 2); ctx.fill();
    ctx.strokeStyle = '#fff'; ctx.lineWidth = 1.5; ctx.stroke();
    // 信号ノードはリング表示
    if (n.signal && n.signal.length && n.signal.reduce((a, b) => a + b, 0) > 0) {
      ctx.strokeStyle = '#e8a31c'; ctx.lineWidth = 2;
      ctx.beginPath(); ctx.arc(x, y, 11, 0, Math.PI * 2); ctx.stroke();
    }
    ctx.fillStyle = 'rgba(100,110,130,0.85)';
    ctx.font = '10px "IBM Plex Mono"';
    ctx.textAlign = 'center';
    ctx.fillText(n.name, x, y - 12);
  });
}

// ── ヒットテスト ──
function editHitNode(mx, my) {
  const T = editTransform();
  for (let i = editNet.nodes.length - 1; i >= 0; i--) {
    const n = editNet.nodes[i];
    if (Math.hypot(T.px(n.x) - mx, T.py(n.y) - my) <= 10) return i;
  }
  return null;
}
function editHitLink(mx, my) {
  const T = editTransform();
  const nodeByName = {};
  for (const n of editNet.nodes) nodeByName[n.name] = n;
  for (let i = editNet.links.length - 1; i >= 0; i--) {
    const lk = editNet.links[i];
    const a = nodeByName[lk.start], b = nodeByName[lk.end];
    if (!a || !b) continue;
    const ax = T.px(a.x), ay = T.py(a.y), bx = T.px(b.x), by = T.py(b.y);
    const L2 = (bx - ax) ** 2 + (by - ay) ** 2;
    if (L2 === 0) continue;
    let t = ((mx - ax) * (bx - ax) + (my - ay) * (by - ay)) / L2;
    t = Math.max(0.05, Math.min(0.95, t));  // 端点付近はノード優先
    const d = Math.hypot(ax + t * (bx - ax) - mx, ay + t * (by - ay) - my);
    if (d <= 6) return i;
  }
  return null;
}

// ── 名前生成 ──
function editUniqueName(prefix, list) {
  const used = new Set(list.map(o => o.name));
  let i = 1;
  while (used.has(`${prefix}${i}`)) i++;
  return `${prefix}${i}`;
}

// ── マウス操作（true を返すと消費 = パンしない） ──
function handleEditMouseDown(e) {
  const rect = canvasEl.getBoundingClientRect();
  const mx = e.clientX - rect.left, my = e.clientY - rect.top;
  const T = editTransform();
  const nodeIdx = editHitNode(mx, my);

  if (editTool === 'addNode') {
    if (nodeIdx != null) { editSelect('node', nodeIdx); return true; }
    const name = editUniqueName('N', editNet.nodes);
    editNet.nodes.push({ name, x: Math.round(T.invX(mx)), y: Math.round(T.invY(my)) });
    editSelect('node', editNet.nodes.length - 1);
    drawEditFrame();
    return true;
  }

  if (editTool === 'addLink') {
    if (nodeIdx == null) { editLinkStart = null; drawEditFrame(); return true; }
    if (editLinkStart == null) {
      editLinkStart = nodeIdx;
      setEditHint(`始点: ${editNet.nodes[nodeIdx].name} — 終点ノードをクリック（Esc で解除）`);
    } else if (editLinkStart !== nodeIdx) {
      const a = editNet.nodes[editLinkStart], b = editNet.nodes[nodeIdx];
      const length = Math.max(1, Math.round(Math.hypot(b.x - a.x, b.y - a.y)));
      const exists = (s, t) => editNet.links.some(l => l.start === s && l.end === t);
      if (!exists(a.name, b.name)) {
        editNet.links.push({ name: editUniqueName('L', editNet.links), start: a.name, end: b.name,
                             length, free_flow_speed: 20, number_of_lanes: 1 });
      }
      if (document.getElementById('et-bidir').checked && !exists(b.name, a.name)) {
        editNet.links.push({ name: editUniqueName('L', editNet.links), start: b.name, end: a.name,
                             length, free_flow_speed: 20, number_of_lanes: 1 });
      }
      editLinkStart = nodeIdx;  // 連続作成（回廊を素早く引ける）
      setEditHint(`リンク作成: ${a.name} → ${b.name} ✓ 続けて終点をクリック（Esc で終了）`);
    }
    drawEditFrame();
    return true;
  }

  if (editTool === 'delete') {
    if (nodeIdx != null) {
      const name = editNet.nodes[nodeIdx].name;
      editNet.nodes.splice(nodeIdx, 1);
      editNet.links = editNet.links.filter(l => l.start !== name && l.end !== name);
      editNet.demands = editNet.demands.filter(d => d.orig !== name && d.dest !== name);
      editSel = null; _etProps.hidden = true;
      drawEditFrame(); renderEditDemands(false);
      return true;
    }
    const linkIdx = editHitLink(mx, my);
    if (linkIdx != null) {
      editNet.links.splice(linkIdx, 1);
      editSel = null; _etProps.hidden = true;
      drawEditFrame();
      return true;
    }
    return false;  // 空き地 → パン
  }

  // select ツール
  if (nodeIdx != null) {
    editSelect('node', nodeIdx);
    editDragIdx = nodeIdx;   // ドラッグで移動
    drawEditFrame();
    return true;
  }
  const linkIdx = editHitLink(mx, my);
  if (linkIdx != null) { editSelect('link', linkIdx); drawEditFrame(); return true; }
  editSel = null; _etProps.hidden = true; drawEditFrame();
  return false;  // 空き地 → パンにフォールスルー
}

window.addEventListener('mousemove', (e) => {
  if (!editMode) return;
  const rect = canvasEl.getBoundingClientRect();
  editMouse = { x: e.clientX - rect.left, y: e.clientY - rect.top };
  if (editDragIdx != null) {
    const T = editTransform();
    const n = editNet.nodes[editDragIdx];
    n.x = Math.round(T.invX(editMouse.x));
    n.y = Math.round(T.invY(editMouse.y));
    drawEditFrame();
    if (editSel && editSel.type === 'node' && editSel.idx === editDragIdx) renderEditProps(false);
  } else if (editTool === 'addLink' && editLinkStart != null) {
    drawEditFrame();
  }
});

window.addEventListener('mouseup', () => {
  if (editDragIdx != null) {
    // 移動したノードに接続するリンクの長さを座標から再計算
    const n = editNet.nodes[editDragIdx];
    const nodeByName = {};
    for (const nd of editNet.nodes) nodeByName[nd.name] = nd;
    for (const lk of editNet.links) {
      if (lk.start === n.name || lk.end === n.name) {
        const a = nodeByName[lk.start], b = nodeByName[lk.end];
        if (a && b) lk.length = Math.max(1, Math.round(Math.hypot(b.x - a.x, b.y - a.y)));
      }
    }
    editDragIdx = null;
    if (editSel && editSel.type === 'node') renderEditProps(false);
    drawEditFrame();
  }
});

document.addEventListener('keydown', (e) => {
  if (!editMode) return;
  const tag = (document.activeElement || {}).tagName;
  if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
  if (e.key === 'Escape') {
    editLinkStart = null; editSel = null; _etProps.hidden = true;
    setEditHint('');
    drawEditFrame();
  } else if (e.key === 'Delete' || e.key === 'Backspace') {
    if (!editSel) return;
    if (editSel.type === 'node') {
      const name = editNet.nodes[editSel.idx].name;
      editNet.nodes.splice(editSel.idx, 1);
      editNet.links = editNet.links.filter(l => l.start !== name && l.end !== name);
      editNet.demands = editNet.demands.filter(d => d.orig !== name && d.dest !== name);
    } else {
      editNet.links.splice(editSel.idx, 1);
    }
    editSel = null; _etProps.hidden = true;
    drawEditFrame(); renderEditDemands(false);
  }
});

// ── 選択 & プロパティパネル ──
function editSelect(type, idx) {
  editSel = { type, idx };
  renderEditProps(true);
}

function renderEditProps(show) {
  if (!editSel) { _etProps.hidden = true; return; }
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  let html = '';
  if (editSel.type === 'node') {
    const n = editNet.nodes[editSel.idx];
    if (!n) { _etProps.hidden = true; return; }
    html = `<div class="ep-title">NODE<button class="ep-close" onclick="editSel=null;document.getElementById('edit-props').hidden=true;drawEditFrame()">×</button></div>`
      + `<div class="ep-row"><label>名前</label><input data-ep="name" value="${esc(n.name)}"></div>`
      + `<div class="ep-row"><label>x</label><input data-ep="x" type="number" value="${n.x}"></div>`
      + `<div class="ep-row"><label>y</label><input data-ep="y" type="number" value="${n.y}"></div>`
      + `<div class="ep-row"><label>流出容量 台/s</label><input data-ep="flow_capacity" type="number" step="0.1" placeholder="無制限" value="${n.flow_capacity ?? ''}"></div>`
      + `<div class="ep-row"><label>信号 青秒,青秒</label><input data-ep="signal" placeholder="例: 60,60（空=なし）" value="${n.signal ? esc(n.signal.join(',')) : ''}"></div>`;
  } else {
    const lk = editNet.links[editSel.idx];
    if (!lk) { _etProps.hidden = true; return; }
    html = `<div class="ep-title">LINK ${esc(lk.start)} → ${esc(lk.end)}<button class="ep-close" onclick="editSel=null;document.getElementById('edit-props').hidden=true;drawEditFrame()">×</button></div>`
      + `<div class="ep-row"><label>名前</label><input data-ep="name" value="${esc(lk.name)}"></div>`
      + `<div class="ep-row"><label>延長 m</label><input data-ep="length" type="number" value="${lk.length}"></div>`
      + `<div class="ep-row"><label>自由流速度 m/s</label><input data-ep="free_flow_speed" type="number" step="0.5" value="${lk.free_flow_speed ?? 20}"></div>`
      + `<div class="ep-row"><label>車線数</label><input data-ep="number_of_lanes" type="number" min="1" value="${lk.number_of_lanes ?? 1}"></div>`
      + `<div class="ep-row"><label>容量 台/s</label><input data-ep="capacity" type="number" step="0.05" min="0" placeholder="自動（FD 由来）" value="${lk.capacity ?? ''}"></div>`
      + `<div class="ep-row"><label>信号 group</label><input data-ep="signal_group" type="number" min="0" placeholder="なし" value="${lk.signal_group ?? ''}"></div>`;
  }
  _etProps.innerHTML = html;
  _etProps.querySelectorAll('input[data-ep]').forEach(inp => {
    inp.addEventListener('change', () => applyEditProp(inp.dataset.ep, inp.value));
  });
  if (show) _etProps.hidden = false;
}

function applyEditProp(field, value) {
  if (!editSel) return;
  if (editSel.type === 'node') {
    const n = editNet.nodes[editSel.idx];
    if (field === 'name') {
      const newName = value.trim();
      if (!newName || editNet.nodes.some((o, i) => o.name === newName && i !== editSel.idx)) {
        showToast('その名前は使えません（空 or 重複）', 'warn'); renderEditProps(false); return;
      }
      // リンク・需要へ伝播
      for (const lk of editNet.links) {
        if (lk.start === n.name) lk.start = newName;
        if (lk.end === n.name) lk.end = newName;
      }
      for (const d of editNet.demands) {
        if (d.orig === n.name) d.orig = newName;
        if (d.dest === n.name) d.dest = newName;
      }
      n.name = newName;
    } else if (field === 'x' || field === 'y') {
      n[field] = parseFloat(value) || 0;
    } else if (field === 'flow_capacity') {
      const v = parseFloat(value);
      if (isNaN(v) || value === '') delete n.flow_capacity; else n.flow_capacity = v;
    } else if (field === 'signal') {
      const parts = value.split(/[,，\s]+/).map(s => parseFloat(s)).filter(v => !isNaN(v) && v > 0);
      if (parts.length) n.signal = parts; else delete n.signal;
    }
  } else {
    const lk = editNet.links[editSel.idx];
    if (field === 'name') {
      const newName = value.trim();
      if (!newName || editNet.links.some((o, i) => o.name === newName && i !== editSel.idx)) {
        showToast('その名前は使えません（空 or 重複）', 'warn'); renderEditProps(false); return;
      }
      lk.name = newName;
    } else if (field === 'signal_group') {
      const v = parseInt(value);
      if (isNaN(v) || value === '') delete lk.signal_group; else lk.signal_group = v;
    } else if (field === 'capacity') {
      // 空欄だけが「自動（FD 由来）」．0 は「流出を止める」という有効な指定なので削除しない
      // （以前は 0 も削除していて，容量制約が外れて基本図由来の容量に戻っていた）
      const v = parseFloat(value);
      if (isNaN(v) || value === '' || v < 0) delete lk.capacity; else lk.capacity = v;
    } else {
      const v = parseFloat(value);
      if (!isNaN(v)) lk[field] = field === 'number_of_lanes' ? Math.max(1, Math.round(v)) : v;
    }
  }
  drawEditFrame();
  renderEditDemands(false);
}

// ── 需要エディタ ──
function renderEditDemands(show) {
  if (_etDemands.hidden && !show) return;
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const nodeOpts = sel => editNet.nodes.map(n =>
    `<option value="${esc(n.name)}"${n.name === sel ? ' selected' : ''}>${esc(n.name)}</option>`).join('');
  let html = `<div class="ep-title">需要 (OD)<button class="ep-close" onclick="document.getElementById('edit-demands').hidden=true">×</button></div>`;
  html += `<div class="ed-row" style="color:var(--sub);font-size:9px"><span style="width:72px">orig</span><span style="width:72px">dest</span><span style="width:52px">開始s</span><span style="width:52px">終了s</span><span style="width:52px">台/s</span></div>`;
  editNet.demands.forEach((d, i) => {
    html += `<div class="ed-row">`
      + `<select data-ed="orig" data-i="${i}">${nodeOpts(d.orig)}</select>`
      + `<select data-ed="dest" data-i="${i}">${nodeOpts(d.dest)}</select>`
      + `<input data-ed="t_start" data-i="${i}" type="number" min="0" value="${d.t_start}">`
      + `<input data-ed="t_end" data-i="${i}" type="number" min="0" value="${d.t_end}">`
      + `<input data-ed="flow" data-i="${i}" type="number" step="0.05" min="0" value="${d.flow}">`
      + `<button class="ed-x" data-del="${i}" title="削除">×</button></div>`;
  });
  html += `<div class="ep-actions"><button id="ed-add">+ 需要を追加</button></div>`;
  _etDemands.innerHTML = html;
  _etDemands.querySelectorAll('[data-ed]').forEach(el => {
    el.addEventListener('change', () => {
      const d = editNet.demands[+el.dataset.i];
      if (!d) return;
      const f = el.dataset.ed;
      d[f] = (f === 'orig' || f === 'dest') ? el.value : (parseFloat(el.value) || 0);
    });
  });
  _etDemands.querySelectorAll('[data-del]').forEach(el => {
    el.addEventListener('click', () => { editNet.demands.splice(+el.dataset.del, 1); renderEditDemands(true); });
  });
  const add = _etDemands.querySelector('#ed-add');
  if (add) add.addEventListener('click', () => {
    if (editNet.nodes.length < 2) { showToast('ノードが 2 つ以上必要です', 'warn'); return; }
    const tmax = parseInt(document.getElementById('et-tmax').value) || 3600;
    editNet.demands.push({ orig: editNet.nodes[0].name, dest: editNet.nodes[editNet.nodes.length - 1].name,
                           t_start: 0, t_end: Math.round(tmax / 2), flow: 0.3 });
    renderEditDemands(true);
  });
  if (show) _etDemands.hidden = false;
}

// ── ツールバー ──
_etToolbar.querySelectorAll('.et-tool').forEach(b => {
  b.addEventListener('click', () => {
    editTool = b.dataset.tool;
    editLinkStart = null;
    _etToolbar.querySelectorAll('.et-tool').forEach(x => x.classList.toggle('active', x === b));
    const hints = {
      select: 'クリックで選択・ドラッグで移動（空き地ドラッグでパン）',
      addNode: '空き地をクリックしてノード追加',
      addLink: 'ノードを順にクリックしてリンク作成（連続作成可 / Esc で終了）',
      delete: 'ノード / リンクをクリックで削除',
    };
    setEditHint(hints[editTool] || '');
    drawEditFrame();
  });
});
document.getElementById('et-demand-btn').addEventListener('click', () => {
  if (_etDemands.hidden) renderEditDemands(true); else _etDemands.hidden = true;
});

// ── 実行 ──
document.getElementById('et-run').addEventListener('click', async () => {
  if (editNet.nodes.length < 2) { showToast('ノードが 2 つ以上必要です', 'warn'); return; }
  if (!editNet.links.length) { showToast('リンクがありません', 'warn'); return; }
  if (!editNet.demands.length) { showToast('需要がありません（「需要」から追加してください）', 'warn'); renderEditDemands(true); return; }
  const tmax = Math.max(60, parseInt(document.getElementById('et-tmax').value) || 3600);
  const rtRaw   = document.getElementById('et-rt').value.trim();
  const seedRaw = document.getElementById('et-seed').value.trim();
  const scenario = {
    name: 'edited',
    tmax,
    deltan: (scenarioData && scenarioData.deltan) || 5,
    nodes: editNet.nodes,
    links: editNet.links,
    demands: editNet.demands,
  };
  // 空欄 = 既定（キー自体を送らない）．値があれば元シナリオ由来でも編集値でもそのまま送る
  if (rtRaw !== '' && Number.isFinite(parseFloat(rtRaw)))  scenario.reaction_time = parseFloat(rtRaw);
  if (seedRaw !== '' && Number.isFinite(parseInt(seedRaw))) scenario.random_seed = parseInt(seedRaw);
  const btn = document.getElementById('et-run');
  btn.disabled = true; btn.textContent = '実行中…';
  try {
    const r = await fetch(`${API}/simulate`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(scenario),
    });
    const d = await r.json();
    if (!r.ok) {
      const det = d.detail;
      const msg = typeof det === 'string' ? det
        : Array.isArray(det) ? det.map(x => (x.msg || JSON.stringify(x)).replace(/^Value error, /, '')).join('; ')
        : `HTTP ${r.status}`;
      throw new Error(msg);
    }
    showToast(`シミュレーション完了 (${d.id})`);
    exitEditMode();
    await loadResult(d.id);
    addMsg('system', `エディタで作成したネットワークを実行しました（sim_id: ${d.id}，` +
      `${scenario.nodes.length} ノード / ${scenario.links.length} リンク / ${scenario.demands.length} 需要，` +
      `deltan ${scenario.deltan}，反応時間 ${scenario.reaction_time ?? '既定'}，seed ${scenario.random_seed ?? 'なし'}）`);
  } catch (e) {
    showToast(`実行エラー: ${e.message}`, 'error', 5000);
  } finally {
    btn.disabled = false; btn.textContent = '▶ 実行';
  }
});

