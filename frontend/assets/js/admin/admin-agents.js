/**
 * AuraPlayFF Admin — Agent Management & Performance
 * SUPER ADMIN ONLY for create/manage. Any staff for viewing.
 *
 * APIs used (management):
 *   GET  /api/admin/agents                              list
 *   POST /api/admin/agents                              create
 *   GET  /api/admin/agents/{id}                         detail
 *   PATCH /api/admin/agents/{id}                        update
 *   POST /api/admin/agents/{id}/activate
 *   POST /api/admin/agents/{id}/deactivate
 *   POST /api/admin/agents/{id}/reset-password
 *   GET  /api/admin/targets                             tier config
 *
 * APIs used (performance — agent-details.html):
 *   GET  /api/admin/agents/{id}/performance/today
 *   GET  /api/admin/agents/{id}/performance/month/{y}/{m}
 *   GET  /api/admin/agents/{id}/performance/daily
 *   GET  /api/admin/agents/{id}/salary
 *   GET  /api/admin/agents/{id}/salary/{y}/{m}
 *   GET  /api/admin/agents/{id}/graces
 *   GET  /api/admin/agents/{id}/warnings
 *   POST /api/admin/agents/{id}/grace
 *   POST /api/admin/agents/{id}/warning
 *   POST /api/admin/agents/{id}/excuse-day
 *   POST /api/admin/agents/{id}/salary/adjust
 *   POST /api/admin/agents/{id}/salary/confirm
 *
 * APIs used (permissions — Super Admin only):
 *   GET    /api/admin/agents/{id}/permissions           list current grants
 *   POST   /api/admin/agents/{id}/permissions           grant a permission
 *   DELETE /api/admin/agents/{id}/permissions/{code}    revoke a permission
 */

import { adminGet, adminPost, adminPatch, adminDelete, isSuper } from './admin-api.js';
import {
  showToast, statusChip, fmtDateTime, fmtRelative,
  escHtml, renderPagination, showTableSkeleton, emptyRow, confirmAction, openModal, closeModal
} from './admin-ui.js';

// ─────────────────────────────────────────────────────────────────────────────
// AGENT LIST
// ─────────────────────────────────────────────────────────────────────────────
let _offset = 0;
const _limit = 25;

export async function loadAgentList() {
  showTableSkeleton('agents-tbody', 6, 6);
  try {
    const data = await adminGet('/api/admin/agents', { limit: _limit, offset: _offset });
    _renderAgentTable(data.items || data || [], data.total || (data.items || data || []).length);
  } catch (err) {
    document.getElementById('agents-tbody').innerHTML = emptyRow(6, '⚠ ' + (err.displayMessage || 'Load failed'));
  }
}

function _renderAgentTable(items, total) {
  const tbody = document.getElementById('agents-tbody');
  if (!tbody) return;
  if (!items.length) {
    tbody.innerHTML = emptyRow(6, 'No agents found. Create the first agent.');
    return;
  }
  tbody.innerHTML = items.map(a => `
    <tr>
      <td>
        <a href="/admin/agent-details.html?id=${encodeURIComponent(a.id || a.agent_db_id)}" style="color:var(--color-blue);font-weight:600">${escHtml(a.agent_id)}</a>
      </td>
      <td>${escHtml(a.display_name)}</td>
      <td>${statusChip(a.role || 'agent')}</td>
      <td>${a.is_active ? '<span class="chip chip-active">Active</span>' : '<span class="chip chip-inactive">Inactive</span>'}</td>
      <td style="font-size:12px;color:var(--color-text-muted)">${fmtDateTime(a.created_at)}</td>
      <td class="td-actions admin-only">
        <a href="/admin/agent-details.html?id=${encodeURIComponent(a.id || a.agent_db_id)}" class="btn btn-ghost btn-sm">Details</a>
        ${a.is_active
          ? `<button class="btn btn-warning btn-sm" style="margin-left:4px" data-action="deactivate" data-id="${escHtml(a.id||a.agent_db_id)}" data-name="${escHtml(a.agent_id)}">Deactivate</button>`
          : `<button class="btn btn-success btn-sm"  style="margin-left:4px" data-action="activate"   data-id="${escHtml(a.id||a.agent_db_id)}" data-name="${escHtml(a.agent_id)}">Activate</button>`}
      </td>
    </tr>`).join('');

  tbody.querySelectorAll('[data-action]').forEach(btn => {
    btn.addEventListener('click', () => {
      const { action, id, name } = btn.dataset;
      if (action === 'activate')   _toggleAgent(id, name, true);
      if (action === 'deactivate') _toggleAgent(id, name, false);
    });
  });

  renderPagination('agents-pagination', total, _limit, _offset, off => {
    _offset = off; loadAgentList();
  });
}

async function _toggleAgent(id, name, activate) {
  if (!confirmAction(`${activate ? 'Activate' : 'Deactivate'} agent ${name}?`)) return;
  try {
    await adminPost(`/api/admin/agents/${id}/${activate ? 'activate' : 'deactivate'}`, {});
    showToast(`Agent ${activate ? 'activated' : 'deactivated'}`, activate ? 'success' : 'warning');
    loadAgentList();
  } catch (err) { showToast(err.displayMessage || 'Action failed', 'error'); }
}

// ── Create agent ──────────────────────────────────────────────────────────────
export function setupCreateAgentModal() {
  const form = document.getElementById('create-agent-form');
  if (!form) return;
  form.addEventListener('submit', async e => {
    e.preventDefault();
    const btn = document.getElementById('btn-agent-submit');
    btn.disabled = true; btn.textContent = 'Creating…';
    try {
      await adminPost('/api/admin/agents', {
        display_name: form.display_name.value.trim(),
        password:     form.password.value,
      });
      showToast('Agent created! Check the list for the assigned ID.', 'success');
      closeModal('modal-create-agent');
      form.reset();
      await loadAgentList();
    } catch (err) {
      showToast(err.displayMessage || 'Create failed', 'error');
    } finally {
      btn.disabled = false; btn.textContent = 'Create Agent';
    }
  });
}

// ─────────────────────────────────────────────────────────────────────────────
// AGENT DETAIL PAGE
// ─────────────────────────────────────────────────────────────────────────────
export async function loadAgentDetail(id) {
  try {
    const today = new Date();
    const [agent, todayPerf, monthPerf, salary, graces, warnings] = await Promise.allSettled([
      adminGet(`/api/admin/agents/${id}`),
      adminGet(`/api/admin/agents/${id}/performance/today`),
      adminGet(`/api/admin/agents/${id}/performance/month/${today.getFullYear()}/${today.getMonth() + 1}`),
      adminGet(`/api/admin/agents/${id}/salary`),
      adminGet(`/api/admin/agents/${id}/graces`),
      adminGet(`/api/admin/agents/${id}/warnings`),
    ]);

    const a  = agent.status      === 'fulfilled' ? agent.value      : null;
    const tp = todayPerf.status  === 'fulfilled' ? todayPerf.value  : null;
    const mp = monthPerf.status  === 'fulfilled' ? monthPerf.value  : null;
    const sl = salary.status     === 'fulfilled' ? salary.value     : null;
    const gr = graces.status     === 'fulfilled' ? graces.value     : null;
    const wn = warnings.status   === 'fulfilled' ? warnings.value   : null;

    if (a) _renderAgentProfile(a);
    if (tp) _renderTodayPerf(tp);
    if (mp) _renderMonthPerf(mp);
    if (sl) _renderSalary(sl, id);
    _renderGraces(gr?.items || []);
    _renderWarnings(wn?.items || []);
  } catch (err) {
    showToast(err.displayMessage || 'Load failed', 'error');
  }
}

function _renderAgentProfile(a) {
  const set = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v ?? '—'; };
  const setHtml = (id, v) => { const el = document.getElementById(id); if (el) el.innerHTML = v; };
  set('ad-agent-id',     a.agent_id);
  set('ad-display-name', a.display_name);
  set('ad-role',         a.role);
  set('ad-created',      fmtDateTime(a.created_at));
  set('ad-last-seen',    a.last_seen_at ? fmtRelative(a.last_seen_at) : 'Never');
  setHtml('ad-status', a.is_active ? '<span class="chip chip-active">Active</span>' : '<span class="chip chip-inactive">Inactive</span>');

  // Wire action buttons
  document.getElementById('btn-ad-toggle')?.addEventListener('click', async () => {
    const activate = !a.is_active;
    if (!confirmAction(`${activate ? 'Activate' : 'Deactivate'} agent ${a.agent_id}?`)) return;
    try {
      await adminPost(`/api/admin/agents/${a.id}/${activate ? 'activate' : 'deactivate'}`, {});
      showToast('Done', 'success');
      loadAgentDetail(a.id);
    } catch (err) { showToast(err.displayMessage || 'Failed', 'error'); }
  });

  document.getElementById('btn-ad-reset-pw')?.addEventListener('click', async () => {
    const pw = prompt('New password for ' + a.agent_id + ':');
    if (!pw || pw.length < 8) { showToast('Password must be ≥ 8 chars', 'warning'); return; }
    if (!confirmAction('Reset password for ' + a.agent_id + '?')) return;
    try {
      await adminPost(`/api/admin/agents/${a.id}/reset-password`, { new_password: pw });
      showToast('Password reset — all sessions invalidated', 'success');
    } catch (err) { showToast(err.displayMessage || 'Failed', 'error'); }
  });
}

function _renderTodayPerf(tp) {
  const el = document.getElementById('ad-today-perf');
  if (!el) return;
  const tiers = tp.tiers || [];
  if (!tiers.length) { el.innerHTML = '<p style="color:var(--color-text-muted);padding:8px">No performance data for today.</p>'; return; }
  el.innerHTML = tiers.map(t => `
    <div style="display:flex;align-items:center;gap:12px;padding:8px 0;border-bottom:1px solid var(--color-border)">
      <div style="font-weight:700;color:var(--color-accent);width:40px">₹${t.entry_fee}</div>
      <div style="flex:1">
        <div style="font-size:12px;color:var(--color-text-muted)">Target: ${t.required_count} | Done: <strong style="color:var(--color-text)">${t.completed_count}</strong></div>
        <div style="height:4px;background:var(--color-bg-3);border-radius:2px;margin-top:4px">
          <div style="height:4px;background:${t.completed_count >= t.required_count ? 'var(--color-success)' : 'var(--color-warning)'};border-radius:2px;width:${Math.min(100, Math.round((t.completed_count/Math.max(t.required_count,1))*100))}%"></div>
        </div>
      </div>
      <div>${statusChip(t.status || 'pending')}</div>
    </div>`).join('');
}

function _renderMonthPerf(mp) {
  const set = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v ?? '—'; };
  set('ad-month-base',      '₹' + (mp.base_salary || '10000.00'));
  set('ad-month-bonus',     '₹' + (mp.total_extra_bonus || '0.00'));
  set('ad-month-deductions','₹' + (mp.total_deductions || '0.00'));
  set('ad-month-adj',       '₹' + (mp.manual_adjustment || '0.00'));
  set('ad-month-final',     '₹' + (mp.estimated_final_salary || '0.00'));
  set('ad-days-met',        mp.days_met ?? 0);
  set('ad-days-missed',     mp.days_missed ?? 0);
  set('ad-days-grace',      mp.days_graced ?? 0);
}

function _renderSalary(sl, agentId) {
  const set = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v ?? '—'; };
  const setHtml = (id, v) => { const el = document.getElementById(id); if (el) el.innerHTML = v; };
  set('ad-sal-base',   '₹' + (sl.base_salary || '10000.00'));
  set('ad-sal-bonus',  '₹' + (sl.total_extra_bonus || '0.00'));
  set('ad-sal-ded',    '₹' + (sl.total_deductions || '0.00'));
  set('ad-sal-adj',    '₹' + (sl.manual_adjustment || '0.00'));
  set('ad-sal-final',  '₹' + (sl.final_salary || '0.00'));
  setHtml('ad-sal-status', statusChip(sl.salary_status || sl.status || 'draft'));

  document.getElementById('btn-ad-sal-adjust')?.addEventListener('click', async () => {
    const amount = prompt('Salary adjustment amount (positive = add, negative = deduct):');
    if (!amount || isNaN(parseFloat(amount))) return;
    const reason = prompt('Reason for adjustment:');
    if (!reason?.trim()) return;
    try {
      await adminPost(`/api/admin/agents/${agentId}/salary/adjust`, { amount: parseFloat(amount), reason });
      showToast('Salary adjusted', 'success');
      loadAgentDetail(agentId);
    } catch (err) { showToast(err.displayMessage || 'Failed', 'error'); }
  });

  document.getElementById('btn-ad-sal-confirm')?.addEventListener('click', async () => {
    if (!confirmAction('Lock and confirm monthly salary?')) return;
    try {
      await adminPost(`/api/admin/agents/${agentId}/salary/confirm`, {});
      showToast('Salary confirmed', 'success');
      loadAgentDetail(agentId);
    } catch (err) { showToast(err.displayMessage || 'Failed', 'error'); }
  });
}

function _renderGraces(graces) {
  const el = document.getElementById('ad-graces-list');
  if (!el) return;
  if (!graces.length) { el.innerHTML = '<p style="color:var(--color-text-muted);font-size:13px;padding:8px">No graces issued.</p>'; return; }
  el.innerHTML = graces.map(g => `
    <div style="padding:8px 0;border-bottom:1px solid var(--color-border);font-size:13px">
      <div style="font-weight:600">${g.grace_date}</div>
      <div style="color:var(--color-text-muted)">${escHtml(g.reason || '—')}</div>
    </div>`).join('');
}

function _renderWarnings(warnings) {
  const el = document.getElementById('ad-warnings-list');
  if (!el) return;
  if (!warnings.length) { el.innerHTML = '<p style="color:var(--color-text-muted);font-size:13px;padding:8px">No warnings issued.</p>'; return; }
  el.innerHTML = warnings.map(w => `
    <div style="padding:8px 0;border-bottom:1px solid var(--color-border);font-size:13px">
      <div style="font-weight:600;color:var(--color-warning)">Warning #${w.warning_number} — ${w.warning_date}</div>
      <div style="color:var(--color-text-muted)">${escHtml(w.reason || '—')}</div>
    </div>`).join('');
}

export async function issueGrace(agentId) {
  const date   = prompt('Date for grace (YYYY-MM-DD):');
  if (!date) return;
  const reason = prompt('Grace reason:');
  if (!reason?.trim()) return;
  // We need the daily_performance_id — fetch today's record
  try {
    await adminPost(`/api/admin/agents/${agentId}/grace`, { grace_date: date, reason });
    showToast('Grace issued', 'success');
    loadAgentDetail(agentId);
  } catch (err) { showToast(err.displayMessage || 'Failed', 'error'); }
}

export async function issueWarning(agentId) {
  const reason = prompt('Warning reason:');
  if (!reason?.trim()) return;
  try {
    await adminPost(`/api/admin/agents/${agentId}/warning`, { reason, warning_date: new Date().toISOString().split('T')[0] });
    showToast('Warning issued', 'warning');
    loadAgentDetail(agentId);
  } catch (err) { showToast(err.displayMessage || 'Failed', 'error'); }
}

// ─────────────────────────────────────────────────────────────────────────────
// PERMISSIONS TAB  (Super Admin only)
//
// Security model:
//   • Only Super Admin can reach these functions — all three backend endpoints
//     (GET/POST/DELETE /api/admin/agents/{id}/permissions) require super_admin
//     role and return 403 for anyone else.
//   • The tab button in agent-details.html has the "admin-only" CSS class, so
//     it is hidden from agents entirely via  body.role-agent .admin-only { display:none }
//   • Agents cannot call grant/revoke APIs — the backend enforces this strictly.
//   • ADMIN_ONLY_PERMISSIONS (finance, settings, agent management, scheduler,
//     audit) are rendered disabled in the UI; the backend also blocks them.
//   • The UI never invents permission codes — every code shown comes from the
//     known constant list below, which mirrors backend/app/core/permissions.py.
// ─────────────────────────────────────────────────────────────────────────────

/**
 * All 29 permission codes, grouped for the UI.
 * Mirrors Permission(StrEnum) in backend/app/core/permissions.py exactly.
 * admin_only=true  → rendered as disabled checkboxes (backend also blocks).
 * admin_only=false → can be granted to agents.
 */
const _PERMISSION_GROUPS = [
  {
    title: '🏆 Tournament Operations',
    items: [
      { code: 'tournament.create',         label: 'Create new tournaments',                   admin_only: false },
      { code: 'tournament.cancel',         label: 'Cancel existing tournaments',              admin_only: false },
      { code: 'tournament.manage_players', label: 'Manage players in tournaments',            admin_only: false },
      { code: 'tournament.release_room',   label: 'Release Room ID and Password',             admin_only: false },
      { code: 'tournament.enter_results',  label: 'Enter tournament results',                 admin_only: false },
      { code: 'tournament.verify_results', label: 'Verify and finalize tournament results',   admin_only: false },
    ],
  },
  {
    title: '📊 Reports & Leaderboard',
    items: [
      { code: 'report.view',               label: 'View leaderboard and platform reports',    admin_only: false },
      { code: 'screenshot.view',           label: 'View match proof screenshots',             admin_only: false },
      { code: 'results.history_view',      label: 'View completed tournament results history (read-only)', admin_only: false },
    ],
  },
  {
    title: '👥 Player Management',
    items: [
      { code: 'user.view',                 label: 'View player profiles and transactions',    admin_only: false },
      { code: 'user.ban',                  label: 'Ban or unban player accounts',             admin_only: false },
    ],
  },
  {
    title: '🆘 Support',
    items: [
      { code: 'support.view',              label: 'View and manage player support requests',  admin_only: false },
    ],
  },
  {
    title: '🤖 Agent Visibility',
    items: [
      { code: 'agent.view',                label: 'View agent profiles and details',          admin_only: false },
      { code: 'agent.view_performance',    label: 'View agent performance and salary data',   admin_only: false },
    ],
  },
  {
    title: '💰 Finance — Read Only',
    items: [
      { code: 'deposit.view',              label: 'View deposit records (read-only)',         admin_only: false },
      { code: 'withdrawal.view',           label: 'View withdrawal records (read-only)',      admin_only: false },
    ],
  },
  {
    // These are ADMIN_ONLY — shown disabled so Super Admin can see the full
    // permission landscape and understand what is blocked for agents.
    title: '🔒 Finance — Admin Only (cannot be granted to agents)',
    adminOnlySection: true,
    items: [
      { code: 'deposit.approve',           label: 'Approve deposit requests',                 admin_only: true },
      { code: 'deposit.reject',            label: 'Reject deposit requests',                  admin_only: true },
      { code: 'withdrawal.process',        label: 'Mark withdrawal as processing',            admin_only: true },
      { code: 'withdrawal.approve',        label: 'Mark withdrawal as paid',                  admin_only: true },
      { code: 'withdrawal.reject',         label: 'Reject withdrawal',                        admin_only: true },
      { code: 'withdrawal.mark_paid',      label: 'Enter UTR and mark withdrawal paid',       admin_only: true },
      { code: 'wallet.manual_adjust',      label: 'Manually adjust player wallet balances',   admin_only: true },
    ],
  },
  {
    title: '⚙️ System — Admin Only (cannot be granted to agents)',
    adminOnlySection: true,
    items: [
      { code: 'agent.create',              label: 'Create new agent accounts',                admin_only: true },
      { code: 'agent.manage',              label: 'Activate, deactivate, update agents',      admin_only: true },
      { code: 'agent.reset_password',      label: 'Reset an agent\'s login password',         admin_only: true },
      { code: 'agent.manage_performance',  label: 'Grant grace, issue warnings',              admin_only: true },
      { code: 'agent.manage_salary',       label: 'Manual salary adjustments and confirm',    admin_only: true },
      { code: 'agent.manage_targets',      label: 'Configure daily tournament targets',       admin_only: true },
      { code: 'settings.manage',           label: 'Manage platform settings',                 admin_only: true },
      { code: 'audit.view',                label: 'View audit logs',                          admin_only: true },
      { code: 'scheduler.view',            label: 'View scheduler job history',               admin_only: true },
      { code: 'scheduler.trigger',         label: 'Manually trigger scheduled jobs',          admin_only: true },
    ],
  },
];

// Track unsaved state between load and save
let _pendingGrant  = new Set();   // codes to grant  (newly checked)
let _pendingRevoke = new Set();   // codes to revoke  (newly unchecked)
let _currentGrants = new Set();   // codes the agent holds right now (from API)

/**
 * Load the agent's current permissions from the backend and render the tab.
 * Called when Super Admin first opens the Permissions tab, and on Reload.
 *
 * @param {string} agentId  — UUID of the target agent (from URL ?id=)
 */
export async function loadPermissions(agentId) {
  const bodyEl  = document.getElementById('ad-perm-body');
  const footEl  = document.getElementById('ad-perm-footer');
  const statusEl = document.getElementById('ad-perm-status');
  const msgEl   = document.getElementById('ad-perm-save-msg');
  if (!bodyEl) return;

  // Reset pending state
  _pendingGrant.clear();
  _pendingRevoke.clear();
  _currentGrants.clear();
  if (msgEl) msgEl.textContent = '';

  // Show loading
  bodyEl.innerHTML = '<div style="text-align:center;padding:32px"><div class="loading-spinner"></div></div>';
  if (footEl) footEl.style.display = 'none';
  if (statusEl) statusEl.textContent = '';

  try {
    const data = await adminGet(`/api/admin/agents/${agentId}/permissions`);
    // data = { agent_id: "AGT-0001", permissions: [{code, description, granted_at, ...}] }
    _currentGrants = new Set((data.permissions || []).map(p => p.code));

    _renderPermissionsTab(bodyEl, _currentGrants);

    const count = _currentGrants.size;
    if (statusEl) statusEl.textContent = `${count} permission${count !== 1 ? 's' : ''} granted`;
    if (footEl) footEl.style.display = 'flex';

  } catch (err) {
    bodyEl.innerHTML = `
      <div style="padding:var(--space-4);background:rgba(255,71,87,.08);border:1px solid var(--color-danger);
                  border-radius:var(--radius-md);color:var(--color-danger);font-size:var(--text-sm)">
        ⚠ ${escHtml(err.displayMessage || 'Failed to load permissions.')}
        <button class="btn btn-ghost btn-sm" style="margin-left:var(--space-3)"
          onclick="document.getElementById('btn-ad-perm-reload')?.click()">Retry</button>
      </div>`;
    if (footEl) footEl.style.display = 'none';
  }
}

/**
 * Render the grouped permission checkboxes into bodyEl.
 * Grantable permissions = interactive checkboxes.
 * Admin-only permissions = disabled checkboxes with a lock label.
 *
 * @param {HTMLElement} bodyEl
 * @param {Set<string>} currentGrants  — codes the agent currently holds
 */
function _renderPermissionsTab(bodyEl, currentGrants) {
  bodyEl.innerHTML = _PERMISSION_GROUPS.map(group => {
    const isAdminOnlySection = group.adminOnlySection === true;

    const rows = group.items.map(item => {
      const checked  = currentGrants.has(item.code) ? 'checked' : '';
      const disabled = item.admin_only ? 'disabled' : '';
      const lockBadge = item.admin_only
        ? '<span style="margin-left:6px;font-size:10px;background:rgba(255,71,87,.15);color:var(--color-danger);padding:1px 6px;border-radius:var(--radius-full);font-weight:600">Admin Only</span>'
        : '';

      return `
        <label class="perm-row${item.admin_only ? ' perm-row-disabled' : ''}">
          <input type="checkbox"
            class="perm-checkbox"
            data-code="${escHtml(item.code)}"
            data-admin-only="${item.admin_only}"
            ${checked}
            ${disabled}>
          <span class="perm-label">
            <span class="perm-code">${escHtml(item.code)}</span>
            <span class="perm-desc">${escHtml(item.label)}</span>
          </span>
          ${lockBadge}
        </label>`;
    }).join('');

    const sectionStyle = isAdminOnlySection
      ? 'opacity:.6;pointer-events:none'
      : '';

    return `
      <div class="perm-group" style="${sectionStyle}">
        <div class="perm-group-title">${group.title}</div>
        ${rows}
      </div>`;
  }).join('');

  // CSS injected once — idempotent
  _injectPermissionStyles();

  // Wire change listeners on every interactive checkbox
  bodyEl.querySelectorAll('.perm-checkbox:not([disabled])').forEach(cb => {
    cb.addEventListener('change', () => {
      const code = cb.dataset.code;
      const nowChecked = cb.checked;
      const wasGranted = _currentGrants.has(code);

      if (nowChecked && !wasGranted) {
        // User checked something that wasn't granted → pending grant
        _pendingGrant.add(code);
        _pendingRevoke.delete(code);
      } else if (!nowChecked && wasGranted) {
        // User unchecked something that was granted → pending revoke
        _pendingRevoke.add(code);
        _pendingGrant.delete(code);
      } else {
        // Restored to original state → no longer pending
        _pendingGrant.delete(code);
        _pendingRevoke.delete(code);
      }

      _updateSaveStatus();
    });
  });
}

/**
 * Apply pending grant/revoke operations sequentially, then reload.
 * Each grant/revoke is a separate API call — matches the backend's
 * idempotent POST/DELETE endpoint design.
 *
 * @param {string} agentId
 */
export async function savePermissions(agentId) {
  if (_pendingGrant.size === 0 && _pendingRevoke.size === 0) {
    showToast('No changes to save.', 'info');
    return;
  }

  const saveBtn = document.getElementById('btn-ad-perm-save');
  const msgEl   = document.getElementById('ad-perm-save-msg');
  if (saveBtn) { saveBtn.disabled = true; saveBtn.textContent = 'Saving…'; }
  if (msgEl)   { msgEl.textContent = ''; }

  const errors = [];
  let grantOk = 0, revokeOk = 0;

  // Process grants
  for (const code of _pendingGrant) {
    try {
      await adminPost(`/api/admin/agents/${agentId}/permissions`, { permission_code: code });
      grantOk++;
    } catch (err) {
      errors.push(`Grant ${code}: ${err.displayMessage || 'failed'}`);
    }
  }

  // Process revokes
  for (const code of _pendingRevoke) {
    try {
      await adminDelete(`/api/admin/agents/${agentId}/permissions/${encodeURIComponent(code)}`);
      revokeOk++;
    } catch (err) {
      errors.push(`Revoke ${code}: ${err.displayMessage || 'failed'}`);
    }
  }

  if (saveBtn) { saveBtn.disabled = false; saveBtn.textContent = 'Save Permissions'; }

  if (errors.length) {
    const msg = errors.join('; ');
    showToast('Some changes failed: ' + msg, 'error', 7000);
    if (msgEl) { msgEl.textContent = '⚠ ' + msg; msgEl.style.color = 'var(--color-danger)'; }
  } else {
    const parts = [];
    if (grantOk > 0)  parts.push(`${grantOk} granted`);
    if (revokeOk > 0) parts.push(`${revokeOk} revoked`);
    showToast('Permissions saved: ' + parts.join(', '), 'success');
  }

  // Always reload to reflect the actual server state
  await loadPermissions(agentId);
}

// ── Helpers ───────────────────────────────────────────────────────────────────

/** Update the "N unsaved changes" indicator on the save button. */
function _updateSaveStatus() {
  const total = _pendingGrant.size + _pendingRevoke.size;
  const msgEl = document.getElementById('ad-perm-save-msg');
  if (!msgEl) return;
  if (total === 0) {
    msgEl.textContent = '';
  } else {
    msgEl.textContent = `${total} unsaved change${total !== 1 ? 's' : ''}`;
    msgEl.style.color = 'var(--color-warning)';
  }
}

/** Inject scoped CSS for the permissions tab once per page load. */
function _injectPermissionStyles() {
  if (document.getElementById('perm-tab-styles')) return; // already injected
  const style = document.createElement('style');
  style.id = 'perm-tab-styles';
  style.textContent = `
    .perm-group {
      margin-bottom: var(--space-5);
    }
    .perm-group-title {
      font-size: var(--text-xs);
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: .06em;
      color: var(--color-text-muted);
      padding: 0 0 var(--space-2);
      border-bottom: 1px solid var(--color-border);
      margin-bottom: var(--space-2);
    }
    .perm-row {
      display: flex;
      align-items: center;
      gap: var(--space-3);
      padding: var(--space-2) var(--space-2);
      border-radius: var(--radius-sm);
      cursor: pointer;
      transition: background var(--transition);
      user-select: none;
    }
    .perm-row:hover:not(.perm-row-disabled) {
      background: var(--color-bg-3);
    }
    .perm-row-disabled {
      cursor: default;
    }
    .perm-row input[type="checkbox"] {
      width: 15px;
      height: 15px;
      flex-shrink: 0;
      cursor: pointer;
      accent-color: var(--color-accent);
    }
    .perm-row input[type="checkbox"]:disabled {
      cursor: default;
      opacity: .45;
    }
    .perm-label {
      display: flex;
      flex-direction: column;
      gap: 1px;
      flex: 1;
      min-width: 0;
    }
    .perm-code {
      font-size: var(--text-xs);
      font-weight: 700;
      font-family: var(--font-mono);
      color: var(--color-text);
    }
    .perm-desc {
      font-size: 11px;
      color: var(--color-text-muted);
    }
    @media (max-width: 600px) {
      .perm-row { flex-wrap: wrap; }
    }
  `;
  document.head.appendChild(style);
}


// ─────────────────────────────────────────────────────────────────────────────
// AGENT TOURNAMENT HISTORY TAB  (Super Admin only)
//
// Calls GET /api/admin/agents/{id}/tournaments — a Super-Admin-only endpoint
// that scopes results by tournament.created_by_id == agent_id server-side.
// Agents cannot call this endpoint (backend returns 403).
// ─────────────────────────────────────────────────────────────────────────────

// Module-level state for the tournament history tab
let _thAgentId   = null;   // UUID of the agent whose history is shown
let _thOffset    = 0;
const _thLimit   = 20;
let _thSearch    = '';
let _thStatus    = '';
let _thDebounce  = null;   // debounce timer for search input

/**
 * Entry point called once when the tab is first opened (lazy-loaded).
 * Stores the agent ID, wires all filter controls, then fetches data.
 *
 * @param {string} agentId  UUID of the agent from the URL ?id= parameter
 */
export async function loadAgentTournamentHistory(agentId) {
  _thAgentId = agentId;
  _thOffset  = 0;
  _thSearch  = '';
  _thStatus  = '';

  _wireTournamentHistoryControls();
  await _fetchTournamentHistory();
}

// ── Wire filter/search/refresh controls ──────────────────────────────────────

function _wireTournamentHistoryControls() {
  // Search input with debounce
  const searchEl = document.getElementById('th-search');
  if (searchEl) {
    // Avoid duplicate listeners if tab is re-opened
    const newEl = searchEl.cloneNode(true);
    searchEl.parentNode.replaceChild(newEl, searchEl);
    newEl.addEventListener('input', e => {
      clearTimeout(_thDebounce);
      _thDebounce = setTimeout(() => {
        _thSearch  = e.target.value.trim();
        _thOffset  = 0;
        _fetchTournamentHistory();
      }, 350);
    });
  }

  // Status filter
  const statusEl = document.getElementById('th-filter-status');
  if (statusEl) {
    const newEl = statusEl.cloneNode(true);
    statusEl.parentNode.replaceChild(newEl, statusEl);
    newEl.addEventListener('change', e => {
      _thStatus = e.target.value;
      _thOffset = 0;
      _fetchTournamentHistory();
    });
  }

  // Reset button
  const resetBtn = document.getElementById('btn-th-reset');
  if (resetBtn) {
    const newBtn = resetBtn.cloneNode(true);
    resetBtn.parentNode.replaceChild(newBtn, resetBtn);
    newBtn.addEventListener('click', () => {
      const s = document.getElementById('th-search');
      const f = document.getElementById('th-filter-status');
      if (s) s.value = '';
      if (f) f.value = '';
      _thSearch = '';
      _thStatus = '';
      _thOffset = 0;
      _fetchTournamentHistory();
    });
  }

  // Refresh button
  const refreshBtn = document.getElementById('btn-th-refresh');
  if (refreshBtn) {
    const newBtn = refreshBtn.cloneNode(true);
    refreshBtn.parentNode.replaceChild(newBtn, refreshBtn);
    newBtn.addEventListener('click', () => _fetchTournamentHistory());
  }
}

// ── Fetch from backend and render ─────────────────────────────────────────────

async function _fetchTournamentHistory() {
  if (!_thAgentId) return;

  showTableSkeleton('th-tbody', 6, 6);
  const badgeEl = document.getElementById('th-total-badge');
  if (badgeEl) badgeEl.textContent = '';

  try {
    const params = { limit: _thLimit, offset: _thOffset };
    if (_thStatus) params.status = _thStatus;
    if (_thSearch) params.search = _thSearch;

    const data = await adminGet(`/api/admin/agents/${_thAgentId}/tournaments`, params);
    _renderTournamentHistoryTable(data.items || [], data.total || 0);
  } catch (err) {
    const tbody = document.getElementById('th-tbody');
    if (tbody) tbody.innerHTML = emptyRow(6, '⚠ ' + (err.displayMessage || 'Failed to load tournament history'));
    if (badgeEl) badgeEl.textContent = '';
  }
}

// ── Render the table ──────────────────────────────────────────────────────────

function _renderTournamentHistoryTable(items, total) {
  const tbody   = document.getElementById('th-tbody');
  const badgeEl = document.getElementById('th-total-badge');

  if (badgeEl) badgeEl.textContent = `${total} tournament${total !== 1 ? 's' : ''}`;

  if (!tbody) return;

  if (!items.length) {
    tbody.innerHTML = emptyRow(6, 'No tournaments found for this agent.');
    renderPagination('th-pagination', 0, _thLimit, _thOffset, () => {});
    return;
  }

  tbody.innerHTML = items.map(t => {
    // Link to the existing tournament details page (Super Admin can access it)
    const detailUrl = `/admin/tournament-details.html?id=${encodeURIComponent(t.id)}`;
    const lifecycle = t.completed_at
      ? `<span style="color:var(--color-success);font-size:11px">${fmtDateTime(t.completed_at)}</span>`
      : t.cancelled_at
        ? `<span style="color:var(--color-danger);font-size:11px">${fmtDateTime(t.cancelled_at)}</span>`
        : '<span style="color:var(--color-text-dim);font-size:11px">—</span>';

    return `
      <tr style="cursor:pointer" onclick="location.href='${detailUrl}'">
        <td>
          <a href="${detailUrl}" class="td-link" onclick="event.stopPropagation()">${escHtml(t.tournament_code)}</a>
          <div style="font-size:11px;color:var(--color-text-muted);margin-top:2px">${escHtml(t.title)}</div>
        </td>
        <td style="font-weight:600">₹${escHtml(String(t.entry_fee))}</td>
        <td>${statusChip(t.status)}</td>
        <td style="text-align:right">${t.player_count ?? 0} <span style="color:var(--color-text-dim)">/ ${t.max_players ?? 50}</span></td>
        <td style="font-size:12px;color:var(--color-text-muted)">${fmtDateTime(t.scheduled_at)}</td>
        <td>${lifecycle}</td>
      </tr>`;
  }).join('');

  renderPagination('th-pagination', total, _thLimit, _thOffset, off => {
    _thOffset = off;
    _fetchTournamentHistory();
  });
}
