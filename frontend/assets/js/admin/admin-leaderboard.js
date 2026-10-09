/**
 * AuraPlayFF Admin — Leaderboard Module
 *
 * Provides read-only leaderboard/statistics views for Admin and Agent roles.
 * Accessible to: Super Admin (always) + Agent with report.view permission.
 *
 * APIs used:
 *   GET /api/admin/metrics/leaderboard/players?sort_by=&limit=&offset=
 *   GET /api/admin/metrics/leaderboard/tournaments?window=&limit=&offset=
 *
 * All displayed values (prize_amount, kill_reward_amount, total_payout,
 * total_winnings, total_kills, total_wins, total_tournaments) come directly
 * from finalized backend records.  No frontend recalculation is performed.
 */

import { adminGet } from './admin-api.js';
import {
  fmtCurrency, fmtDateTime, escHtml,
  showTableSkeleton, emptyRow, renderPagination,
} from './admin-ui.js';

// ── State ─────────────────────────────────────────────────────────────────────
const LIMIT = 20;

let _playersState = { offset: 0, sortBy: 'winnings' };
let _tournamentsState = { offset: 0, window: '24h' };

// Track which tournament top-10 rows are expanded
const _expandedTournaments = new Set();

// ── Entry points ──────────────────────────────────────────────────────────────

/**
 * Wire up all tab, sort, filter, and pagination controls on the page,
 * then perform the initial data load.
 * Call once after initAdminLayout().
 */
export function initLeaderboard() {
  _wireTabs();
  _wireSortChips();
  _wireWindowChips();
  _wireRefresh();
  // Default: load players tab
  loadPlayers();
}

// ── Players leaderboard ───────────────────────────────────────────────────────

export async function loadPlayers(offset = _playersState.offset) {
  _playersState.offset = offset;

  const tbody  = document.getElementById('lb-players-tbody');
  const errEl  = document.getElementById('lb-players-error');
  const pagEl  = document.getElementById('lb-players-pagination');
  if (!tbody) return;

  showTableSkeleton('lb-players-tbody', 6, 8);
  if (errEl) { errEl.textContent = ''; errEl.style.display = 'none'; }

  try {
    const data = await adminGet('/api/admin/metrics/leaderboard/players', {
      sort_by: _playersState.sortBy,
      limit:   LIMIT,
      offset:  _playersState.offset,
    });

    if (!data.items.length) {
      tbody.innerHTML = emptyRow(6, 'No players have completed a tournament yet.');
      if (pagEl) pagEl.innerHTML = '';
      return;
    }

    tbody.innerHTML = data.items.map(p => {
      const rankCls = p.rank === 1 ? 'style="color:var(--color-warning);font-weight:800"'
                    : p.rank === 2 ? 'style="color:var(--color-text-muted);font-weight:700"'
                    : p.rank === 3 ? 'style="color:#cd7f32;font-weight:700"'
                    : '';
      // display_name = ff_name when set, else username (resolved by backend)
      const displayName = p.display_name || p.username;
      const playerCell  = displayName !== p.username
        ? `<div style="font-weight:600">${escHtml(displayName)}</div>
           <div style="font-size:11px;color:var(--color-text-muted)">${escHtml(p.username)}</div>`
        : `<div style="font-weight:600">${escHtml(displayName)}</div>`;
      return `<tr>
        <td ${rankCls}>${_rankIcon(p.rank)}</td>
        <td>${playerCell}</td>
        <td class="td-mono">${p.total_tournaments}</td>
        <td class="td-mono" style="color:var(--color-success)">${p.total_wins}</td>
        <td class="td-mono">${p.total_kills}</td>
        <td class="td-amount">${fmtCurrency(p.total_winnings)}</td>
      </tr>`;
    }).join('');

    if (pagEl) {
      renderPagination('lb-players-pagination', data.total, LIMIT, _playersState.offset,
        newOff => loadPlayers(newOff));
    }

  } catch (err) {
    tbody.innerHTML = emptyRow(6, '');
    if (errEl) {
      errEl.textContent = err.displayMessage || 'Failed to load player leaderboard.';
      errEl.style.display = 'block';
    }
  }
}

// ── Tournaments leaderboard ───────────────────────────────────────────────────

export async function loadTournaments(offset = _tournamentsState.offset) {
  _tournamentsState.offset = offset;

  const listEl = document.getElementById('lb-tournaments-list');
  const errEl  = document.getElementById('lb-tournaments-error');
  const pagEl  = document.getElementById('lb-tournaments-pagination');
  if (!listEl) return;

  listEl.innerHTML = _skeletonCards(3);
  if (errEl) { errEl.textContent = ''; errEl.style.display = 'none'; }
  _expandedTournaments.clear();

  try {
    const data = await adminGet('/api/admin/metrics/leaderboard/tournaments', {
      window: _tournamentsState.window,
      limit:  LIMIT,
      offset: _tournamentsState.offset,
    });

    if (!data.items.length) {
      listEl.innerHTML = `
        <div style="padding:40px;text-align:center;color:var(--color-text-muted)">
          ${_tournamentsState.window === '24h'
            ? 'No tournaments completed in the last 24 hours.'
            : 'No completed tournaments found.'}
        </div>`;
      if (pagEl) pagEl.innerHTML = '';
      return;
    }

    listEl.innerHTML = data.items.map(t => _buildTournamentCard(t)).join('');

    // Wire expand/collapse buttons
    listEl.querySelectorAll('[data-expand-tid]').forEach(btn => {
      btn.addEventListener('click', () => _toggleTop10(btn.dataset.expandTid));
    });

    if (pagEl) {
      renderPagination('lb-tournaments-pagination', data.total, LIMIT, _tournamentsState.offset,
        newOff => loadTournaments(newOff));
    }

  } catch (err) {
    listEl.innerHTML = '';
    if (errEl) {
      errEl.textContent = err.displayMessage || 'Failed to load tournament leaderboard.';
      errEl.style.display = 'block';
    }
  }
}

// ── Card builder ──────────────────────────────────────────────────────────────

function _buildTournamentCard(t) {
  const w = t.winner;
  const completedStr = fmtDateTime(t.completed_at);
  const top = t.top_results || [];

  // Winner summary row
  const winnerHtml = w
    ? (() => {
        // display_name = ff_name_snapshot → live ff_name → username (resolved by backend)
        const wName     = w.display_name || w.username;
        const wSubLabel = wName !== w.username
          ? `<span class="lb-winner-sub">${escHtml(w.username)}</span>`
          : '';
        return `<div class="lb-winner-row">
          <span class="lb-winner-medal">🥇</span>
          <span class="lb-winner-name">${escHtml(wName)}${wSubLabel}</span>
          <span class="lb-winner-stat">${w.kills} Kills</span>
          <span class="lb-winner-sep">·</span>
          <span class="lb-winner-stat">Prize <strong style="color:var(--color-success)">${fmtCurrency(w.prize_amount)}</strong></span>
          <span class="lb-winner-sep">·</span>
          <span class="lb-winner-stat">Kill Rew. <strong style="color:var(--color-blue)">${fmtCurrency(w.kill_reward_amount)}</strong></span>
          <span class="lb-winner-sep">·</span>
          <span class="lb-winner-stat">Total <strong style="color:var(--color-accent)">${fmtCurrency(w.total_payout)}</strong></span>
        </div>`;
      })()
    : `<div style="font-size:var(--text-xs);color:var(--color-text-dim);padding:var(--space-2) 0">
        No finalized results yet.
      </div>`;

  // Top-10 table rows
  const resultRows = top.map(r => {
    // display_name = ff_name_snapshot → live ff_name → username (resolved by backend)
    const rName     = r.display_name || r.username;
    const playerCell = rName !== r.username
      ? `<div style="font-weight:600">${escHtml(rName)}</div>
         <div style="font-size:11px;color:var(--color-text-muted)">${escHtml(r.username)}</div>`
      : `<div style="font-weight:600">${escHtml(rName)}</div>`;
    return `
    <tr>
      <td style="text-align:center;font-size:15px;font-weight:800;width:44px">${_rankIcon(r.placement)}</td>
      <td>${playerCell}</td>
      <td class="td-mono td-muted" style="font-size:var(--text-xs)">${escHtml(r.ff_uid_snapshot || '—')}</td>
      <td class="td-mono">${r.kills}</td>
      <td class="td-amount">${fmtCurrency(r.prize_amount)}</td>
      <td class="td-mono" style="color:var(--color-blue)">${fmtCurrency(r.kill_reward_amount)}</td>
      <td class="td-amount">${fmtCurrency(r.total_payout)}</td>
    </tr>`;
  }).join('');

  const top10Html = top.length > 0
    ? `<div class="lb-top10-body" id="lb-top10-${escHtml(t.tournament_id)}" style="display:none">
        <div class="table-container" style="border:none;border-top:1px solid var(--color-border)">
          <table>
            <thead><tr>
              <th style="width:44px">Rank</th>
              <th>Player</th>
              <th>FF UID</th>
              <th>Kills</th>
              <th>Prize</th>
              <th>Kill Reward</th>
              <th>Total Payout</th>
            </tr></thead>
            <tbody>${resultRows}</tbody>
          </table>
        </div>
      </div>`
    : '';

  const expandBtn = top.length > 0
    ? `<button class="btn btn-ghost btn-sm" data-expand-tid="${escHtml(t.tournament_id)}"
         style="font-size:var(--text-xs)">
         <span class="lb-expand-icon" id="lb-icon-${escHtml(t.tournament_id)}">▼</span>
         Top ${top.length}
       </button>`
    : `<span style="font-size:var(--text-xs);color:var(--color-text-dim)">No results</span>`;

  return `
    <div class="card" style="padding:0;margin-bottom:var(--space-4)">
      <div class="lb-t-card-header">
        <div class="lb-t-card-left">
          <div class="lb-t-title">🏆 ${escHtml(t.title)}</div>
          <div class="lb-t-meta">
            <span>${escHtml(t.tournament_code)}</span>
            <span class="lb-meta-sep">·</span>
            <span>${completedStr}</span>
            <span class="lb-meta-sep">·</span>
            <span>${t.player_count} Players</span>
            <span class="lb-meta-sep">·</span>
            <span style="color:var(--color-accent);font-weight:700">${fmtCurrency(t.entry_fee)} Entry</span>
          </div>
          ${winnerHtml}
        </div>
        <div class="lb-t-card-actions">
          ${expandBtn}
        </div>
      </div>
      ${top10Html}
    </div>`;
}

// ── Expand / collapse top-10 ──────────────────────────────────────────────────

function _toggleTop10(tid) {
  const body = document.getElementById(`lb-top10-${tid}`);
  const icon = document.getElementById(`lb-icon-${tid}`);
  if (!body) return;

  const isOpen = body.style.display !== 'none';
  body.style.display = isOpen ? 'none' : 'block';
  if (icon) icon.textContent = isOpen ? '▼' : '▲';

  if (isOpen) _expandedTournaments.delete(tid);
  else        _expandedTournaments.add(tid);
}

// ── Tab wiring ────────────────────────────────────────────────────────────────

function _wireTabs() {
  document.querySelectorAll('[data-lb-tab]').forEach(btn => {
    btn.addEventListener('click', () => {
      const tab = btn.dataset.lbTab;
      document.querySelectorAll('[data-lb-tab]').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      document.getElementById('lb-panel-players')?.classList.toggle('hidden', tab !== 'players');
      document.getElementById('lb-panel-tournaments')?.classList.toggle('hidden', tab !== 'tournaments');
      if (tab === 'players')     loadPlayers(0);
      if (tab === 'tournaments') loadTournaments(0);
    });
  });
}

// ── Sort chips (Players tab) ──────────────────────────────────────────────────

function _wireSortChips() {
  document.querySelectorAll('[data-lb-sort]').forEach(chip => {
    chip.addEventListener('click', () => {
      _playersState.sortBy = chip.dataset.lbSort;
      _playersState.offset = 0;
      document.querySelectorAll('[data-lb-sort]').forEach(c => c.classList.remove('active'));
      chip.classList.add('active');
      loadPlayers(0);
    });
  });
}

// ── Window chips (Tournaments tab) ───────────────────────────────────────────

function _wireWindowChips() {
  document.querySelectorAll('[data-lb-window]').forEach(chip => {
    chip.addEventListener('click', () => {
      _tournamentsState.window = chip.dataset.lbWindow;
      _tournamentsState.offset = 0;
      document.querySelectorAll('[data-lb-window]').forEach(c => c.classList.remove('active'));
      chip.classList.add('active');
      loadTournaments(0);
    });
  });
}

// ── Refresh button ────────────────────────────────────────────────────────────

function _wireRefresh() {
  document.getElementById('lb-btn-refresh')?.addEventListener('click', () => {
    const activeTab = document.querySelector('[data-lb-tab].active')?.dataset.lbTab;
    if (activeTab === 'tournaments') loadTournaments(0);
    else                             loadPlayers(0);
  });
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function _rankIcon(rank) {
  if (rank === 1) return '🥇';
  if (rank === 2) return '🥈';
  if (rank === 3) return '🥉';
  return `#${rank}`;
}

function _skeletonCards(n) {
  return Array.from({ length: n }, () => `
    <div class="card" style="padding:var(--space-5);margin-bottom:var(--space-4)">
      <div class="skeleton" style="height:14px;width:40%;border-radius:4px;margin-bottom:10px"></div>
      <div class="skeleton" style="height:11px;width:60%;border-radius:4px;margin-bottom:8px"></div>
      <div class="skeleton" style="height:11px;width:50%;border-radius:4px"></div>
    </div>`).join('');
}
