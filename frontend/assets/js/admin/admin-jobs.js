/**
 * AuraPlayFF Admin — Scheduler / Jobs Monitor
 * SUPER ADMIN ONLY.
 *
 * APIs:
 *   GET  /api/admin/jobs              dashboard (latest per job)
 *   GET  /api/admin/jobs/history      execution history
 *   GET  /api/admin/jobs/registered   triggerable job names
 *   POST /api/admin/jobs/{name}/trigger  manual trigger
 */

import { adminGet, adminPost, isSuper } from './admin-api.js';
import {
  showToast, statusChip, fmtDateTime, fmtRelative,
  escHtml, renderPagination, showTableSkeleton, emptyRow, confirmAction
} from './admin-ui.js';

const JOB_DESCRIPTIONS = {
  tournament_auto_transition: 'Auto-transitions CONFIRMED→LIVE when scheduled time reached & min players met',
  room_release_reminder:      'Notifies agent when tournament room should be released soon',
  daily_performance_refresh:  'Recalculates agent daily performance counts from tournament records',
  month_end_salary_snapshot:  'Generates month-end salary records for all active agents',
  screenshot_cleanup:         'Deletes expired screenshot files from Supabase storage',
  leaderboard_refresh:        'Pre-computes and caches leaderboard datasets',
  active_user_metrics:        'Computes and caches active user counts (online/daily/24h)',
};

// ── Job dashboard ─────────────────────────────────────────────────────────────
export async function loadJobDashboard() {
  if (!isSuper()) {
    document.getElementById('jobs-grid').innerHTML =
      '<div class="alert alert-warning">🔒 Scheduler access is restricted to Super Admin.</div>';
    return;
  }
  const grid = document.getElementById('jobs-grid');
  if (grid) grid.innerHTML = Array.from({ length: 7 }, () =>
    `<div class="card"><div class="skeleton" style="height:14px;width:60%;margin-bottom:8px;border-radius:4px"></div><div class="skeleton" style="height:24px;width:40%;border-radius:4px"></div></div>`
  ).join('');

  try {
    const jobs = await adminGet('/api/admin/jobs');
    _renderJobCards(jobs);
  } catch (err) {
    if (grid) grid.innerHTML = `<div class="alert alert-error">⚠ ${escHtml(err.displayMessage || 'Failed to load jobs')}</div>`;
  }
}

function _renderJobCards(jobs) {
  const grid = document.getElementById('jobs-grid');
  if (!grid) return;
  if (!jobs.length) {
    grid.innerHTML = '<p style="color:var(--color-text-muted)">No jobs registered.</p>';
    return;
  }

  grid.innerHTML = jobs.map(j => {
    const statusClass = j.last_status === 'success' ? 'success' : j.last_status === 'failed' ? 'danger' : j.last_status === 'running' ? 'blue' : '';
    const desc = JOB_DESCRIPTIONS[j.job_name] || '';
    return `<div class="card ${statusClass ? 'stat-card ' + statusClass : 'stat-card'}">
      <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:8px;margin-bottom:8px">
        <div style="font-weight:700;font-size:13px;color:var(--color-text)">${escHtml(j.job_name)}</div>
        ${j.last_status ? statusChip(j.last_status) : '<span class="chip chip-skipped">Never run</span>'}
      </div>
      ${desc ? `<div style="font-size:11px;color:var(--color-text-muted);margin-bottom:8px">${escHtml(desc)}</div>` : ''}
      <div style="font-size:11px;color:var(--color-text-muted)">
        Last run: ${j.last_run_at ? fmtRelative(j.last_run_at) : '—'}
        ${j.last_duration_ms ? ` · ${j.last_duration_ms}ms` : ''}
        ${j.last_records_affected != null ? ` · ${j.last_records_affected} records` : ''}
      </div>
      ${j.last_error_summary ? `<div style="font-size:11px;color:var(--color-danger);margin-top:4px;word-break:break-word">${escHtml(j.last_error_summary)}</div>` : ''}
      ${j.is_registered ? `<button class="btn btn-ghost btn-sm" style="margin-top:10px" data-trigger="${escHtml(j.job_name)}">▶ Trigger Now</button>` : ''}
    </div>`;
  }).join('');

  grid.querySelectorAll('[data-trigger]').forEach(btn => {
    btn.addEventListener('click', () => triggerJob(btn.dataset.trigger));
  });
}

export async function triggerJob(name) {
  if (!confirmAction(`Manually trigger job "${name}"?`)) return;
  try {
    await adminPost(`/api/admin/jobs/${encodeURIComponent(name)}/trigger`, {});
    showToast(`Job "${name}" triggered. Check history in a moment.`, 'success');
    setTimeout(loadJobHistory, 2000);
  } catch (err) { showToast(err.displayMessage || 'Trigger failed', 'error'); }
}

// ── Job history ───────────────────────────────────────────────────────────────
let _hOffset = 0;
const _hLimit = 30;
let _hJobFilter  = '';
let _hStatusFilter = '';

export async function loadJobHistory() {
  if (!isSuper()) return;
  showTableSkeleton('history-tbody', 6, 8);
  try {
    const params = { limit: _hLimit, offset: _hOffset };
    if (_hJobFilter)    params.job_name = _hJobFilter;
    if (_hStatusFilter) params.status   = _hStatusFilter;
    const data = await adminGet('/api/admin/jobs/history', params);
    _renderHistoryTable(data.items || [], data.total || 0);
  } catch (err) {
    document.getElementById('history-tbody').innerHTML =
      emptyRow(6, '⚠ ' + (err.displayMessage || 'Load failed'));
  }
}

function _renderHistoryTable(items, total) {
  const tbody = document.getElementById('history-tbody');
  if (!tbody) return;
  if (!items.length) {
    tbody.innerHTML = emptyRow(6, 'No execution history yet.');
    renderPagination('history-pagination', 0, _hLimit, 0, () => {});
    return;
  }
  tbody.innerHTML = items.map(h => `
    <tr>
      <td style="font-size:12px;white-space:nowrap;color:var(--color-text-muted)">${fmtDateTime(h.started_at)}</td>
      <td style="font-weight:600">${escHtml(h.job_name)}</td>
      <td>${statusChip(h.status)}</td>
      <td style="text-align:right;font-variant-numeric:tabular-nums">${h.duration_ms != null ? h.duration_ms + 'ms' : '—'}</td>
      <td style="text-align:right">${h.records_affected ?? '—'}</td>
      <td style="font-size:11px;color:var(--color-danger);max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${h.error_summary ? escHtml(h.error_summary) : '—'}</td>
    </tr>`).join('');

  renderPagination('history-pagination', total, _hLimit, _hOffset, off => {
    _hOffset = off; loadJobHistory();
  });
}

export function setupJobHistoryFilters() {
  document.getElementById('filter-job-name')?.addEventListener('change', e => {
    _hJobFilter = e.target.value; _hOffset = 0; loadJobHistory();
  });
  document.getElementById('filter-job-status')?.addEventListener('change', e => {
    _hStatusFilter = e.target.value; _hOffset = 0; loadJobHistory();
  });
  document.getElementById('btn-history-reset')?.addEventListener('click', () => {
    _hJobFilter = ''; _hStatusFilter = ''; _hOffset = 0;
    const n = document.getElementById('filter-job-name');
    const s = document.getElementById('filter-job-status');
    if (n) n.value = ''; if (s) s.value = '';
    loadJobHistory();
  });
}
