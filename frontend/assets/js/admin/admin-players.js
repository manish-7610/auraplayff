/**
 * AuraPlayFF Admin — Player Management
 *
 * APIs used:
 *   GET  /api/admin/users                              list/search
 *   GET  /api/admin/users/{id}                         detail
 *   POST /api/admin/users/{id}/ban                     ban
 *   POST /api/admin/users/{id}/unban                   unban
 *   POST /api/admin/users/{id}/issue-password-reset    reset token
 *   GET  /api/admin/wallets/{id}                       wallet balances
 *   GET  /api/admin/wallets/{id}/transactions          ledger
 */

import { adminGet, adminPost, getStaffProfile } from './admin-api.js';
import {
  showToast, statusChip, fmtDateTime, fmtRelative, fmtCurrency,
  escHtml, renderPagination, showTableSkeleton, emptyRow, confirmAction, copyToClipboard
} from './admin-ui.js';

let _offset = 0;
const _limit = 25;
let _search = '';
let _filterActive = '';
let _filterBanned = '';

// ── List ──────────────────────────────────────────────────────────────────────
export async function loadPlayerList() {
  showTableSkeleton('players-tbody', 7, 8);
  try {
    const params = { limit: _limit, offset: _offset };
    if (_search)       params.search    = _search;
    if (_filterActive !== '') params.is_active = _filterActive;
    if (_filterBanned !== '') params.is_banned = _filterBanned;
    const data = await adminGet('/api/admin/users', params);
    _renderTable(data.items || [], data.total || 0);
  } catch (err) {
    document.getElementById('players-tbody').innerHTML = emptyRow(7, '⚠ ' + (err.displayMessage || 'Load failed'));
  }
}

function _renderTable(items, total) {
  const tbody = document.getElementById('players-tbody');
  if (!tbody) return;
  if (!items.length) {
    tbody.innerHTML = emptyRow(7, 'No players found.');
    renderPagination('players-pagination', 0, _limit, 0, () => {});
    return;
  }
  tbody.innerHTML = items.map(p => `
    <tr>
      <td>
        <a href="#" class="player-detail-link" data-id="${escHtml(p.id)}" style="color:var(--color-blue);font-weight:600">${escHtml(p.username)}</a>
      </td>
      <td class="td-mono">${escHtml(p.ff_uid)}</td>
      <td>
        ${p.is_banned ? '<span class="chip chip-banned">Banned</span>' : (p.is_active ? '<span class="chip chip-active">Active</span>' : '<span class="chip chip-inactive">Inactive</span>')}
      </td>
      <td style="text-align:right">${p.total_tournaments ?? 0}</td>
      <td style="text-align:right">${p.total_wins ?? 0}</td>
      <td class="td-amount">₹${p.total_winnings ?? '0.00'}</td>
      <td style="font-size:12px;color:var(--color-text-muted)">${fmtDateTime(p.created_at)}</td>
    </tr>`).join('');

  // Player detail links
  document.querySelectorAll('.player-detail-link').forEach(a => {
    a.addEventListener('click', e => { e.preventDefault(); openPlayerModal(a.dataset.id); });
  });

  renderPagination('players-pagination', total, _limit, _offset, off => {
    _offset = off;
    loadPlayerList();
  });
}

// ── Filters ───────────────────────────────────────────────────────────────────
export function setupPlayerFilters() {
  const searchInput = document.getElementById('player-search');
  let debounceTimer;
  searchInput?.addEventListener('input', e => {
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(() => {
      _search = e.target.value.trim();
      _offset = 0;
      loadPlayerList();
    }, 400);
  });

  document.getElementById('filter-player-status')?.addEventListener('change', e => {
    const v = e.target.value;
    if (v === 'banned')   { _filterBanned = 'true';  _filterActive = ''; }
    else if (v === 'active')   { _filterActive = 'true';  _filterBanned = 'false'; }
    else if (v === 'inactive') { _filterActive = 'false'; _filterBanned = ''; }
    else { _filterActive = ''; _filterBanned = ''; }
    _offset = 0;
    loadPlayerList();
  });

  document.getElementById('btn-player-reset')?.addEventListener('click', () => {
    if (searchInput) searchInput.value = '';
    document.getElementById('filter-player-status').value = '';
    _search = ''; _filterActive = ''; _filterBanned = '';
    _offset = 0;
    loadPlayerList();
  });
}

// ── Player detail modal ───────────────────────────────────────────────────────
let _currentPlayerId = null;

export async function openPlayerModal(id) {
  _currentPlayerId = id;
  const modal = document.getElementById('modal-player');
  if (!modal) return;
  modal.classList.add('open');

  // Loading state
  document.getElementById('pm-username').textContent  = 'Loading…';
  document.getElementById('pm-body').innerHTML = '<div style="text-align:center;padding:40px"><div class="loading-spinner"></div></div>';

  try {
    const [user, wallet] = await Promise.allSettled([
      adminGet(`/api/admin/users/${id}`),
      adminGet(`/api/admin/wallets/${id}`),
    ]);
    const u = user.status === 'fulfilled' ? user.value : null;
    const w = wallet.status === 'fulfilled' ? wallet.value : null;
    if (u) _renderPlayerModal(u, w);
  } catch (err) {
    document.getElementById('pm-body').innerHTML = `<p style="color:var(--color-danger)">${escHtml(err.displayMessage || 'Load failed')}</p>`;
  }
}

function _renderPlayerModal(u, w) {
  document.getElementById('pm-username').textContent = u.username;

  document.getElementById('pm-body').innerHTML = `
    <div class="detail-grid" style="margin-bottom:20px">
      <div class="detail-item"><div class="detail-label">FF UID</div><div class="detail-value mono">${escHtml(u.ff_uid)}</div></div>
      <div class="detail-item"><div class="detail-label">Status</div><div class="detail-value">
        ${u.is_banned ? '<span class="chip chip-banned">Banned</span>' : (u.is_active ? '<span class="chip chip-active">Active</span>' : '<span class="chip chip-inactive">Inactive</span>')}
      </div></div>
      <div class="detail-item"><div class="detail-label">Referral Code</div><div class="detail-value mono">${escHtml(u.referral_code)}</div></div>
      <div class="detail-item"><div class="detail-label">Joined</div><div class="detail-value">${fmtDateTime(u.created_at)}</div></div>
      <div class="detail-item"><div class="detail-label">Last Seen</div><div class="detail-value">${u.last_seen_at ? fmtRelative(u.last_seen_at) : '—'}</div></div>
    </div>

    <div class="stats-grid" style="grid-template-columns:repeat(4,1fr);margin-bottom:20px">
      <div class="stat-card"><div class="stat-label">Tournaments</div><div class="stat-value">${u.total_tournaments ?? 0}</div></div>
      <div class="stat-card"><div class="stat-label">Wins</div><div class="stat-value">${u.total_wins ?? 0}</div></div>
      <div class="stat-card"><div class="stat-label">Kills</div><div class="stat-value">${u.total_kills ?? 0}</div></div>
      <div class="stat-card success"><div class="stat-label">Winnings</div><div class="stat-value" style="font-size:16px">₹${u.total_winnings ?? '0.00'}</div></div>
    </div>

    ${w ? `
    <div class="card" style="margin-bottom:16px">
      <div class="card-header"><span class="card-title">💰 Wallet Balances</span>${w.is_frozen ? '<span class="chip chip-danger">FROZEN</span>' : ''}</div>
      <div class="stats-grid" style="grid-template-columns:repeat(4,1fr)">
        <div class="stat-card"><div class="stat-label">Deposit</div><div class="stat-value" style="font-size:16px">₹${w.deposit_balance ?? u.deposit_balance ?? '0.00'}</div></div>
        <div class="stat-card"><div class="stat-label">Winnings</div><div class="stat-value" style="font-size:16px">₹${w.winning_balance ?? u.winning_balance ?? '0.00'}</div></div>
        <div class="stat-card"><div class="stat-label">Referral</div><div class="stat-value" style="font-size:16px">₹${w.referral_balance ?? u.referral_balance ?? '0.00'}</div></div>
        <div class="stat-card warning"><div class="stat-label">On Hold</div><div class="stat-value" style="font-size:16px">₹${w.withdrawal_hold ?? u.withdrawal_hold ?? '0.00'}</div></div>
      </div>
    </div>` : ''}

    <div style="display:flex;gap:10px;flex-wrap:wrap">
      ${u.is_banned
        ? `<button class="btn btn-success btn-sm" id="btn-pm-unban">✓ Unban Player</button>`
        : `<button class="btn btn-danger btn-sm" id="btn-pm-ban">⛔ Ban Player</button>`}
      ${getStaffProfile()?.role === 'super_admin'
        ? `<button class="btn btn-warning btn-sm" id="btn-pm-reset">🔑 Issue Password Reset</button>`
        : ''
      }
    </div>
    <div id="pm-reset-result" style="margin-top:12px"></div>`;

  // Ban/Unban handlers
  document.getElementById('btn-pm-ban')?.addEventListener('click', () => _banPlayer(u.id, u.username, true));
  document.getElementById('btn-pm-unban')?.addEventListener('click', () => _banPlayer(u.id, u.username, false));
  document.getElementById('btn-pm-reset')?.addEventListener('click', () => _issueReset(u.id));
}

async function _banPlayer(id, username, ban) {
  const action = ban ? 'Ban' : 'Unban';
  if (!confirmAction(`${action} player "${username}"?`)) return;
  try {
    await adminPost(`/api/admin/users/${id}/${ban ? 'ban' : 'unban'}`, {});
    showToast(`Player ${ban ? 'banned' : 'unbanned'} successfully`, ban ? 'warning' : 'success');
    document.getElementById('modal-player')?.classList.remove('open');
    loadPlayerList();
  } catch (err) {
    showToast(err.displayMessage || 'Action failed', 'error');
  }
}

async function _issueReset(id) {
  if (!confirmAction('Issue a one-time password reset token?')) return;
  try {
    const data = await adminPost(`/api/admin/users/${id}/issue-password-reset`, {});
    const resultEl = document.getElementById('pm-reset-result');
    if (resultEl) {
      resultEl.innerHTML = `
        <div class="alert alert-warning">
          <div>
            <div style="font-weight:700;margin-bottom:4px">⚠ Reset Token — Share with player ONCE only</div>
            <div style="font-family:var(--font-mono);font-size:13px;word-break:break-all;margin-bottom:8px">${escHtml(data.reset_token)}</div>
            <div style="font-size:12px">Expires in ${data.expires_in_minutes} minutes.</div>
            <button class="btn btn-ghost btn-sm" style="margin-top:8px" onclick="import('/assets/js/admin/admin-ui.js').then(m=>m.copyToClipboard('${escHtml(data.reset_token)}'))">📋 Copy Token</button>
          </div>
        </div>`;
    }
  } catch (err) {
    showToast(err.displayMessage || 'Failed to issue reset', 'error');
  }
}
