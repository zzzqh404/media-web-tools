/* 视频转码页逻辑（API 前缀 /api/video） */
const BASE = '/api/video';

const STATUS_LABEL = {
  queued: '排队中',
  running: '转码中',
  cancelling: '取消中',
  completed: '已完成',
  failed: '失败',
  cancelled: '已取消',
};

let encoders = {};
let presetDefaults = {};   // id -> {label, codec, note, params}，由 /api/video/info 返回
let selectedPreset = 'amd_hevc';
let pending = [];          // 待提交的本地路径
let previewTimer = null;

const PRESET_LABEL_SHORT = l => (l || '').replace(/\s*\([^)]*\)\s*$/, '') || l;

/* ---------------- ffmpeg info ---------------- */
async function loadVideoInfo() {
  for (let attempt = 0; attempt < 5; attempt++) {
    try {
      const info = await api(`${BASE}/info`);
      encoders = info.encoders || {};
      presetDefaults = info.presets || {};
      const el = $('ffmpeg-info');
      el.textContent = `${info.version}  |  本地文件输出到视频所在文件夹`;
      renderPresets();
      applyParams(presetDefaults[selectedPreset]?.params || {});
      updatePreview();
      return;
    } catch (e) {
      if (attempt < 4) {
        $('ffmpeg-info').textContent = '正在检测 ffmpeg... (重试中)';
        await new Promise(r => setTimeout(r, 1000));
      } else {
        $('ffmpeg-info').textContent = '检测失败: ' + e.message;
      }
    }
  }
}

function renderPresets() {
  const grid = $('preset-grid');
  grid.innerHTML = '';
  for (const [id, p] of Object.entries(presetDefaults)) {
    const ok = !!encoders[p.codec];
    const card = document.createElement('div');
    card.className = 'preset-card' + (id === selectedPreset ? ' selected' : '');
    card.dataset.preset = id;
    card.innerHTML = `
      <div class="p-badge ${ok ? 'ok' : 'missing'}">${ok ? '可用' : '不可用'}</div>
      <div class="p-name">${escapeHtml(PRESET_LABEL_SHORT(p.label))}</div>
      <div class="p-note">${escapeHtml(p.note)}</div>
    `;
    card.addEventListener('click', () => selectPreset(id));
    grid.appendChild(card);
  }
  syncPresetFields();
}

function selectPreset(id) {
  selectedPreset = id;
  document.querySelectorAll('.preset-card').forEach(c => c.classList.toggle('selected', c.dataset.preset === id));
  syncPresetFields();
  applyParams(presetDefaults[id]?.params || {});
  schedulePreview();
}

function syncPresetFields() {
  const codec = presetDefaults[selectedPreset]?.codec || 'hevc_amf';
  $('field-x265-preset').classList.toggle('hidden', codec !== 'libx265');
  $('field-amf-quality').classList.toggle('hidden', codec !== 'hevc_amf');
  $('field-nvenc-preset').classList.toggle('hidden', !codec.endsWith('_nvenc'));
  $('field-qsv-preset').classList.toggle('hidden', !codec.endsWith('_qsv'));
  $('crf-name').textContent = codec === 'hevc_amf' ? 'QP' : 'CRF';
}

/* ---------------- apply preset defaults to form ---------------- */
function applyParams(p) {
  const mode = p.mode === 'bitrate' ? 'bitrate' : 'crf';
  const modeRadio = document.querySelector(`input[name="mode"][value="${mode}"]`);
  if (modeRadio) modeRadio.checked = true;
  $('field-crf').classList.toggle('hidden', mode !== 'crf');
  $('field-bitrate').classList.toggle('hidden', mode !== 'bitrate');

  if (p.crf !== undefined && p.crf !== '') { $('crf').value = p.crf; $('crf-value').textContent = p.crf; }
  if (p.bitrate !== undefined) $('bitrate').value = p.bitrate;
  if (p.maxrate !== undefined) $('maxrate').value = p.maxrate;
  if (p.bufsize !== undefined) $('bufsize').value = p.bufsize;
  if (p.x265_preset !== undefined) $('x265-preset').value = p.x265_preset;
  if (p.nvenc_preset !== undefined) $('nvenc-preset').value = p.nvenc_preset;
  if (p.qsv_preset !== undefined) $('qsv-preset').value = p.qsv_preset;
  if (p.amf_quality !== undefined) $('amf-quality').value = p.amf_quality;
  if (p.fps !== undefined) $('fps').value = p.fps;
  if (p.audio !== undefined) $('audio').value = p.audio;
  if (p.keep_all_audio !== undefined) $('keep-all-audio').value = String(p.keep_all_audio);
  if (p.custom_args !== undefined) $('custom-args').value = p.custom_args;

  const knownRes = ['original', '3840:2160', '2560:1440', '1920:1080',
                    '2160:3840', '1440:2560', '1080:1920'];
  const res = String(p.resolution || 'original');
  const showCustom = !knownRes.includes(res);
  $('resolution').value = showCustom ? 'custom' : res;
  $('res-w').classList.toggle('hidden', !showCustom);
  $('res-h').classList.toggle('hidden', !showCustom);
  if (showCustom) {
    if (p.res_w) $('res-w').value = p.res_w;
    if (p.res_h) $('res-h').value = p.res_h;
  }
}

/* ---------------- pending items ---------------- */
function renderPending() {
  const list = $('pending-list');
  list.innerHTML = '';
  pending.forEach((it, i) => {
    const div = document.createElement('div');
    div.className = 'pending-item';
    div.innerHTML = `<span class="name">📄 ${escapeHtml(it.path)}</span><span class="remove" title="移除">✕</span>`;
    div.querySelector('.remove').addEventListener('click', () => {
      pending.splice(i, 1);
      renderPending();
      schedulePreview();
    });
    div.addEventListener('click', (e) => {
      if (e.target.classList.contains('remove')) return;
      openPathDetail(it.path);
    });
    list.appendChild(div);
  });
  scheduleProbe();
  schedulePreview();
}

function addPath(path) {
  path = (path || '').trim().replace(/^"|"$/g, '').replace(/^'|'$/g, '');
  if (!path) return;
  if (pending.some(p => p.type === 'path' && p.path === path)) return;
  pending.push({ type: 'path', path });
  $('path-input').value = '';
  renderPending();
}

/* ---------------- form helpers ---------------- */
function collectParams() {
  const mode = document.querySelector('input[name="mode"]:checked').value;
  const res = $('resolution').value;
  return {
    preset: selectedPreset,
    mode,
    crf: $('crf').value,
    bitrate: $('bitrate').value.trim() || '8000k',
    maxrate: $('maxrate').value.trim(),
    bufsize: $('bufsize').value.trim(),
    resolution: res,
    res_w: $('res-w').value,
    res_h: $('res-h').value,
    fps: $('fps').value,
    audio: $('audio').value,
    keep_all_audio: $('keep-all-audio').value,
    x265_preset: $('x265-preset').value,
    nvenc_preset: $('nvenc-preset').value,
    qsv_preset: $('qsv-preset').value,
    amf_quality: $('amf-quality').value,
    custom_args: $('custom-args').value.trim(),
  };
}

async function submitTasks() {
  const errEl = $('submit-error');
  errEl.textContent = '';
  if (!pending.length) {
    errEl.textContent = '请先添加视频文件或路径。';
    return;
  }
  const params = collectParams();
  const fd = new FormData();
  for (const k of Object.keys(params)) fd.append(k, params[k]);
  pending.forEach(p => fd.append('path', p.path));
  $('btn-start').disabled = true;
  try {
    const data = await api(`${BASE}/tasks`, { method: 'POST', body: fd });
    pending = [];
    renderPending();
    errEl.textContent = '';
    schedulePreview();
  } catch (e) {
    errEl.textContent = e.message;
  } finally {
    $('btn-start').disabled = false;
  }
}

/* ---------------- command preview ---------------- */
function schedulePreview() {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(updatePreview, 350);
}

async function updatePreview() {
  const params = collectParams();
  const qs = new URLSearchParams(params);
  const first = pending[0];
  if (first) {
    qs.set('path', first.path);
    qs.set('name', first.path.split(/[\\/]/).pop());
  }
  try {
    const d = await api(`${BASE}/preview?` + qs.toString());
    $('cmd-preview').textContent = d.cmd;
    $('cmd-preview').title = d.cmd;
    $('out-preview').textContent = '📄 输出: ' + d.output;
  } catch (e) {
    $('cmd-preview').textContent = '预览失败: ' + e.message;
    $('out-preview').textContent = '';
  }
}

/* ---------------- video info panel ---------------- */
let probeTimer = null;

function infoRows(info) {
  const v = info.video || {};
  const a = info.audio || {};
  return [
    ['文件名', info.name],
    ['容器', info.container],
    ['大小', formatBytes(info.size_bytes)],
    ['时长', info.duration ? fmtDuration(info.duration) : '未知'],
    ['分辨率', v.width && v.height ? `${v.width}×${v.height}` : ''],
    ['帧率', v.fps ? v.fps + ' fps' : ''],
    ['视频编码', v.codec],
    ['视频码率', v.bitrate ? Math.round(v.bitrate / 1000) + ' kb/s' : ''],
    ['像素格式', v.pix_fmt],
    ['音频编码', a.codec || '无'],
    ['音频码率', a.bitrate ? Math.round(a.bitrate / 1000) + ' kb/s' : ''],
    ['采样率', a.sample_rate ? (+a.sample_rate / 1000) + ' kHz' : ''],
  ].filter(r => r[1]);
}

function infoRowsHtml(info) {
  return infoRows(info).map(r =>
    `<div class="v-row"><span>${r[0]}</span><b>${escapeHtml(String(r[1]))}</b></div>`
  ).join('');
}

function renderVideoInfo(info) {
  const panel = $('vinfo-panel');
  panel.classList.toggle('hidden', !info);
  if (!info) return;
  $('video-info').innerHTML = infoRowsHtml(info);
}

async function probeFirst() {
  const first = pending[0];
  if (!first || first.type !== 'path') {
    renderVideoInfo(null);
    return;
  }
  const box = $('video-info');
  $('vinfo-panel').classList.remove('hidden');
  box.innerHTML = '<span class="m-loading">正在读取视频信息...</span>';
  try {
    const info = await api(`${BASE}/probe?path=` + encodeURIComponent(first.path));
    renderVideoInfo(info);
  } catch (e) {
    box.innerHTML = `<div class="v-row" style="color:var(--red)">读取失败: ${escapeHtml(e.message)}</div>`;
  }
}

function scheduleProbe() {
  clearTimeout(probeTimer);
  probeTimer = setTimeout(probeFirst, 300);
}

/* ---------------- detail modal ---------------- */
async function loadInfoInto(box, path) {
  try {
    const info = await api(`${BASE}/probe?path=` + encodeURIComponent(path));
    box.innerHTML = infoRowsHtml(info);
  } catch (e) {
    box.innerHTML = `<div class="v-row" style="color:var(--red)">读取失败: ${escapeHtml(e.message)}</div>`;
  }
}

function showDetail(name, source, output, cmd, status) {
  $('modal-name').textContent = name || '';
  $('modal-source').textContent = source || '';
  $('modal-output').textContent = output || '';
  $('modal-output-row').style.display = output ? '' : 'none';
  $('modal-cmd').textContent = cmd || '';
  $('modal-cmd-box').style.display = cmd ? '' : 'none';
  const info = $('modal-info');
  info.innerHTML = '<span class="m-loading">正在读取视频信息...</span>';

  const hasOut = !!output && status === 'completed';
  $('modal-out-box').style.display = hasOut ? '' : 'none';
  if (hasOut) {
    $('modal-out-info').innerHTML = '<span class="m-loading">正在读取输出视频信息...</span>';
    loadInfoInto($('modal-out-info'), output);
  }

  $('modal').classList.remove('hidden');
  loadInfoInto(info, source);
}

function closeModal() {
  $('modal').classList.add('hidden');
}

function openTaskDetail(t) {
  showDetail(t.name, t.source, t.output || '', t.cmd || '', t.status);
}

function openPathDetail(path) {
  showDetail(path.split(/[\\/]/).pop(), path);
}

/* ---------------- task list ---------------- */
function runningLabel(t) {
  const pct = t.duration ? t.progress : null;
  if (pct === null) return '正在转码...';
  let txt = pct.toFixed(1) + '%';
  if (t.speed) txt += ' · ' + Number(t.speed).toFixed(2) + 'x';
  const remain = (t.duration && t.speed > 0)
    ? t.duration * (1 - t.progress / 100) / t.speed : 0;
  if (remain > 0) txt += ' · 剩余 ' + fmtDuration(remain);
  return txt;
}

function buildVideoCard(t, prev) {
  const presetLabel = PRESET_LABEL_SHORT(presetDefaults[t.params.preset]?.label) || t.params.preset;
  const pct = t.status === 'running' && !t.duration ? null : t.progress;
  const barClass = ['running', 'cancelling'].includes(t.status) ? 'running'
    : t.status === 'completed' ? 'completed'
    : t.status === 'failed' ? 'failed' : '';
  const sizeTxt = t.status === 'completed' && t.out_size
    ? ` · ${formatBytes(t.out_size)}` +
      (t.src_size ? `（压缩到 ${Math.max(1, Math.round(t.out_size / t.src_size * 100))}%）` : '')
    : '';

  const actions = [];
  if (['queued', 'running'].includes(t.status)) actions.push({ act: 'cancel', label: '取消', cls: 'danger' });
  if (t.status === 'completed') actions.push({ act: 'download', label: '下载' }, { act: 'open', label: '打开所在文件夹' });
  if (['failed', 'cancelled'].includes(t.status)) actions.push({ act: 'retry', label: '重试' });
  if (t.cmd) actions.push({ act: 'copycmd', label: '复制命令' });

  const card = buildTaskCard(t, {
    sub: `${presetLabel} · 时长 ${t.duration ? fmtDuration(t.duration) : '未知'}${sizeTxt}`,
    barClass,
    barPct: pct ?? 0,
    runningText: t.status === 'running' ? runningLabel(t) : '',
    actions,
  });
  card.id = 'task-' + t.id;

  bindTaskActions(card, t, (btn, act) => {
    if (act === 'copycmd') {
      navigator.clipboard.writeText(t.cmd);
      return;
    }
    commonTaskAction(btn, act, t, BASE, board.refreshTasks);
  });

  card.addEventListener('click', (e) => {
    if (e.target.closest('[data-act]')) return;
    openTaskDetail(t);
  });

  if (t.status === 'completed' && prev !== 'completed') flashCompleted(card);
  return card;
}

/* 签名未变（仍在转码）时原地更新进度条与剩余时间，避免整卡重建 */
function updateRunningCard(card, t) {
  if (t.status !== 'running') return;
  const pct = t.duration ? t.progress : null;
  card.querySelector('.progress-fill').style.width = (pct ?? 0) + '%';
  card.querySelector('.pl-left').textContent = runningLabel(t);
}

const board = createTaskBoard({
  basePath: BASE,
  sigKeys: ['status', 'error', 'output', 'duration', 'out_size'],
  buildCard: buildVideoCard,
  updateLive: updateRunningCard,
});

/* ---------------- picker ---------------- */
async function pickAndAdd(kind) {
  const d = await openPicker(kind, 'video', 'submit-error');
  if (!d) return;
  if (kind === 'dir') {
    if (d.path) addPath(d.path);
  } else {
    (d.paths || []).forEach(p => addPath(p));
  }
}

/* ---------------- events ---------------- */
function init() {
  loadVideoInfo();

  // 速度档下拉选项
  const xp = $('x265-preset');
  ['ultrafast','superfast','veryfast','faster','fast','medium','slow','slower','veryslow'].forEach(p => {
    const o = document.createElement('option');
    o.value = p;
    o.textContent = p;
    if (p === 'medium') o.selected = true;
    xp.appendChild(o);
  });
  const np = $('nvenc-preset');
  ['p1','p2','p3','p4','p5','p6','p7'].forEach(p => {
    const o = document.createElement('option');
    o.value = p;
    o.textContent = p;
    if (p === 'p5') o.selected = true;
    np.appendChild(o);
  });
  const qp = $('qsv-preset');
  ['veryfast','faster','fast','medium','slow','slower','veryslow'].forEach(p => {
    const o = document.createElement('option');
    o.value = p;
    o.textContent = p;
    if (p === 'medium') o.selected = true;
    qp.appendChild(o);
  });

  $('btn-pick').addEventListener('click', () => pickAndAdd('files'));
  $('btn-pick-dir').addEventListener('click', () => pickAndAdd('dir'));

  $('modal-close').addEventListener('click', closeModal);
  $('modal').addEventListener('click', e => { if (e.target === $('modal')) closeModal(); });
  document.addEventListener('keydown', e => { if (e.key === 'Escape') closeModal(); });

  $('btn-add-path').addEventListener('click', () => addPath($('path-input').value));
  $('path-input').addEventListener('keydown', e => { if (e.key === 'Enter') addPath(e.target.value); });

  $('btn-start').addEventListener('click', submitTasks);
  $('btn-clear').addEventListener('click', async () => { await api(`${BASE}/tasks/clear`, { method: 'POST' }); board.refreshTasks(); });
  $('btn-stop').addEventListener('click', async () => {
    try {
      await api(`${BASE}/stop-all`, { method: 'POST' });
    } catch (e) { $('submit-error').textContent = e.message; }
  });
  $('btn-restart').addEventListener('click', async () => {
    if (!confirm('确定要重启服务吗？会中断当前正在处理的转码/压缩任务。')) return;
    try {
      await api('/api/restart', { method: 'POST' });
      $('ffmpeg-info').textContent = '正在重启服务，请稍候...';
      setTimeout(() => location.reload(), 3500);
    } catch (e) { alert('重启失败: ' + e.message); }
  });

  $('crf').addEventListener('input', () => { $('crf-value').textContent = $('crf').value; });
  document.querySelectorAll('input[name="mode"]').forEach(r => r.addEventListener('change', () => {
    const m = document.querySelector('input[name="mode"]:checked').value;
    $('field-crf').classList.toggle('hidden', m !== 'crf');
    $('field-bitrate').classList.toggle('hidden', m !== 'bitrate');
  }));
  $('resolution').addEventListener('change', () => {
    const custom = $('resolution').value === 'custom';
    $('res-w').classList.toggle('hidden', !custom);
    $('res-h').classList.toggle('hidden', !custom);
  });

  // 参数变化时刷新命令预览
  document.querySelector('.params').addEventListener('input', schedulePreview);
  document.querySelector('.params').addEventListener('change', schedulePreview);

  updatePreview();

  board.refreshTasks();
  board.pollLoop();
}

document.addEventListener('DOMContentLoaded', init);
