/* 重复清理页逻辑（API 前缀 /api/dedupe）；$ 等 helpers 来自 common.js */
const BASE = '/api/dedupe';

let result = null;          // /api/dedupe/result 响应
let pollTimer = null;
let lightbox = null;        // { gid, row } 当前预览的组与行号

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
const thumbUrl = (path, size) => `${BASE}/thumb?path=${encodeURIComponent(path)}&size=${size}`;

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
      <label class="du-del" title="勾选 = 删除此文件">
        <input type="checkbox" data-del="${escapeHtml(f.path)}" ${f.suggest_delete ? 'checked' : ''}>
        删除
      </label>
      ${f.is_image && f.w
        ? `<img class="du-thumb" loading="lazy" title="点击查看大图"
               src="${thumbUrl(f.path, 240)}" data-row="${i}" alt="">`
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
      <span class="du-spacer"></span>
      <span class="du-sel" data-sel></span>
      <button class="btn small du-mini" data-gact="sel-all">全选</button>
      <button class="btn small du-mini" data-gact="sel-none">全不选</button>
    </div>
    ${rows}
  `;
  card.addEventListener('change', e => {
    if (e.target.matches('input[data-del]')) updateDeleteBar();
  });
  card.addEventListener('click', e => {
    const gact = e.target.closest('[data-gact]');
    if (gact) {
      card.querySelectorAll('input[data-del]').forEach(cb => {
        cb.checked = gact.dataset.gact === 'sel-all';
      });
      updateDeleteBar();
      return;
    }
    if (e.target.matches('img.du-thumb')) {
      openLightbox(g.id, Number(e.target.dataset.row));
    }
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
  updateGroupSelCount(card);
  return card;
}

function updateGroupSelCount(card) {
  const el = card.querySelector('[data-sel]');
  if (!el) return;
  const boxes = Array.from(card.querySelectorAll('input[data-del]'));
  const n = boxes.filter(cb => cb.checked).length;
  el.textContent = `已选删除 ${n}/${boxes.length}`;
}

/* 勾选了删除的文件路径 + 整组全删的组列表 */
function collectDeletes() {
  const paths = [];
  const fullGroups = [];
  document.querySelectorAll('.du-group').forEach(card => {
    const boxes = Array.from(card.querySelectorAll('input[data-del]'));
    const picked = boxes.filter(cb => cb.checked);
    paths.push(...picked.map(cb => cb.dataset.del));
    if (picked.length === boxes.length && boxes.length > 0) {
      const name = card.querySelector('.du-name');
      fullGroups.push(name ? name.textContent.trim() : card.dataset.gid);
    }
  });
  return { paths, fullGroups };
}

function updateDeleteBar() {
  const { paths } = collectDeletes();
  let bytes = 0;
  if (result && result.ready) {
    const byPath = new Map(result.groups.flatMap(g => g.files.map(f => [f.path, f])));
    bytes = paths.reduce((s, p) => s + ((byPath.get(p) || {}).size || 0), 0);
  }
  document.querySelectorAll('.du-group').forEach(updateGroupSelCount);
  $('du-delete-count').textContent = paths.length
    ? `将删除 ${paths.length} 个文件，释放约 ${fmtSize(bytes)}`
    : '勾选要删除的文件（默认按建议勾好）';
  $('btn-delete').disabled = !paths.length;
}

/* ---------------- 删除（弹确认框，带预览图） ---------------- */
function askDelete() {
  const { paths, fullGroups } = collectDeletes();
  if (!paths.length) return;
  const mode = document.querySelector('input[name="del-mode"]:checked').value;
  const verb = mode === 'recycle' ? '移入回收站' : '永久删除';
  let bytes = 0;
  const byPath = new Map(result.groups.flatMap(g => g.files.map(f => [f.path, f])));
  const items = paths.map(p => ({ p, f: byPath.get(p) || {} }));
  bytes = items.reduce((s, it) => s + (it.f.size || 0), 0);

  $('cd-summary').textContent =
    `将${verb} ${paths.length} 个文件，释放约 ${fmtSize(bytes)}。请核对以下预览图：`;
  const grid = $('cd-grid');
  grid.innerHTML = items.map(it => `
    <div class="cd-item">
      ${(it.f.is_image && it.f.w)
        ? `<img src="${thumbUrl(it.p, 240)}" alt="">`
        : '<div class="cd-file">📄</div>'}
      <div class="cd-name" title="${escapeHtml(it.p)}">${escapeHtml(it.f.name || it.p.split(/[\\/]/).pop())}</div>
      <div class="cd-size">${fmtSize(it.f.size || 0)}</div>
    </div>`).join('');
  const warn = $('cd-warning');
  if (fullGroups.length) {
    warn.textContent = `⚠ 整组全部勾选（将不留任何副本）：${fullGroups.join('、')}`;
    warn.classList.remove('hidden');
  } else {
    warn.textContent = '';
    warn.classList.add('hidden');
  }
  const confirmBtn = $('cd-confirm');
  confirmBtn.textContent = mode === 'recycle'
    ? `🗑 移入回收站（${paths.length} 个）`
    : `🗑 永久删除（${paths.length} 个）`;
  confirmBtn.dataset.mode = mode;
  $('confirm-del').classList.remove('hidden');
}

async function doDelete() {
  const { paths } = collectDeletes();
  const mode = $('cd-confirm').dataset.mode;
  closeConfirm();
  if (!paths.length) return;
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

function closeConfirm() {
  $('confirm-del').classList.add('hidden');
}

/* ---------------- 大图预览（组内左右翻页） ---------------- */
function openLightbox(gid, row) {
  lightbox = { gid, row };
  renderLightbox();
  $('lightbox').classList.remove('hidden');
}

function renderLightbox() {
  if (!lightbox || !result || !result.ready) return;
  const g = result.groups.find(x => x.id === lightbox.gid);
  if (!g) return;
  const f = g.files[lightbox.row];
  $('lb-name').textContent = f.name;
  $('lb-img').src = thumbUrl(f.path, 1200);
  $('lb-meta').textContent =
    `${fmtSize(f.size)}${f.w ? ` · ${f.w}×${f.h}` : ''} · 第 ${lightbox.row + 1}/${g.files.length} 张 · ${f.path}`;
  const cb = findDelCheckbox(g.id, f.path);
  $('lb-del').checked = cb ? cb.checked : false;
  $('lb-prev').disabled = g.files.length < 2;
  $('lb-next').disabled = g.files.length < 2;
}

function moveLightbox(step) {
  if (!lightbox || !result || !result.ready) return;
  const g = result.groups.find(x => x.id === lightbox.gid);
  if (!g || g.files.length < 2) return;
  lightbox.row = (lightbox.row + step + g.files.length) % g.files.length;
  renderLightbox();
}

function closeLightbox() {
  $('lightbox').classList.add('hidden');
  lightbox = null;
}

/* 灯箱里的删除勾选与列表双向同步 */
function syncLightboxCheckbox() {
  if (!lightbox || !result || !result.ready) return;
  const g = result.groups.find(x => x.id === lightbox.gid);
  if (!g) return;
  const f = g.files[lightbox.row];
  const cb = findDelCheckbox(g.id, f.path);
  if (cb) {
    cb.checked = $('lb-del').checked;
    updateDeleteBar();
  }
}

function findDelCheckbox(gid, path) {
  return Array.from(document.querySelectorAll(`.du-group[data-gid="${gid}"] input[data-del]`))
    .find(x => x.dataset.del === path);
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
  $('btn-delete').addEventListener('click', askDelete);
  $('btn-reset-keep').addEventListener('click', () => {
    // 恢复默认建议：勾回所有 suggest_delete 文件（组内第一个未勾选项为建议保留）
    document.querySelectorAll('.du-group').forEach(card => {
      card.querySelectorAll('.du-row').forEach((row, idx) => {
        const cb = row.querySelector('input[data-del]');
        if (cb) cb.checked = idx > 0;   // 后端把建议保留的文件排在每组第一行
      });
    });
    updateDeleteBar();
  });

  $('opt-similar').addEventListener('change', () => {
    $('field-threshold').classList.toggle('hidden', !$('opt-similar').checked);
  });
  $('threshold').addEventListener('input', () => {
    $('threshold-value').textContent = $('threshold').value;
  });

  // 删除确认框
  $('cd-confirm').addEventListener('click', doDelete);
  $('cd-cancel').addEventListener('click', closeConfirm);
  $('cd-close').addEventListener('click', closeConfirm);
  $('confirm-del').addEventListener('click', e => {
    if (e.target === $('confirm-del')) closeConfirm();
  });

  // 灯箱
  $('lb-close').addEventListener('click', closeLightbox);
  $('lightbox').addEventListener('click', e => {
    if (e.target === $('lightbox')) closeLightbox();
  });
  $('lb-prev').addEventListener('click', () => moveLightbox(-1));
  $('lb-next').addEventListener('click', () => moveLightbox(1));
  $('lb-reveal').addEventListener('click', async () => {
    if (!lightbox || !result || !result.ready) return;
    const g = result.groups.find(x => x.id === lightbox.gid);
    if (!g) return;
    try {
      await api(`${BASE}/reveal`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: g.files[lightbox.row].path }),
      });
    } catch (e) { alert(e.message); }
  });
  $('lb-del').addEventListener('change', syncLightboxCheckbox);
  document.addEventListener('keydown', e => {
    if (!$('lightbox').classList.contains('hidden')) {
      if (e.key === 'Escape') closeLightbox();
      else if (e.key === 'ArrowLeft') moveLightbox(-1);
      else if (e.key === 'ArrowRight') moveLightbox(1);
    } else if (!$('confirm-del').classList.contains('hidden') && e.key === 'Escape') {
      closeConfirm();
    }
  });

  loadResult();
}

document.addEventListener('DOMContentLoaded', init);
