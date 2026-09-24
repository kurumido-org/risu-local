// RISU UI — 接続確認・添付・ダウンロード・メッセージ送受信・Markdown．
// index.html の <script> から切り出したもの（内容・順序は同じ）．classic script として
// 同じグローバルスコープを共有するので，index.html の読み込み順を変えないこと．
// DOM に触らないロジックは risu-core.js（node でテスト）へ．
// ── 接続確認 ──
async function checkHealth() {
  try {
    const r = await fetch(`${API}/docs`, { signal: AbortSignal.timeout(3000) });
    setStatus(r.ok);
  } catch { setStatus(false); }
}
function setStatus(ok) {
  document.getElementById('status-dot').className = 'status-dot ' + (ok ? 'ok' : 'err');
  document.getElementById('status-label').textContent = ok ? 'online' : 'offline';
}
checkHealth();

// ── ファイル添付管理 ──
let attachedFiles = [];

function handleFileSelect(event) {
  const fileList = event.target.files;
  if (!fileList || fileList.length === 0) return;
  for (const f of fileList) attachedFiles.push(f);
  event.target.value = '';
  renderFilePreview();
}

function removeAttachedFile(idx) {
  attachedFiles.splice(idx, 1);
  renderFilePreview();
}

function renderFilePreview() {
  const preview = document.getElementById('file-preview');
  const fileBtn = document.getElementById('file-btn');
  preview.innerHTML = '';
  if (attachedFiles.length === 0) {
    preview.classList.remove('visible');
    fileBtn.classList.remove('has-files');
    return;
  }
  preview.classList.add('visible');
  fileBtn.classList.add('has-files');
  attachedFiles.forEach((f, i) => {
    const chip = document.createElement('span');
    chip.className = 'file-chip';
    const icon = document.createTextNode('\uD83D\uDCC4 ');
    const nameSpan = document.createElement('span');
    nameSpan.textContent = f.name;
    const rm = document.createElement('span');
    rm.className = 'remove';
    rm.textContent = '\u00D7';
    rm.addEventListener('click', () => removeAttachedFile(i));
    chip.appendChild(icon);
    chip.appendChild(nameSpan);
    chip.appendChild(document.createTextNode(' '));
    chip.appendChild(rm);
    preview.appendChild(chip);
  });
}

// ── 結果ダウンロード（完全: サーバーから直接ストリーム取得してメモリに載せない） ──
async function downloadResult() {
  if (!lastSimId) return;
  try {
    const res = await fetch(`${API}/results/${lastSimId}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = `risu_full_${lastSimId}.json`; a.click();
    URL.revokeObjectURL(url);
  } catch (e) {
    console.error(e);
    alert('ダウンロードに失敗しました: ' + e.message);
  }
}

// ── シナリオのみダウンロード（軽量・再現用） ──
async function downloadScenario() {
  if (!lastSimId) return;
  try {
    const res = await fetch(`${API}/results/${lastSimId}/scenario`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
    const url  = URL.createObjectURL(blob);
    const a    = document.createElement('a');
    a.href = url; a.download = `risu_scenario_${lastSimId}.json`; a.click();
    URL.revokeObjectURL(url);
  } catch (e) {
    console.error(e);
    alert('シナリオ取得に失敗しました: ' + e.message);
  }
}

// ── メッセージ送信 ──
async function sendMessage() {
  const input = document.getElementById('user-input');
  const text  = input.value.trim();
  const hasFiles = attachedFiles.length > 0;
  if (!text && !hasFiles) return;

  // ユーザーメッセージ表示（ファイル名も含める）
  const fileNames = attachedFiles.map(f => f.name);
  const displayText = hasFiles
    ? (text ? `${text}\n📎 ${fileNames.join(', ')}` : `📎 ${fileNames.join(', ')}`)
    : text;
  addMsg('user', displayText);

  // チャット履歴にはテキストのみ追加（ファイル内容はサーバー側で処理）
  const userContent = text || `添付ファイル（${fileNames.join(', ')}）を使ってシミュレーションを実行してください．`;
  chatHistory.push({ role: 'user', content: userContent });
  input.value = '';
  input.style.height = '';

  // ファイルをローカルに保持してクリア
  const filesToSend = [...attachedFiles];
  attachedFiles = [];
  renderFilePreview();

  const btn = document.getElementById('send-btn');
  btn.disabled = true;

  // ── AI ステータスバー制御 ──
  const aiStatus = document.getElementById('ai-status');
  const aiStatusMsg = aiStatus.querySelector('.status-msg');
  function showStatus(msg) {
    aiStatusMsg.textContent = msg;
    aiStatus.classList.add('visible');
  }
  function hideStatus() {
    aiStatus.classList.remove('visible');
  }
  showStatus('RISUが考えています...');

  try {
    if (filesToSend.length > 0) {
      const totalSize = filesToSend.reduce((s, f) => s + (f.size || 0), 0);
      const INLINE_LIMIT = 20000; // 20KB 超は LLM に埋め込まず参照渡し

      if (totalSize > INLINE_LIMIT) {
        // ── 大きい添付: /upload で直接実行し，チャットには sim_id 参照だけを渡す ──
        // （LLM にネットワーク全体を往復させるとサイズ超過になるため．
        //   以降の修正・再実行は LLM が rerun_simulation で行う）
        showStatus('添付ネットワークをアップロードして実行中...');
        const fd = new FormData();
        for (const f of filesToSend) fd.append('files', f);
        const up = await fetch(`${API}/upload`, { method: 'POST', body: fd });
        const ud = await up.json().catch(() => null);
        if (!up.ok) throw new Error((ud && ud.detail) || `アップロード失敗 (HTTP ${up.status})`);
        await loadResult(ud.id);
        addMsg('system', `添付ネットワークを実行しました（sim_id: ${ud.id}）`);
        const last = chatHistory[chatHistory.length - 1];
        last.content += `\n\n[システム: 添付ファイル（${fileNames.join(', ')}）はサーバーで実行済み．` +
          `sim_id="${ud.id}"，統計: ${JSON.stringify(ud.stats || {})}．` +
          `このネットワークの内容は再送不要．修正・再実行は rerun_simulation(base_sim_id="${ud.id}") を使うこと]`;
      } else {
        // ── 小さい添付: 従来どおりメッセージに埋め込む ──
        const fileParts = [];
        for (const f of filesToSend) {
          const text = await f.text();
          fileParts.push(`[添付ファイル: ${f.name}]\n\`\`\`\n${text}\n\`\`\``);
        }
        const last = chatHistory[chatHistory.length - 1];
        last.content = last.content + '\n\n' + fileParts.join('\n\n');
      }
    }

    const res = await fetch(`${API}/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      // last_sim_id: この会話で表示中の sim．サーバーはこれだけを
      // LLM のコンテキスト（rerun_simulation のベース）として注入する
      body: JSON.stringify({ messages: chatHistory, last_sim_id: lastSimId }),
    });

    if (!res.ok) {
      const err = await res.json().catch(() => null);
      const detail = err?.detail;
      const errMsg = typeof detail === 'string' ? detail
        : Array.isArray(detail) ? detail.map(d => d.msg || JSON.stringify(d)).join('; ')
        : `HTTP ${res.status}`;
      throw new Error(errMsg);
    }

    // SSE ストリーミング対応（text/event-stream の場合）
    const contentType = res.headers.get('content-type') || '';
    if (contentType.includes('text/event-stream')) {
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      let data = null;
      let streamingMsgEl = null;
      let streamingBody = null;
      const messagesWrap = document.getElementById('messages');

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        const lines = buffer.split('\n\n');
        buffer = lines.pop();
        for (const chunk of lines) {
          const line = chunk.trim();
          if (!line.startsWith('data: ')) continue;
          try {
            const evt = JSON.parse(line.slice(6));
            if (evt.type === 'progress') {
              showStatus(evt.message);
            } else if (evt.type === 'stream_start') {
              // ストリーミング開始 → メッセージ要素を作成
              showStatus('応答を生成中...');
              if (!streamingMsgEl) {
                streamingMsgEl = addMsg('assistant', '');
                streamingBody = streamingMsgEl.querySelector('.body');
              }
            } else if (evt.type === 'text_delta' && streamingBody) {
              streamingBody._raw = (streamingBody._raw || '') + evt.text;
              scheduleMdRender(streamingBody);
              messagesWrap.scrollTop = messagesWrap.scrollHeight;
            } else if (evt.type === 'stream_end_partial') {
              // 追加ツール実行 → ステータスバー再表示
              showStatus('追加処理中...');
              // テキストを 1 文字も出さずにツール呼び出しへ進んだ場合，空の吹き出しが残るので消す
              if (streamingMsgEl && streamingBody && !(streamingBody._raw || '').trim()) streamingMsgEl.remove();
              streamingMsgEl = null;
              streamingBody = null;
            } else if (evt.type === 'done') {
              data = evt;
            } else if (evt.type === 'error') {
              throw new Error(evt.message);
            }
          } catch (parseErr) {
            if (parseErr.message && !parseErr.message.includes('JSON')) throw parseErr;
          }
        }
      }

      hideStatus();
      if (!data) throw new Error('サーバーからの応答がありませんでした');

      // ストリーミング中のメッセージをクリーンアップ → 最終版で置き換え
      if (streamingMsgEl) streamingMsgEl.remove();
      renderAssistantResponse(data);

    } else {
      // 従来の JSON レスポンス（mock / ollama）
      hideStatus();
      renderAssistantResponse(await res.json());
    }
  } catch(e) {
    hideStatus();
    if (e.name === 'AbortError' || (e.message || '').toLowerCase().includes('abort')) {
      addMsg('system', '中止しました');
    } else {
      const sysEl = addMsg('system', `エラー: ${e.message}`);
      // Add retry action on last user message
      const retryBtn = document.createElement('button');
      retryBtn.className = 'hdr-btn';
      retryBtn.style.marginLeft = '10px';
      retryBtn.textContent = 'RETRY';
      retryBtn.onclick = () => {
        const lastUser = [...chatHistory].reverse().find(m => m.role === 'user');
        if (lastUser) {
          chatHistory.pop(); // remove failed turn
          document.getElementById('user-input').value = lastUser.content;
          sendMessage();
        }
      };
      sysEl.appendChild(retryBtn);
    }
  }
  btn.disabled = false;
}

// LLM のトークン使用量は画面に出さない（内部事情なので）．サーバーログの usage 行で確認する．

// ── Markdown レンダリング（assistant の吹き出しのみ） ──
// marked でパースし DOMPurify でサニタイズしてから innerHTML に渡す．
// ライブラリが読み込めなければ null を返し，呼び出し側が生テキストにフォールバックする．
const _mdReady = (() => {
  if (typeof marked === 'undefined' || typeof DOMPurify === 'undefined') {
    console.warn('[RISU] marked / DOMPurify を読み込めませんでした．Markdown は生テキストで表示します');
    return false;
  }
  const renderer = new marked.Renderer();
  // 外部リンクは新規タブで開く（rel は tabnabbing 対策）
  renderer.link = (href, title, text) => {
    const t = title ? ` title="${title}"` : '';
    return `<a href="${href}"${t} target="_blank" rel="noopener noreferrer">${text}</a>`;
  };
  // breaks: true — LLM は箇条書き内で単一改行を多用するので <br> に変換する
  marked.setOptions({ gfm: true, breaks: true, renderer });
  return true;
})();

function renderMarkdown(text) {
  if (!_mdReady) return null;
  try {
    return DOMPurify.sanitize(marked.parse(String(text ?? '')), { ADD_ATTR: ['target', 'rel'] });
  } catch (err) {
    console.warn('[RISU] Markdown レンダリング失敗:', err);
    return null;
  }
}

// 本文要素に text を流し込む．生テキストは body._raw に必ず保持する
// （ストリーミング中の追記と，空メッセージ判定が textContent に依存できなくなるため）．
function setBody(body, text, markdown) {
  body._raw = text;
  const html = markdown ? renderMarkdown(text) : null;
  if (html === null) {
    body.classList.remove('md');   // .md が無いと CSS 側は pre-wrap のまま
    body.textContent = text;
  } else {
    body.classList.add('md');
    body.innerHTML = html;
  }
}

// ストリーミング中の再レンダリングを 1 フレーム 1 回に間引く
// （text_delta はトークン単位で飛んでくるので，都度 parse すると無駄が多い）．
function scheduleMdRender(body) {
  if (!body || body._mdPending) return;
  body._mdPending = true;
  requestAnimationFrame(() => {
    body._mdPending = false;
    if (body.isConnected) setBody(body, body._raw || '', true);
  });
}

function addMsg(role, text, thinking = false) {
  const wrap = document.getElementById('messages');
  const div  = document.createElement('div');
  div.className = `msg ${role}`;

  if (thinking) {
    const t = document.createElement('div');
    t.className = 'thinking';
    t.innerHTML = '<span></span><span></span><span></span>';
    div.appendChild(t);
  } else {
    const body = document.createElement('div');
    body.className = 'body';
    setBody(body, text, role === 'assistant');
    div.appendChild(body);
  }

  wrap.appendChild(div);
  wrap.scrollTop = wrap.scrollHeight;
  return div;
}

// LLM の最終応答を吹き出しに反映する（SSE 経路と JSON 経路で共通）．
// sim バッジ / チャート / トークン使用量の付け方は両経路で同じなので，
// ここに 1 本化しておく（以前は 2 か所に同じコードがあった）．
function renderAssistantResponse(data) {
  const msgEl = addMsg('assistant', data.content);

  if (data.sim_id) {
    const badge = document.createElement('div');
    badge.className = 'sim-badge';
    badge.textContent = `▶ 結果を表示  ID: ${data.sim_id}`;
    badge.onclick = () => loadResult(data.sim_id);
    msgEl.querySelector('.body').appendChild(badge);
    loadResult(data.sim_id);
  }
  if (data.charts && data.charts.length) {
    for (const chart of data.charts) renderChart(msgEl, chart);
  } else if (data.chart) {
    renderChart(msgEl, data.chart);
  }
  chatHistory.push({ role: 'assistant', content: data.content });
  return msgEl;
}

