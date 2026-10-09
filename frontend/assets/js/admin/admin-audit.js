/**
 * AuraPlayFF Admin — Audit Logs
 * SUPER ADMIN ONLY.
 *
 * API: GET /api/admin/audit-logs
 */

import { adminGet, isSuper } from './admin-api.js';
import {
  showToast, fmtDateTime, escHtml,
  renderPagination, showTableSkeleton, emptyRow
} from './admin-ui.js';

let _offset = 0;
const _limit = 50;
let _filters = { action: '', actor_id: '', entity_type: '', entity_id: '' };

export async function loadAuditLogs() {
  if (!isSuper()) {
    document.getElementById('audit-tbody').innerHTML =
      emptyRow(6, '🔒 Audit log access is restricted to Super Admin.');
    return;
  }
  showTableSkeleton('audit-tbody', 6, 10);
  try {
    const params = { limit: _limit, offset: _offset };
    if (_filters.action)      params.action      = _filters.action;
    if (_filters.actor_id)    params.actor_id    = _filters.actor_id;
    if (_filters.entity_type) params.entity_type = _filters.entity_type;
    if (_filters.entity_id)   params.entity_id   = _filters.entity_id;

    const data = await adminGet('/api/admin/audit-logs', params);
    _renderTable(data.items || [], data.total || 0);
  } catch (err) {
    document.getElementById('audit-tbody').innerHTML =
      emptyRow(6, '⚠ ' + (err.displayMessage || 'Load failed'));
  }
}

function _renderTable(items, total) {
  const tbody = document.getElementById('audit-tbody');
  if (!tbody) return;

  if (!items.length) {
    tbody.innerHTML = emptyRow(6, 'No audit entries found.');
    renderPagination('audit-pagination', 0, _limit, 0, () => {});
    return;
  }

  tbody.innerHTML = items.map(log => {
    const metaSnippet = _metaSnippet(log.new_value || log.old_value);
    return `<tr>
      <td style="font-size:12px;color:var(--color-text-muted);white-space:nowrap">${fmtDateTime(log.created_at)}</td>
      <td>
        <span class="chip chip-${escHtml(log.actor_type)}">${escHtml(log.actor_type)}</span>
        <div style="font-size:11px;color:var(--color-text-muted);margin-top:2px;font-family:var(--font-mono)">${escHtml(_shortId(log.actor_id))}</div>
      </td>
      <td style="font-weight:600;color:var(--color-blue)">${escHtml(log.action)}</td>
      <td>
        <span style="font-size:12px">${escHtml(log.entity_type)}</span>
        <div style="font-size:11px;color:var(--color-text-muted);font-family:var(--font-mono)">${escHtml(_shortId(log.entity_id))}</div>
      </td>
      <td style="font-size:11px;color:var(--color-text-muted);max-width:200px;overflow:hidden;text-overflow:ellipsis">${escHtml(metaSnippet)}</td>
      <td style="font-size:11px;color:var(--color-text-dim)">${log.ip_address ? escHtml(log.ip_address) : '—'}</td>
    </tr>`;
  }).join('');

  renderPagination('audit-pagination', total, _limit, _offset, off => {
    _offset = off;
    loadAuditLogs();
  });
}

export function setupAuditFilters() {
  document.getElementById('audit-filter-action')?.addEventListener('input', e => {
    _filters.action = e.target.value.trim(); _offset = 0;
  });
  document.getElementById('audit-filter-actor')?.addEventListener('input', e => {
    _filters.actor_id = e.target.value.trim(); _offset = 0;
  });
  document.getElementById('audit-filter-entity')?.addEventListener('change', e => {
    _filters.entity_type = e.target.value; _offset = 0;
  });

  // Debounced search
  let timer;
  ['audit-filter-action', 'audit-filter-actor'].forEach(id => {
    document.getElementById(id)?.addEventListener('input', () => {
      clearTimeout(timer);
      timer = setTimeout(loadAuditLogs, 500);
    });
  });
  document.getElementById('audit-filter-entity')?.addEventListener('change', loadAuditLogs);

  document.getElementById('btn-audit-reset')?.addEventListener('click', () => {
    _filters = { action: '', actor_id: '', entity_type: '', entity_id: '' };
    _offset  = 0;
    ['audit-filter-action', 'audit-filter-actor'].forEach(id => {
      const el = document.getElementById(id);
      if (el) el.value = '';
    });
    const sel = document.getElementById('audit-filter-entity');
    if (sel) sel.value = '';
    loadAuditLogs();
  });
}

// ── Helpers ───────────────────────────────────────────────────────────────────
function _shortId(id) {
  if (!id) return '—';
  // If it looks like a UUID, show first 8 chars
  if (id.length > 12 && id.includes('-')) return id.slice(0, 8) + '…';
  return id;
}

function _metaSnippet(obj) {
  if (!obj) return '';
  try {
    const str = JSON.stringify(obj);
    return str.length > 120 ? str.slice(0, 120) + '…' : str;
  } catch { return ''; }
}
