/* 照片压缩页逻辑（API 前缀 /api/photo） */
const BASE = '/api/photo';

const STATUS_LABEL = {
  queued: '排队中',
  running: '压缩中',
  cancelling: '取消中',
  completed: '已完成',
  failed: '失败',
  cancelled: '已取消',
};

// 各预设的质量滑条范围与显示名
const QUALITY_META = {
  jpeg: { min: 1, max: 95, name: '质量' },
  webp: { min: 0, max: 100, name: '质量' },
  avif: { min: 0, max: 100, name: '质量' },
  png: null,
};

const ORIENTATION_LABEL = {
  1: '正常', 2: '水平翻转', 3: '旋转180°', 4: '垂直翻转',
  5: '转置', 6: '需旋转90°', 7: '反向转置', 8: '需旋转270°',
};

let caps = {};             // /api/photo/caps 响应：{pillow, heif_input, presets}
let presetDefaults = {};   // id -> {label, note, ext, available, params}
let selectedPreset = 'avif';
let pending = [];          // 待提交的本地路径
let previewTimer = null;

/* ---------------- caps / presets ---------------- */
async function loadCaps() {
  for (let attempt = 0; attempt < 5; attempt++) {
    try {
      caps = await api(`${BASE}/caps`);
      presetDefaults = caps.presets || {};
      const heif = caps.heif_input ? 'HEIC 可读' : 'HEIC 不可读（缺 pillow-heif）';
      $('caps-info').textContent =
        `Pillow ${caps.pillow}  |  ${heif}  |  并发 ${caps.workers}  |  输出到照片所在文件夹`;
      // 默认 AVIF；预设缺失或不可用（如 Pillow 无 AVIF）时退回第一个可用的
      const cur = presetDefaults[selectedPreset];
      if (!cur || !cur.available) {
        const firstOk = Object.entries(presetDefaults).find(([, p]) => p.available);
        selectedPreset = firstOk ? firstOk[0] : Object.keys(presetDefaults)[0];
      }
      renderPresets();
      applyParams(presetDefaults[selectedPreset]?.params || {});
      updatePreview();
      return;
    } catch (e) {
      if (attempt < 4) {
        $('caps-info').textContent = '正在检测 Pillow... (重试中)';
        await new Promise(r => setTimeout(r, 1000));
      } else {
        $('caps-info').textContent = '检测失败: ' + e.message;
      }
    }
  }
}

function renderPresets() {
  const grid = $('preset-grid');
  grid.innerHTML = '';
  for (const [id, p] of Object.entries(presetDefaults)) {
    const card = document.createElement('div');
    card.className = 'preset-card' + (id === selectedPreset ? ' selected' : '');
    card.dataset.preset = id;
    card.innerHTML = `
      <div class="p-badge ${p.available ? 'ok' : 'missing'}">${p.available ? '可用' : '不可用'}</div>
      <div class="p-name">${escapeHtml(p.label)}</div>
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
  const meta = QUALITY_META[selectedPreset];
  $('field-quality').classList.toggle('hidden', !meta);
  $('field-lossless').classList.toggle('hidden', selectedPreset !== 'webp');
  $('field-speed').classList.toggle('hidden', selectedPreset !== 'avif');
  if (meta) {
    $('quality-name').textContent = meta.name;
    $('quality').min = meta.min;
    $('quality').max = meta.max;
  }
}

/* ---------------- apply preset defaults to form ---------------- */
function applyParams(p) {
  const meta = QUALITY_META[selectedPreset];
  if (meta) {
    const q = p.quality !== undefined && p.quality !== '' ? p.quality : meta.min;
    $('quality').value = q;
    $('quality-value').textContent = q;
  }
  $('lossless').checked = String(p.lossless ?? '0') === '1';
  if (p.speed !== undefined) $('speed').value = p.speed;
  $('keep-exif').checked = String(p.keep_exif ?? '1') === '1';
  $('del-source').checked = String(p.del_source ?? '0') === '1';

  const resize = p.resize || 'original';
  $('resize').value = resize;
  $('resize-px').classList.toggle('hidden', resize === 'original');
  if (p.resize_px) $('resize-px').value = p.resize_px;
}

/* ---------------- pending items ---------------- */
function renderPending() {
  const list = $('pending-list');
  list.innerHTML = '';
  pending.forEach((it, i) => {
    const div = document.createElement('div');
    div.className = 'pending-item';
    if (it.type === 'dir') {
      // 文件夹：显示勾选后的照片数 + 各格式勾选框
      const selected = it.selected.filter(f => it.formats[f]);
      const count = selected.reduce((s, f) => s + it.formats[f], 0);
      const fmtHtml = Object.entries(it.formats).map(([f, n]) => `
        <label class="fmt-item" title="只处理 ${f} 格式的照片">
          <input type="checkbox" data-fmt="${f}" ${it.selected.includes(f) ? 'checked' : ''}>
          ${escapeHtml(f)} ${n}
        </label>`).join('');
      const basename = it.path.split(/[\\/]/).pop() || it.path;
      div.innerHTML = `
        <span class="name" title="${escapeHtml(it.path)}">📁 ${escapeHtml(basename)}（${count} 张）</span>
        <span class="fmt-list">${fmtHtml}</span>
        <span class="remove" title="移除">✕</span>`;
      div.querySelectorAll('input[data-fmt]').forEach(cb => {
        cb.addEventListener('change', () => {
          const f = cb.dataset.fmt;
          if (cb.checked) {
            if (!it.selected.includes(f)) it.selected.push(f);
          } else {
            it.selected = it.selected.filter(x => x !== f);
          }
          renderPending();
          schedulePreview();
        });
      });
    } else {
      div.innerHTML = `<span class="name">🖼️ ${escapeHtml(it.path)}</span><span class="remove" title="移除">✕</span>`;
    }
    div.querySelector('.remove').addEventListener('click', () => {
      pending.splice(i, 1);
      renderPending();
      schedulePreview();
    });
    div.addEventListener('click', (e) => {
      if (e.target.classList.contains('remove')) return;
      if (e.target.closest('.fmt-item')) return;
      if (it.type === 'file') openPathDetail(it.path);
    });
    list.appendChild(div);
  });
  scheduleProbe();
  schedulePreview();
}

async function addPath(raw) {
  const path = (raw || '').trim().replace(/^"|"$/g, '').replace(/^'|'$/g, '');
  if (!path) return;
  if (pending.some(p => p.path === path)) return;

  // 添加时即探测：文件夹立即递归扫描出照片数量和格式分布，供勾选要处理的格式
  try {
    const d = await api(`${BASE}/scan?path=` + encodeURIComponent(path));
    let item;
    if (d.type === 'dir') {
      if (!d.count) {
        $('submit-error').textContent = '文件夹中未找到照片: ' + path;
        return;
      }
      item = { type: 'dir', path, count: d.count,
               formats: d.formats || {}, selected: Object.keys(d.formats || {}) };
    } else {
      item = { type: 'file', path };
    }
    pending.push(item);
    $('path-input').value = '';
    $('submit-error').textContent = '';
    renderPending();
  } catch (e) {
    $('submit-error').textContent = e.message;
  }
}

/* ---------------- form helpers ---------------- */
function collectParams() {
  return {
    preset: selectedPreset,
    quality: $('quality').value,
    lossless: $('lossless').checked ? '1' : '0',
    speed: $('speed').value,
    resize: $('resize').value,
    resize_px: $('resize-px').value,
    keep_exif: $('keep-exif').checked ? '1' : '0',
    del_source: $('del-source').checked ? '1' : '0',
  };
}

async function submitTasks() {
  const errEl = $('submit-error');
  errEl.textContent = '';
  if (!pending.length) {
    errEl.textContent = '请先添加照片文件或路径。';
    return;
  }
  const params = collectParams();
  if (params.del_source === '1' &&
      !confirm('压缩成功后将删除原图，此操作不可恢复。\n（失败或取消的任务不会删除原图）\n\n确定继续吗？')) {
    return;
  }
  const fd = new FormData();
  for (const k of Object.keys(params)) fd.append(k, params[k]);
  // formats 与 path 一一对应（文件为空串），后端据此过滤文件夹里的输入格式
  for (const p of pending) {
    if (p.type === 'dir') {
      if (!p.selected.filter(f => p.formats[f]).length) {
        errEl.textContent = `「${p.path.split(/[\\/]/).pop()}」未勾选任何要处理的格式。`;
        return;
      }
      fd.append('path', p.path);
      fd.append('formats', p.selected.filter(f => p.formats[f]).join(','));
    } else {
      fd.append('path', p.path);
      fd.append('formats', '');
    }
  }
  $('btn-start').disabled = true;
  try {
    await api(`${BASE}/tasks`, { method: 'POST', body: fd });
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

/* ---------------- preview ---------------- */
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
    $('op-preview').textContent = d.summary;
    $('op-preview').title = d.output;
    $('out-preview').textContent = '📄 输出: ' + d.output;
  } catch (e) {
    $('op-preview').textContent = '预览失败: ' + e.message;
    $('out-preview').textContent = '';
  }
}

/* ---------------- image info panel ---------------- */
let probeTimer = null;

// 只对图片文件做信息探测；文件夹路径不探测
const IMAGE_FILE_RE = /\.(jpe?g|jfif|png|webp|bmp|tiff?|avif|heic|heif)$/i;

function infoRows(info) {
  const ori = info.orientation && info.orientation !== 1
    ? `（方向：${ORIENTATION_LABEL[info.orientation] || info.orientation}）` : '';
  return [
    ['文件名', info.name],
    ['格式', info.format],
    ['尺寸', info.width && info.height ? `${info.width}×${info.height}` : ''],
    ['大小', formatBytes(info.size_bytes)],
    ['色彩模式', info.mode],
    ['EXIF', info.has_exif ? '有' + ori : '无'],
    ['ICC 配置', info.has_icc ? '有' : '无'],
    ['动图', info.animated ? '是（仅取第一帧）' : ''],
  ].filter(r => r[1]);
}

function infoRowsHtml(info) {
  return infoRows(info).map(r =>
    `<div class="v-row"><span>${r[0]}</span><b>${escapeHtml(String(r[1]))}</b></div>`
  ).join('');
}

function renderImageInfo(info) {
  const panel = $('vinfo-panel');
  panel.classList.toggle('hidden', !info);
  if (!info) return;
  $('image-info').innerHTML = infoRowsHtml(info);
}

async function probeFirst() {
  const first = pending[0];
  if (!first || first.type !== 'file' || !IMAGE_FILE_RE.test(first.path)) {
    renderImageInfo(null);
    return;
  }
  const box = $('image-info');
  $('vinfo-panel').classList.remove('hidden');
  box.innerHTML = '<span class="m-loading">正在读取照片信息...</span>';
  try {
    const info = await api(`${BASE}/probe?path=` + encodeURIComponent(first.path));
    renderImageInfo(info);
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

function showDetail(name, source, output, status, delSource) {
  $('modal-name').textContent = name || '';
  $('modal-source').textContent =
    source + (delSource && status === 'completed' ? '（压缩成功后已删除）' : '');
  $('modal-output').textContent = output || '';
  $('modal-output-row').style.display = output ? '' : 'none';
  const info = $('modal-info');
  const hasOut = !!output && status === 'completed';
  $('modal-out-box').style.display = hasOut ? '' : 'none';
  if (hasOut) {
    $('modal-out-info').innerHTML = '<span class="m-loading">正在读取输出照片信息...</span>';
    loadInfoInto($('modal-out-info'), output);
  }

  $('modal').classList.remove('hidden');
  if (delSource && status === 'completed') {
    // 原图已删除，无需探测
    info.innerHTML = '<span class="m-loading">原图已删除</span>';
  } else {
    info.innerHTML = '<span class="m-loading">正在读取照片信息...</span>';
    loadInfoInto(info, source);
  }
}

function closeModal() {
  $('modal').classList.add('hidden');
}

function openTaskDetail(t) {
  showDetail(t.name, t.source, t.output || '', t.status,
             String(t.params?.del_source ?? '0') === '1');
}

function openPathDetail(path) {
  showDetail(path.split(/[\\/]/).pop(), path);
}

/* ---------------- task list ---------------- */
function sizeText(t) {
  if (t.status !== 'completed' || !t.out_size) return '';
  const pct = t.src_size ? Math.max(1, Math.round(t.out_size / t.src_size * 100)) : null;
  let txt = ` · ${formatBytes(t.out_size)}`;
  if (pct !== null) {
    txt += pct <= 100 ? `（压缩到 ${pct}%）` : `（变为 ${pct}%，比原图更大）`;
  }
  return txt;
}

function buildPhotoCard(t, prev) {
  const presetLabel = presetDefaults[t.params.preset]?.label || t.params.preset;
  const barClass = ['running', 'cancelling'].includes(t.status) ? 'running'
    : t.status === 'completed' ? 'completed'
    : t.status === 'failed' ? 'failed' : '';
  const delTxt = (String(t.params?.del_source ?? '0') === '1' && t.status === 'completed')
    ? ' · 已删原图' : '';

  const actions = [];
  if (['queued', 'running'].includes(t.status)) actions.push({ act: 'cancel', label: '取消', cls: 'danger' });
  if (t.status === 'completed') actions.push({ act: 'download', label: '下载' }, { act: 'open', label: '打开所在文件夹' });
  if (['failed', 'cancelled'].includes(t.status)) actions.push({ act: 'retry', label: '重试' });

  const card = buildTaskCard(t, {
    sub: `${presetLabel}${delTxt}${sizeText(t)}`,
    barClass,
    barPct: t.status === 'completed' ? 100 : 0,
    runningText: t.status === 'running' ? '处理中...' : '',
    actions,
  });
  card.id = 'task-' + t.id;

  bindTaskActions(card, t, (btn, act) => {
    commonTaskAction(btn, act, t, BASE, board.refreshTasks);
  });

  card.addEventListener('click', (e) => {
    if (e.target.closest('[data-act]')) return;
    openTaskDetail(t);
  });

  if (t.status === 'completed' && prev !== 'completed') flashCompleted(card);
  return card;
}

const board = createTaskBoard({
  basePath: BASE,
  sigKeys: ['status', 'error', 'output', 'out_size'],
  buildCard: buildPhotoCard,
});

/* ---------------- picker ---------------- */
async function pickAndAdd(kind) {
  const d = await openPicker(kind, 'photo', 'submit-error');
  if (!d) return;
  if (kind === 'dir') {
    if (d.path) addPath(d.path);
  } else {
    (d.paths || []).forEach(p => addPath(p));
  }
}

/* ---------------- events ---------------- */
function init() {
  loadCaps();

  $('btn-pick').addEventListener('click', () => pickAndAdd('files'));
  $('btn-pick-dir').addEventListener('click', () => pickAndAdd('dir'));
  $('btn-add-path').addEventListener('click', () => addPath($('path-input').value));
  $('path-input').addEventListener('keydown', e => { if (e.key === 'Enter') addPath(e.target.value); });

  $('btn-start').addEventListener('click', submitTasks);
  $('btn-clear').addEventListener('click', async () => { await api(`${BASE}/tasks/clear`, { method: 'POST' }); board.refreshTasks(); });
  $('btn-stop').addEventListener('click', async () => {
    try {
      await api(`${BASE}/stop-all`, { method: 'POST' });
    } catch (e) { $('submit-error').textContent = e.message; }
  });

  $('modal-close').addEventListener('click', closeModal);
  $('modal').addEventListener('click', e => { if (e.target === $('modal')) closeModal(); });
  document.addEventListener('keydown', e => { if (e.key === 'Escape') closeModal(); });

  $('quality').addEventListener('input', () => { $('quality-value').textContent = $('quality').value; });
  $('resize').addEventListener('change', () => {
    $('resize-px').classList.toggle('hidden', $('resize').value === 'original');
  });

  // 参数变化时刷新操作预览
  document.querySelector('.params').addEventListener('input', schedulePreview);
  document.querySelector('.params').addEventListener('change', schedulePreview);

  updatePreview();
  board.refreshTasks();
  board.pollLoop();
}

document.addEventListener('DOMContentLoaded', init);
