'use strict';

const SAMPLE = [
  {index: 0, type: 'const', value: 1},          // condition source
  {index: 1, type: 'const', value: 7},
  {index: 2, type: 'const', value: 3},
  {index: 3, type: 'formula', raw: 'if(c0, c1 + c2, c1 - c2)'},
  {index: 4, type: 'formula', raw: 'c3 * 2'},
  {index: 5, type: 'formula', raw: 'if(c0, c6, c2)'},
  {index: 6, type: 'formula', raw: 'c1 + 100'},
  {index: 7, type: 'formula', raw: 'c4 + c5'},
  {index: 8, type: 'const', value: 0},
  {index: 9, type: 'formula', raw: 'if(c0, c10, 9)'},
  {index: 10, type: 'formula', raw: 'c1 + c8'},
];

let TABLE = null;
let SELECTED = 0;

const $ = (id) => document.getElementById(id);

function cs(n) { return `c${n}`; }
function chips(list, cls) {
  if (!list || !list.length) return '<span class="hint">无</span>';
  return '<div class="chips">' +
    list.map((x) => `<span class="chip ${cls}">${cs(x)}</span>`).join('') +
    '</div>';
}

function renderTable() {
  if (!TABLE) return;
  $('gen').textContent = TABLE.generation;
  const nonEmpty = TABLE.cells.filter((c) => c.type !== 'empty');
  const grid = $('grid');
  grid.innerHTML = '';
  nonEmpty.forEach((cell) => {
    const div = document.createElement('div');
    div.className = `cell ${cell.type}${cell.idx === SELECTED ? ' sel' : ''}`;
    div.onclick = () => { SELECTED = cell.idx; renderTable(); syncForm(); };
    const deps = cell.deps.map(cs).join(', ');
    div.innerHTML =
      `<span class="g">g${cell.generation}</span>
       <div class="nm">${cs(cell.index)}</div>
       <div class="vl" title="${(cell.raw || '').replace(/"/g, '&quot;')}">${
         cell.type === 'const' ? cell.value : escapeHtml(cell.raw)}</div>
       <div class="vl" style="color:var(--muted)">
         ${cell.value === null ? '—' : '= ' + cell.value}
         ${deps ? `· → ${deps}` : ''}
       </div>`;
    grid.appendChild(div);
  });
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"]/g, (ch) => (
    {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[ch]));
}

function syncForm() {
  $('idx').value = SELECTED;
  const cell = TABLE.cells[SELECTED];
  if (!cell) return;
  if (cell.type === 'const') {
    $('kind').value = 'const';
    $('raw').value = String(cell.value);
  } else if (cell.type === 'formula') {
    $('kind').value = 'formula';
    $('raw').value = cell.raw || '';
  }
}

function renderVerdict(r) {
  const box = $('verdict');
  box.className = `verdict ${r.ok ? 'ok' : 'fail'}`;
  if (!r.ok) {
    const f = r.failure;
    box.innerHTML =
      `<div class="banner">✗ 候选失败（${reasonLabel(f.reason)}）— 已回滚，表未改动</div>
       <label>候选格 → 首个失败点的稳定链路</label>
       <div class="chain">${f.chain_labels.join(' → ')}</div>
       <div class="hint">${escapeHtml(f.detail)}</div>`;
    return;
  }
  const a = r.affected;
  box.innerHTML =
    `<div class="banner">✓ 原子发布为新代次 g${a.generation}</div>
     <div class="section"><label>共同发布（重算）</label>${chips(a.recomputed, '')}</div>
     <div class="section"><label>缓存命中（未改格）</label>${chips(a.cache_hits, '')}</div>
     <div class="section"><label>新增依赖</label>${
       renderDeps(a.deps_added, 'add')}</div>
     <div class="section"><label>移除依赖</label>${
       renderDeps(a.deps_removed, 'rem')}</div>
     <div class="section"><label>受影响格活动依赖（分支实际进入）</label>${
       renderActive(a.active_deps)}</div>`;
}

function reasonLabel(r) {
  return {unknown:'未知引用', overflow:'64 位溢出',
          cycle:'活动环路', divzero:'除零'}[r] || r;
}

function renderDeps(map, cls) {
  const keys = Object.keys(map);
  if (!keys.length) return '<span class="hint">无</span>';
  return keys.map((k) =>
    `<div class="hint">${cs(+k)}：${map[k].map(cs).join(', ')}</div>`).join('');
}

function renderActive(map) {
  const keys = Object.keys(map);
  if (!keys.length) return '<span class="hint">无</span>';
  return keys.map((k) =>
    `<div class="hint">${cs(+k)} → ${
      map[k].length ? map[k].map(cs).join(', ') : '（常量/无依赖）'}</div>`
  ).join('');
}

async function api(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body || {}),
  });
  const data = await res.json();
  if (!res.ok && (res.status !== 422)) throw new Error(data.error || 'request failed');
  return data;
}

async function refresh() {
  TABLE = await api('GET', '/api/table');
  renderTable();
}

$('submit').onclick = async () => {
  const idx = parseInt($('idx').value, 10);
  const kind = $('kind').value;
  const text = $('raw').value;
  const body = {index: idx, type: kind};
  if (kind === 'const') body.value = text;
  else body.raw = text;
  try {
    const verdict = await api('POST', '/api/candidates', body);
    renderVerdict(verdict);
    await refresh();
  } catch (e) {
    $('verdict').className = 'verdict fail';
    $('verdict').innerHTML =
      `<div class="banner">✗ ${escapeHtml(e.message)}</div>`;
  }
};

$('kind').onchange = () => {
  $('rawLabel').textContent = $('kind').value === 'const' ? '整数值' : '公式文本';
};

$('loadSample').onclick = async () => {
  await api('POST', '/api/table', {cells: SAMPLE});
  await refresh();
  syncForm();
};

refresh().catch((e) => {
  $('grid').textContent = '加载失败：' + e.message;
});
