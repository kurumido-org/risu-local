// RISU UI — 再生制御（タイムライン）．
// index.html の <script> から切り出したもの（内容・順序は同じ）．classic script として
// 同じグローバルスコープを共有するので，index.html の読み込み順を変えないこと．
// DOM に触らないロジックは risu-core.js（node でテスト）へ．
// ── 再生制御 ──
let playing = false, animStartTime = 0, animStartVal = 0, animFrameId = null;
let playbackSpeed = 1;            // 1x = PLAY_CYCLE_MS で 1 周
const PLAY_CYCLE_MS = 10000;      // 1x のとき 1 周にかかる時間 (ms)
const playBtn      = document.getElementById('play-btn');
const timeSlider   = document.getElementById('time-slider');
const timeLabel    = document.getElementById('time-label');
const speedSelect  = document.getElementById('speed-select');

playBtn.addEventListener('click', togglePlay);
timeSlider.addEventListener('input', onSliderInput);
speedSelect.addEventListener('change', onSpeedChange);

function togglePlay() { playing ? stopPlay() : startPlay(); }

function startPlay() {
  playing = true;
  playBtn.textContent = '\u23F8';
  animStartTime = performance.now();
  animStartVal  = parseFloat(timeSlider.value);
  if (animFrameId) cancelAnimationFrame(animFrameId);
  animFrameId = requestAnimationFrame(tick);
}
function stopPlay() {
  playing = false;
  playBtn.textContent = '\u25B6';
  if (animFrameId) { cancelAnimationFrame(animFrameId); animFrameId = null; }
}
function tick(now) {
  if (!playing) return;
  const cycleMs = PLAY_CYCLE_MS / playbackSpeed;
  let val = animStartVal + ((now - animStartTime) / cycleMs) * 1000;
  if (val >= 1000) { val %= 1000; animStartTime = now; animStartVal = val; }
  timeSlider.value = val;
  drawFrame();
  if (typeof drawAllStatCharts === 'function') drawAllStatCharts();
  if (typeof updateStatValues === 'function') updateStatValues();
  if (typeof updateTimeLabel === 'function') updateTimeLabel();
  animFrameId = requestAnimationFrame(tick);
}
function onSliderInput() { if (playing) stopPlay(); drawFrame(); }
function onSpeedChange() {
  playbackSpeed = parseFloat(speedSelect.value) || 1;
  // 再生中なら基準点を現在位置にリセットして連続性を保つ
  if (playing) {
    animStartTime = performance.now();
    animStartVal  = parseFloat(timeSlider.value);
  }
}

