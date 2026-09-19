/* 两个工具页共享：请求封装、格式化工具、任务列表（keyed 增量更新 + 轮询） */
const $ = id => document.getElementById(id);

async function api(url, options) {
  const res = await fetch(url, options);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || ('请求失败 ' + res.status));
  return data;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]));
}

function formatBytes(b) {
  if (!b) return '';
  if (b < 1024) return b + ' B';
  if (b < 1048576) return (b / 1024).toFixed(1) + ' KB';
  if (b < 1073741824) return (b / 1048576).toFixed(1) + ' MB';
  return (b / 1073741824).toFixed(2) + ' GB';
}

function fmtDuration(sec) {
  if (!sec) return '';
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = Math.floor(sec % 60);
  const p = (x) => String(x).padStart(2, '0');
  return h > 0 ? `${h}:${p(m)}:${p(s)}` : `${m}:${p(s)}`;
}

/* 任务看板：三栏（排队/进行中/已完成），按任务 id 复用 DOM，签名变化才重建卡片。
   cfg: {
     basePath:  '/api/video' | '/api/photo',
     sigKeys:   参与签名比较的任务字段（变化即重建卡片）
     buildCard: (t, prevStatus) => HTMLElement
     updateLive:(card, t) => void   // 签名未变时的原地更新（如转码进度条），可省略
   } */
function createTaskBoard(cfg) {
  let lastTasks = {};
  let pollTimer = null;
  let hasActive = false;

  const taskSig = t => cfg.sigKeys.map(k => String(t[k] ?? '')).join('|');

  function renderTasks(list) {
    const groups = { queued: [], running: [], done: [] };
    for (const t of list) {
      if (t.status === 'queued') groups.queued.push(t);
      else if (['running', 'cancelling'].includes(t.status)) groups.running.push(t);
      else groups.done.push(t);
    }
    hasActive = groups.queued.length > 0 || groups.running.length > 0;
    renderCol('list-queued', 'count-queued', groups.queued, '暂无排队任务');
    renderCol('list-running', 'count-running', groups.running, '暂无运行中任务');
    renderCol('list-done', 'count-done', groups.done, '暂无完成的任务');
    $('task-summary').textContent = list.length ? `${list.length} 个任务` : '';
    const hasFinished = list.some(t => ['completed', 'failed', 'cancelled'].includes(t.status));
    $('btn-clear').style.display = hasFinished ? '' : 'none';
    $('btn-stop').disabled = !hasActive;
  }

  function renderCol(listId, countId, items, emptyText) {
    const wrap = $(listId);
    $(countId).textContent = items.length;
    const byId = new Map();
    for (const el of Array.from(wrap.children)) {
      if (el.dataset && el.dataset.tid) byId.set(el.dataset.tid, el);
    }
    const emptyEl = wrap.querySelector('.empty');
    if (!items.length) {
      byId.forEach(el => el.remove());
      if (!emptyEl) wrap.innerHTML = `<div class="empty">${emptyText}</div>`;
      return;
    }
    if (emptyEl) emptyEl.remove();
    for (const t of items) {
      const old = byId.get(t.id);
      let card;
      if (old && old.dataset.sig === taskSig(t)) {
        card = old;
        if (cfg.updateLive) cfg.updateLive(card, t);
      } else {
        card = cfg.buildCard(t, lastTasks[t.id]);
        card.dataset.sig = taskSig(t);
        if (old) old.replaceWith(card);
      }
      wrap.appendChild(card);
      byId.delete(t.id);
      lastTasks[t.id] = t.status;
    }
    byId.forEach(el => el.remove());
  }

  async function refreshTasks() {
    try {
      const data = await api(cfg.basePath + '/tasks');
      renderTasks(data.tasks);
    } catch (e) {
      /* transient */
    }
  }

  /* 有活动任务时 1s 轮询，空闲时降到 3s */
  function pollLoop() {
    clearTimeout(pollTimer);
    pollTimer = setTimeout(async () => {
      await refreshTasks();
      pollLoop();
    }, hasActive ? 1000 : 3000);
  }

  return { refreshTasks, pollLoop };
}

/* 新完成的任务卡片高亮一次 */
function flashCompleted(card) {
  card.style.transition = 'box-shadow .6s';
  card.style.boxShadow = '0 0 0 1px var(--green)';
  setTimeout(() => { card.style.boxShadow = ''; }, 1500);
}

/* 任务卡片通用骨架：头部 + 进度条 + 错误 + 操作按钮。
   opts: { sub, barClass, barPct, runningText, doneText, actions: [{act, label, cls}] }
   onAct(btn, act, t) 处理按钮点击；卡片主体点击回调 onOpen(t)。 */
function buildTaskCard(t, opts) {
  const card = document.createElement('div');
  card.className = 'task-card';
  card.dataset.tid = t.id;

  const actions = (opts.actions || []).map(a =>
    `<button class="btn small ${a.cls || ''}" data-act="${a.act}">${a.label}</button>`).join('');

  card.innerHTML = `
    <div class="t-head">
      <div style="min-width:0">
        <div class="t-name">${escapeHtml(t.name)}</div>
        <div class="t-sub">${escapeHtml(opts.sub || '')}</div>
      </div>
      <span class="status ${t.status}">${STATUS_LABEL[t.status] || t.status}</span>
    </div>
    <div class="progress-wrap">
      <div class="progress-bar">
        <div class="progress-fill ${opts.barClass || ''}" style="width:${opts.barPct ?? 0}%"></div>
      </div>
      <div class="progress-label">
        <span class="pl-left">${opts.runningText || ''}</span>
        <span>${escapeHtml(t.output ? t.output.split(/[\\/]/).pop() : '')}</span>
      </div>
    </div>
    ${t.error ? `<div class="t-error">⚠ ${escapeHtml(t.error)}</div>` : ''}
    ${actions ? `<div class="t-actions">${actions}</div>` : ''}
  `;
  return card;
}

function bindTaskActions(card, t, handlers) {
  card.querySelectorAll('[data-act]').forEach(btn => {
    btn.addEventListener('click', () => handlers(btn, btn.dataset.act, t));
  });
}

/* 两个页面共用的取消/重试/下载/打开所在文件夹动作；onRefresh 在重试后刷新列表 */
async function commonTaskAction(btn, act, t, basePath, onRefresh) {
  if (act === 'cancel') {
    await api(`${basePath}/tasks/${t.id}/cancel`, { method: 'POST' });
  } else if (act === 'download') {
    window.location.href = `${basePath}/download/${t.id}`;
  } else if (act === 'open') {
    await api(`${basePath}/open-file/${t.id}`, { method: 'POST' });
  } else if (act === 'retry') {
    btn.disabled = true;
    try {
      await api(`${basePath}/tasks/${t.id}/retry`, { method: 'POST' });
      onRefresh();
    } catch (err) {
      alert('重试失败: ' + err.message);
      btn.disabled = false;
    }
  }
}

async function openPicker(kind, tool, errEl) {
  const endpoint = (kind === 'dir' ? '/api/pick-dir' : '/api/pick') + `?tool=${tool}`;
  try {
    $(errEl).textContent = '';
    return await api(endpoint, { method: 'POST' });
  } catch (e) {
    $(errEl).textContent = e.message;
  }
}
