// RISU UI — Canvas のホバーツールチップ．
// index.html の <script> から切り出したもの（内容・順序は同じ）．classic script として
// 同じグローバルスコープを共有するので，index.html の読み込み順を変えないこと．
// DOM に触らないロジックは risu-core.js（node でテスト）へ．
// ══════════════════════════════════════════════════════════════════
// Canvas ホバーツールチップ
// ══════════════════════════════════════════════════════════════════
(function setupCanvasHover() {
  const tooltip = document.getElementById('canvas-tooltip');
  if (!tooltip) return;
  let hoverTimer = null;
  let lastTarget = null;

  function pointToSegmentDist(px, py, ax, ay, bx, by) {
    const dx = bx - ax, dy = by - ay;
    const len2 = dx*dx + dy*dy;
    if (len2 === 0) return Math.hypot(px - ax, py - ay);
    let t = ((px - ax) * dx + (py - ay) * dy) / len2;
    t = Math.max(0, Math.min(1, t));
    return Math.hypot(px - (ax + t*dx), py - (ay + t*dy));
  }

  function pointToLinkDist(mx, my, info) {
    if (info.multipoint && info.screenCoords) {
      let best = Infinity;
      const sc = info.screenCoords;
      for (let i = 1; i < sc.length; i++) {
        const d = pointToSegmentDist(mx, my, sc[i-1][0], sc[i-1][1], sc[i][0], sc[i][1]);
        if (d < best) best = d;
      }
      return best;
    }
    if (info.bidir && info.cpx !== undefined) {
      // ベジェ曲線を 8 分割で近似してセグメント距離を取る
      let best = Infinity;
      const N = 8;
      let prev = [info.ax, info.ay];
      for (let i = 1; i <= N; i++) {
        const t = i / N;
        const x = (1-t)*(1-t)*info.ax + 2*(1-t)*t*info.cpx + t*t*info.bx;
        const y = (1-t)*(1-t)*info.ay + 2*(1-t)*t*info.cpy + t*t*info.by;
        const d = pointToSegmentDist(mx, my, prev[0], prev[1], x, y);
        if (d < best) best = d;
        prev = [x, y];
      }
      return best;
    }
    return pointToSegmentDist(mx, my, info.ax, info.ay, info.bx, info.by);
  }

  function findHover(mx, my) {
    // ノード優先（半径 12px 以内）
    let bestNode = null, bestNodeDist = 12;
    for (const n of hitTargets.nodes) {
      const d = Math.hypot(mx - n.sx, my - n.sy);
      if (d < bestNodeDist) { bestNodeDist = d; bestNode = n; }
    }
    if (bestNode) return { type: 'node', target: bestNode };
    // リンク（距離 6px 以内）
    let bestLink = null, bestLinkDist = 6;
    for (const lk of hitTargets.links) {
      const d = pointToLinkDist(mx, my, lk.info);
      if (d < bestLinkDist) { bestLinkDist = d; bestLink = lk; }
    }
    if (bestLink) return { type: 'link', target: bestLink };
    return null;
  }

  function buildTooltipHTML(hit) {
    if (hit.type === 'node') {
      const n = hit.target;
      const name = n.name || '(unnamed)';
      let html = `<div class="tt-kind">NODE</div>`;
      html += `<div class="tt-id">${escapeHtml(name)}</div>`;
      html += `<div class="tt-meta">`;
      html += `位置: <b>(${n.x.toFixed(0)}, ${n.y.toFixed(0)})</b>`;
      if (n.signal) {
        html += `<br>信号: <b>[${n.signal.join(', ')}]</b> 秒`;
        html += `<br>サイクル: <b>${n.signal.reduce((a,b)=>a+b,0)}</b> 秒 / ${n.signal.length} 現示`;
      }
      html += `</div>`;
      html += `<div class="tt-hint">チャットで「ノード <b>${escapeHtml(name)}</b>」と参照</div>`;
      return html;
    } else {
      const lk = hit.target;
      const p = lk.properties || {};
      let html = `<div class="tt-kind">LINK</div>`;
      html += `<div class="tt-id">${escapeHtml(lk.name)}</div>`;
      html += `<div class="tt-meta">`;
      if (p.length) html += `延長: <b>${Math.round(p.length)} m</b><br>`;
      if (p.free_flow_speed) html += `自由流速度: <b>${p.free_flow_speed} m/s</b><br>`;
      if (p.number_of_lanes) html += `車線数: <b>${p.number_of_lanes}</b><br>`;
      // 該当 link が信号制御下なら group を表示
      for (const sig of signalsData) {
        if (sig.groups && sig.groups[lk.name] !== undefined) {
          const gg = sig.groups[lk.name];
          const nPh = (sig.phases || []).length;
          const label = (Array.isArray(gg) && nPh > 1 && gg.length >= nPh) ? '全現示（常時青）'
                      : Array.isArray(gg) ? gg.join(', ') : String(gg);
          html += `信号 group: <b>${escapeHtml(label)}</b> @ ノード <b>${escapeHtml(sig.node)}</b><br>`;
          break;
        }
      }
      html += `</div>`;
      html += `<div class="tt-hint">チャットで「リンク <b>${escapeHtml(lk.name)}</b>」と参照</div>`;
      return html;
    }
  }

  function escapeHtml(s) {
    if (s == null) return '';
    return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  }

  function showTooltip(hit, mx, my) {
    tooltip.innerHTML = buildTooltipHTML(hit);
    tooltip.hidden = false;
    // 位置調整: カーソル右下，ただし画面端を回避
    const rect = canvasWrap.getBoundingClientRect();
    let left = mx + 14, top = my + 14;
    requestAnimationFrame(() => {
      const tw = tooltip.offsetWidth, th = tooltip.offsetHeight;
      if (left + tw > rect.width)  left = mx - tw - 14;
      if (top + th > rect.height)  top  = my - th - 14;
      tooltip.style.left = Math.max(4, left) + 'px';
      tooltip.style.top  = Math.max(4, top)  + 'px';
      tooltip.classList.add('visible');
    });
  }

  function hideTooltip() {
    tooltip.classList.remove('visible');
    setTimeout(() => { if (!tooltip.classList.contains('visible')) tooltip.hidden = true; }, 200);
  }

  canvasEl.addEventListener('mousemove', (e) => {
    if (editMode) { hideTooltip(); return; }  // 編集モードは独自のホバー処理
    if (isDragging) { hideTooltip(); return; }
    const rect = canvasEl.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    const my = e.clientY - rect.top;
    const hit = findHover(mx, my);
    if (!hit) {
      if (hoverTimer) { clearTimeout(hoverTimer); hoverTimer = null; }
      hideTooltip();
      lastTarget = null;
      return;
    }
    const targetKey = hit.type + ':' + (hit.type === 'node' ? hit.target.name + '@' + hit.target.x + ',' + hit.target.y : hit.target.name);
    if (targetKey === lastTarget) {
      // 同じ対象上で動いているだけ → 位置だけ更新
      if (!tooltip.hidden) {
        tooltip.style.left = (mx + 14) + 'px';
        tooltip.style.top  = (my + 14) + 'px';
      }
      return;
    }
    lastTarget = targetKey;
    if (hoverTimer) clearTimeout(hoverTimer);
    hoverTimer = setTimeout(() => showTooltip(hit, mx, my), 350);
  });

  canvasEl.addEventListener('mouseleave', () => {
    if (hoverTimer) { clearTimeout(hoverTimer); hoverTimer = null; }
    hideTooltip();
    lastTarget = null;
  });
})();

// 起動時のヘルスチェックとステータス更新
setTimeout(async () => {
  await checkHealth();
  const dot = document.getElementById('status-dot');
  const label = document.getElementById('status-label');
  if (dot.classList.contains('ok')) {
    label.textContent = 'CONNECTED';
  } else {
    label.textContent = 'OFFLINE';
    addMsg('system', 'API に接続できません．server.py を起動してください: python server.py');
  }
}, 500);

