/* 重复清理页逻辑（API 前缀 /api/dedupe）；$ 等 helpers 来自 common.js */
const BASE = '/api/dedupe';

let result = null;          // /api/dedupe/result 响应
let pollTimer = null;

/* ---------------- 扫描 ---------------- */
async function startScan() {
  const errEl = $('submit-error');
  errEl.textContent = '';
  const path = $('path-input').value.trim();
  if (!path) {
    errEl.textContent = '请先选择或粘贴要扫描的文件夹。';
    return;
  }
  if (!$('opt-exact').checked && !$('opt-similar').checked) {
    errEl.textContent = '请至少选择一种查重方式。';
    return;
  }
  const fd = new FormData();
  fd.append('path', path);
  fd.append('exact', $('opt-exact').checked ? '1' : '0');
  fd.append('similar', $('opt-similar').checked ? '1' : '0');
  fd.append('threshold', $('threshold').value);
  fd.append('min_size_kb', $('min-size-kb').value || '0');
  $('btn-start').disabled = true;
  try {
    await api(`${BASE}/tasks`, { method: 'POST', body: fd });
    errEl.textContent = '';
    $('du-groups').innerHTML = '<div class="empty">扫描中...</div>';
    $('du-summary').textContent = '尚未扫描';
    $('du-actions').classList.add('hidden');
    pollScan();
  } catch (e) {
    errEl.textContent = e.message;
  } finally {
    $('btn-start').disabled = false;
  }
}

/* 轮询扫描任务状态；完成/失败后停止并拉取结果 */
function pollScan() {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(async () => {
    let tasks = [];
    try {
      tasks = (await api(`${BASE}/tasks`)).tasks || [];
    } catch (e) { /* transient */ }
    const t = tasks[0];
    const active = t && ['queued', 'running', 'cancelling'].includes(t.status);
    $('scan-panel').classList.toggle('hidden', !t);
    $('btn-stop').disabled = !active;
    if (active) {
      const phase = t.phase || (t.status === 'queued' ? '排队中' : '处理中');
      $('scan-text').textContent =
        t.status === 'cancelling' ? '正在取消...' : phase;
      $('scan-count').textContent =
        t.total ? `${t.scanned ?? 0} / ${t.total}` : (t.scanned ? `${t.scanned} 个文件` : '');
      $('scan-fill').style.width = (t.progress || 0) + '%';
      pollScan();
      return;
    }
    if (t && t.status === 'failed') {
      $('scan-text').textContent = '扫描失败';
      $('scan-count').textContent = t.error || '';
      $('scan-fill').style.width = '0%';
      return;
    }
    if (t && t.status === 'cancelled') {
      $('scan-text').textContent = '扫描已取消';
      $('scan-fill').style.width = '0%';
      $('du-groups').innerHTML = '<div class="empty">扫描已取消</div>';
      return;
    }
    if (t) $('scan-fill').style.width = '100%';
    loadResult();
  }, 700);
}

/* ---------------- 结果 ---------------- */
async function loadResult() {
  try {
    result = await api(`${BASE}/result`);
    renderResult();
  } catch (e) {
    $('du-groups').innerHTML = `<div class="empty">读取结果失败: ${escapeHtml(e.message)}</div>`;
  }
}

const fmtSize = b => formatBytes(b) || '0 B';

function renderResult() {
  const groups = (result && result.ready) ? result.groups : [];
  const keepBtn = $('btn-reset-keep');
  if (!result || !result.ready) {
    $('du-summary').textContent = '尚未扫描';
    $('du-actions').classList.add('hidden');
    keepBtn.style.display = 'none';
    if (!$('du-groups').querySelector('.du-group'))
      $('du-groups').innerHTML = '<div class="empty">没有扫描结果</div>';
    updateDeleteBar();
    return;
  }
  const st = result.stats;
  $('du-summary').textContent =
    `扫描 ${st.files} 个文件 · ${st.groups} 组（完全重复 ${st.exact_groups} · 相似照片 ${st.similar_groups}）` +
    (st.skipped_images ? ` · 无法读取 ${st.skipped_images} 张` : '') +
    ` · 可释放 ${fmtSize(st.wasted_bytes)}`;
  keepBtn.style.display = groups.length ? '' : 'none';

  const wrap = $('du-groups');
  wrap.innerHTML = '';
  if (!groups.length) {
    wrap.innerHTML = '<div class="empty">🎉 没有发现重复文件</div>';
  }
  for (const g of groups) {
    wrap.appendChild(groupCard(g));
  }
  $('du-actions').classList.toggle('hidden', !groups.length);
  updateDeleteBar();
}

function groupCard(g) {
  const card = document.createElement('div');
  card.className = 'du-group';
  card.dataset.gid = g.id;
  const badge = g.kind === 'exact'
    ? '<span class="du-badge exact">完全相同</span>'
    : '<span class="du-badge similar">相似</span>';
  const rows = g.files.map((f, i) => `
    <div class="du-row" data-path="${escapeHtml(f.path)}">
      <label class="du-keep" title="保留此文件，删除组内其他文件">
        <input type="radio" name="keep-${g.id}" data-keep="${escapeHtml(f.path)}"
               ${f.suggest_delete ? '' : 'checked'}>
        保留
      </label>
      ${f.is_image && f.w
        ? `<img class="du-thumb" loading="lazy" src="${BASE}/thumb?path=${encodeURIComponent(f.path)}" alt="">`
        : '<span class="du-thumb du-thumb-file">📄</span>'}
      <div class="du-info">
        <div class="du-name" title="${escapeHtml(f.path)}">${escapeHtml(f.name)}
          ${f.suggest_delete ? '' : '<span class="du-keep-tag">建议保留</span>'}
        </div>
        <div class="du-path" title="${escapeHtml(f.path)}">${escapeHtml(f.path)}</div>
      </div>
      <div class="du-meta">
        ${fmtSize(f.size)}${f.w ? ` · ${f.w}×${f.h}` : ''}
        ${f.suggest_delete && g.kind === 'exact' ? `<div class="du-waste">重复 ${fmtSize(f.size)}</div>` : ''}
      </div>
      <button class="btn small" data-act="reveal" data-path="${escapeHtml(f.path)}">定位</button>
    </div>`).join('');
  card.innerHTML = `
    <div class="du-group-head">
      ${badge}
      <span>${g.files.length} 个文件 · 可释放 ${fmtSize(g.wasted)}</span>
    </div>
    ${rows}
  `;
  card.addEventListener('change', e => {
    if (e.target.matches('input[data-keep]')) updateDeleteBar();
  });
  card.querySelectorAll('[data-act="reveal"]').forEach(btn => {
    btn.addEventListener('click', async () => {
      try {
        await api(`${BASE}/reveal`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ path: btn.dataset.path }),
        });
      } catch (e) {
        alert(e.message);
      }
    });
  });
  return card;
}

/* 统计“将删除”的文件：每组除保留项以外的全部 */
function collectDeletes() {
  const paths = [];
  document.querySelectorAll('.du-group').forEach(card => {
    const keep = card.querySelector('input[data-keep]:checked');
    if (!keep) return;
    card.querySelectorAll('.du-row').forEach(row => {
      if (row.dataset.path !== keep.dataset.keep) paths.push(row.dataset.path);
    });
  });
  return paths;
}

function updateDeleteBar() {
  const paths = collectDeletes();
  let bytes = 0;
  if (result && result.ready) {
    const byPath = new Map(result.groups.flatMap(g => g.files.map(f => [f.path, f])));
    bytes = paths.reduce((s, p) => s + ((byPath.get(p) || {}).size || 0), 0);
  }
  $('du-delete-count').textContent = paths.length
    ? `将删除 ${paths.length} 个文件，释放约 ${fmtSize(bytes)}`
    : '每组勾选一个要保留的文件';
  $('btn-delete').disabled = !paths.length;
}

/* ---------------- 删除 ---------------- */
async function deleteSelected() {
  const paths = collectDeletes();
  if (!paths.length) return;
  const mode = document.querySelector('input[name="del-mode"]:checked').value;
  const verb = mode === 'recycle' ? '移入回收站' : '永久删除';
  let bytes = 0;
  if (result && result.ready) {
    const byPath = new Map(result.groups.flatMap(g => g.files.map(f => [f.path, f])));
    bytes = paths.reduce((s, p) => s + ((byPath.get(p) || {}).size || 0), 0);
  }
  if (!confirm(`确定要${verb} ${paths.length} 个文件（约 ${fmtSize(bytes)}）吗？\n\n建议先抽查几个“定位”确认无误。`)) {
    return;
  }
  $('btn-delete').disabled = true;
  try {
    const data = await api(`${BASE}/delete`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ paths, mode }),
    });
    const failed = data.results.filter(r => !r.ok);
    if (failed.length) {
      alert(`${paths.length - failed.length} 个删除成功，${failed.length} 个失败：\n` +
            failed.slice(0, 5).map(r => `${r.path}: ${r.error}`).join('\n'));
    }
    result = data.result;
    renderResult();
  } catch (e) {
    alert('删除失败: ' + e.message);
  } finally {
    $('btn-delete').disabled = false;
    updateDeleteBar();
  }
}

/* ---------------- 事件 ---------------- */
function init() {
  $('btn-pick-dir').addEventListener('click', async () => {
    const d = await openPicker('dir', 'dedupe', 'submit-error');
    if (d && d.path) $('path-input').value = d.path;
  });
  $('btn-add-path').addEventListener('click', () => {
    $('path-input').value = $('path-input').value.trim().replace(/^"|"$/g, '');
  });
  $('path-input').addEventListener('keydown', e => {
    if (e.key === 'Enter') $('path-input').value = e.target.value.trim().replace(/^"|"$/g, '');
  });

  $('btn-start').addEventListener('click', startScan);
  $('btn-stop').addEventListener('click', async () => {
    try {
      await api(`${BASE}/stop-all`, { method: 'POST' });
    } catch (e) { $('submit-error').textContent = e.message; }
  });
  $('btn-delete').addEventListener('click', deleteSelected);
  $('btn-reset-keep').addEventListener('click', () => {
    // 后端把“建议保留”的文件排在每组第一行，勾回每组第一项即恢复默认建议
    document.querySelectorAll('.du-group').forEach(card => {
      const first = card.querySelector('input[data-keep]');
      if (first) first.checked = true;
    });
    updateDeleteBar();
  });

  $('opt-similar').addEventListener('change', () => {
    $('field-threshold').classList.toggle('hidden', !$('opt-similar').checked);
  });
  $('threshold').addEventListener('input', () => {
    $('threshold-value').textContent = $('threshold').value;
  });

  loadResult();
}

document.addEventListener('DOMContentLoaded', init);
