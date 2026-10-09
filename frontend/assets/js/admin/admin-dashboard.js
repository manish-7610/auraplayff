/**
 * AuraPlayFF Admin — Dashboard
 * Loads real metrics from backend APIs only. No mock data.
 *
 * APIs used:
 *   GET /api/admin/metrics/active-users
 *   GET /api/admin/metrics/tournaments
 *   GET /api/admin/metrics/agents
 *   GET /api/admin/deposits/pending-manual  (pending count)
 *   GET /api/admin/withdrawals/pending       (pending count)
 *   GET /api/admin/tournaments               (recent list)
 *   GET /api/staff/notifications             (recent notifications)
 */

import { adminGet, isSuper } from './admin-api.js';
import { fmtCurrency, fmtDateTime, fmtRelative, showStatSkeleton, statusChip, escHtml } from './admin-ui.js';

export async function loadDashboard() {
  // Show skeletons while loading
  const statsGrid = document.getElementById('stats-grid');
  if (statsGrid) {
    statsGrid.innerHTML = Array.from({ length: 10 }, () =>
      `<div class="stat-card"><div class="skeleton" style="height:14px;width:60%;margin-bottom:8px;border-radius:4px"></div><div class="skeleton" style="height:28px;width:40%;border-radius:4px"></div></div>`
    ).join('');
  }

  // Parallel requests — financial ones guarded by isSuper()
  const requests = [
    adminGet('/api/admin/metrics/active-users'),
    adminGet('/api/admin/metrics/tournaments'),
    adminGet('/api/admin/metrics/agents'),
    adminGet('/api/admin/tournaments', { limit: 8 }),
    adminGet('/api/staff/notifications', { limit: 5, unread_only: false }),
  ];

  // Only fetch financial if super admin (backend also enforces, but no point erroring)
  if (isSuper()) {
    requests.push(adminGet('/api/admin/deposits/pending-manual', { limit: 1 }));
    requests.push(adminGet('/api/admin/withdrawals/pending', { limit: 1 }));
  }

  const results = await Promise.allSettled(requests);

  const users    = results[0].status === 'fulfilled' ? results[0].value : null;
  const tourn    = results[1].status === 'fulfilled' ? results[1].value : null;
  const agents   = results[2].status === 'fulfilled' ? results[2].value : null;
  const tournList= results[3].status === 'fulfilled' ? results[3].value : null;
  const notifs   = results[4].status === 'fulfilled' ? results[4].value : null;
  const depo     = isSuper() && results[5]?.status === 'fulfilled' ? results[5].value : null;
  const withdr   = isSuper() && results[6]?.status === 'fulfilled' ? results[6].value : null;

  // ── Stat cards ────────────────────────────────────────────────────────────
  const statDefs = [
    { label: 'Total Players',       value: users?.total_registered ?? '—',    icon: '👥', cls: '' },
    { label: 'Online Now',           value: users?.online_now ?? '—',          icon: '🟢', cls: 'success' },
    { label: 'Active Today (IST)',    value: users?.active_today_ist ?? '—',    icon: '📅', cls: 'blue' },
    { label: 'Active Last 24h',       value: users?.active_last_24h ?? '—',     icon: '⏰', cls: '' },
    { label: 'Open Tournaments',      value: _countByStatus(tourn, 'open'),     icon: '🏆', cls: 'success' },
    { label: 'Live Now',              value: _countByStatus(tourn, 'live'),     icon: '🔴', cls: 'accent' },
    { label: 'Total Completed',       value: _countByStatus(tourn, 'completed'),icon: '✅', cls: '' },
    { label: 'Pending Deposits',      value: depo?.total ?? (isSuper() ? '—' : 'N/A'), icon: '💰', cls: (depo?.total > 0) ? 'warning' : '' },
    { label: 'Pending Withdrawals',   value: withdr?.total ?? (isSuper() ? '—' : 'N/A'), icon: '💸', cls: (withdr?.total > 0) ? 'warning' : '' },
    { label: 'Total Agents',          value: Array.isArray(agents) ? agents.length : '—', icon: '🤖', cls: '' },
  ];

  if (statsGrid) {
    statsGrid.innerHTML = statDefs.map(s => `
      <div class="stat-card ${s.cls}">
        <div class="stat-icon">${s.icon}</div>
        <div class="stat-label">${escHtml(s.label)}</div>
        <div class="stat-value">${escHtml(String(s.value))}</div>
      </div>`).join('');
  }

  // ── Recent tournaments ────────────────────────────────────────────────────
  _renderRecentTournaments(tournList?.items ?? []);

  // ── Pending financial actions (admin only) ────────────────────────────────
  if (isSuper()) {
    _renderPendingActions(depo?.total ?? 0, withdr?.total ?? 0);
  } else {
    const el = document.getElementById('dash-pending-list');
    if (el) el.innerHTML = `<p style="padding:16px;color:var(--color-text-muted);font-size:13px">Not available for agents.</p>`;
  }

  // ── Recent notifications ──────────────────────────────────────────────────
  _renderRecentNotifications(notifs?.items ?? notifs ?? []);

  // ── Agent today summary ───────────────────────────────────────────────────
  if (Array.isArray(agents) && agents.length) {
    _renderAgentSummary(agents.slice(0, 6));
  } else {
    const el = document.getElementById('dash-agents-list');
    if (el) el.innerHTML = `<p style="padding:16px;color:var(--color-text-muted);font-size:13px">No agents found.</p>`;
  }
}

// ── Helpers ───────────────────────────────────────────────────────────────────
function _countByStatus(tournData, status) {
  if (!tournData) return '—';
  // Backend returns { by_status: {open:N, live:N,...} } or flat counts
  if (tournData.by_status?.[status] !== undefined) return tournData.by_status[status];
  if (tournData[status] !== undefined) return tournData[status];
  return '—';
}

function _renderRecentTournaments(items) {
  const tbody = document.getElementById('dash-tourn-tbody');
  if (!tbody) return;
  if (!items.length) {
    tbody.innerHTML = `<tr><td colspan="5" class="empty-state" style="padding:24px">No tournaments yet.</td></tr>`;
    return;
  }
  tbody.innerHTML = items.map(t => {
    const id = t.tournament_id || t.id;
    return `<tr>
      <td><a href="/admin/tournament-details.html?id=${encodeURIComponent(id)}" style="color:var(--color-blue);font-weight:600">${escHtml(t.tournament_code)}</a></td>
      <td>₹${t.entry_fee}</td>
      <td>${statusChip(t.status)}</td>
      <td>${t.player_count ?? 0}/${t.max_players ?? 50}</td>
      <td class="td-muted" style="font-size:12px">${_fmtShort(t.scheduled_at)}</td>
    </tr>`;
  }).join('');
}

function _renderPendingActions(pendingDepo, pendingWithdr) {
  const el = document.getElementById('dash-pending-list');
  if (!el) return;

  const items = [
    { label: 'Deposits awaiting approval', count: pendingDepo,  href: '/admin/deposits.html',     icon: '💰', urgent: pendingDepo > 0 },
    { label: 'Withdrawals to process',      count: pendingWithdr, href: '/admin/withdrawals.html',  icon: '💸', urgent: pendingWithdr > 0 },
  ];

  el.innerHTML = items.map(i => `
    <a href="${i.href}" style="display:flex;align-items:center;gap:12px;padding:12px;border-bottom:1px solid var(--color-border);text-decoration:none;color:inherit;transition:background .15s" onmouseover="this.style.background='var(--color-bg-3)'" onmouseout="this.style.background=''">
      <span style="font-size:22px">${i.icon}</span>
      <div style="flex:1">
        <div style="font-size:13px;font-weight:500;color:var(--color-text)">${i.label}</div>
      </div>
      <span style="font-size:18px;font-weight:700;color:${i.urgent ? 'var(--color-warning)' : 'var(--color-text-muted)'}">${i.count}</span>
    </a>`).join('');
}

function _renderRecentNotifications(items) {
  const el = document.getElementById('dash-notif-list');
  if (!el) return;
  if (!items.length) {
    el.innerHTML = `<p style="padding:16px;color:var(--color-text-muted);font-size:13px;text-align:center">No notifications.</p>`;
    return;
  }
  el.innerHTML = items.slice(0, 5).map(n => `
    <div style="display:flex;gap:10px;padding:10px 0;border-bottom:1px solid var(--color-border);align-items:flex-start">
      <span style="font-size:18px;flex-shrink:0">${n.is_read ? '📭' : '📬'}</span>
      <div style="flex:1;overflow:hidden">
        <div style="font-size:13px;font-weight:${n.is_read ? '400' : '600'};color:var(--color-text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${escHtml(n.title || '')}</div>
        <div style="font-size:11px;color:var(--color-text-muted)">${fmtRelative(n.created_at)}</div>
      </div>
    </div>`).join('');
}

function _renderAgentSummary(agents) {
  const el = document.getElementById('dash-agents-list');
  if (!el) return;
  el.innerHTML = agents.map(a => `
    <a href="/admin/agent-details.html?id=${encodeURIComponent(a.id || a.agent_id)}" style="display:flex;align-items:center;gap:12px;padding:10px 0;border-bottom:1px solid var(--color-border);text-decoration:none;color:inherit">
      <span style="font-size:20px">🤖</span>
      <div style="flex:1;overflow:hidden">
        <div style="font-weight:600;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${escHtml(a.display_name || a.agent_id)}</div>
        <div style="font-size:11px;color:var(--color-text-muted)">${escHtml(a.agent_id || '')}</div>
      </div>
      <div style="text-align:right;flex-shrink:0">
        <div style="font-size:13px;font-weight:700;color:${a.is_active ? 'var(--color-success)' : 'var(--color-danger)'}">${a.is_active ? '● Active' : '● Inactive'}</div>
        <div style="font-size:10px;color:var(--color-text-dim)">${a.completed_today !== undefined ? a.completed_today + ' today' : ''}</div>
      </div>
    </a>`).join('');
}

function _fmtShort(iso) {
  if (!iso) return '—';
  try {
    return new Date(iso).toLocaleString('en-IN', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: true });
  } catch { return iso; }
}
