/**
 * AuraPlayFF Admin — Results & Winner Declaration
 * v2026-09-30-A  ← version stamp; confirms browser loaded fresh file
 *
 * APIs used:
 *   GET  /api/admin/tournaments                         list (for selector)
 *   GET  /api/admin/tournaments/{id}/entries            enrolled players
 *   GET  /api/admin/tournaments/{id}/results            existing results
 *   POST /api/admin/tournaments/{id}/results            enter result
 *   PUT  /api/admin/tournaments/{id}/results/{rid}      correct result
 *   POST /api/admin/tournaments/{id}/results/{rid}/verify  verify result
 *   GET  /api/admin/prize-configs/kill-reward-preview   kill reward calc
 *
 * Response shapes confirmed against live backend:
 *   /entries  → { items: AdminEntryResponse[], total, limit, offset }
 *   /results  → { tournament_id, results: AdminResultResponse[], total }
 */

console.log('[admin-results] module loaded v2026-09-30-A');

import { adminGet, adminPost } from './admin-api.js';
import {
  showToast, statusChip, fmtDateTime, fmtCurrency,
  escHtml, renderPagination, showTableSkeleton, emptyRow, confirmAction,
} from './admin-ui.js';

let _selectedTournament = null;
let _entries = [];   // AdminEntryResponse[] — enrolled players for current tournament
let _results = [];   // AdminResultResponse[] — existing results
// Map entry_id → { username, ff_uid, sheet_number } for fast lookup in results table
let _entryMap = {};

// ── Tournament selector ───────────────────────────────────────────────────────

export async function loadResultsTournamentList() {
  const select = document.getElementById('results-tourn-select');
  if (!select) return;
  select.innerHTML = '<option value="">Loading tournaments…</option>';
  try {
    // Load live + result_entry tournaments via Promise.allSettled so one
    // failure doesn't suppress the other.
    const [liveData, resultData] = await Promise.allSettled([
      adminGet('/api/admin/tournaments', { status: 'live',         limit: 50 }),
      adminGet('/api/admin/tournaments', { status: 'result_entry', limit: 50 }),
    ]);

    const items = [
      ...(liveData.status   === 'fulfilled' ? (liveData.value?.items   || []) : []),
      ...(resultData.status === 'fulfilled' ? (resultData.value?.items || []) : []),
    ];

    if (!items.length) {
      select.innerHTML = '<option value="">No live/result-entry tournaments</option>';
      return;
    }

    // AdminTournamentSummary uses field "id" (not "tournament_id")
    select.innerHTML =
      '<option value="">Select tournament…</option>' +
      items.map(t =>
        `<option value="${escHtml(t.id)}"
                 data-fee="${escHtml(String(t.entry_fee))}"
                 data-code="${escHtml(t.tournament_code)}">` +
        `[${escHtml(t.tournament_code)}] ₹${escHtml(String(t.entry_fee))} — ${escHtml(t.status)}` +
        `</option>`
      ).join('');

  } catch (err) {
    console.error('[admin-results] loadResultsTournamentList error:', err);
    select.innerHTML = '<option value="">Failed to load tournaments</option>';
  }
}

export function setupResultsTournamentSelect() {
  document.getElementById('results-tourn-select')
    ?.addEventListener('change', async e => {
      const id = e.target.value;
      if (!id) { _clearResultsView(); return; }
      _selectedTournament = id;
      await _loadEntriesAndResults(id);
    });
}

// ── Load entries + results for selected tournament ────────────────────────────

async function _loadEntriesAndResults(id) {
  document.getElementById('results-section')?.style.setProperty('display', 'block');
  showTableSkeleton('results-tbody', 7, 6);

  // ── Fetch entries ────────────────────────────────────────────────────────
  let entriesResp;
  try {
    entriesResp = await adminGet(
      `/api/admin/tournaments/${id}/entries`,
      { limit: 100, active_only: true },
    );
    console.log('[admin-results] entries response:', entriesResp);
  } catch (err) {
    console.error('[admin-results] /entries failed:', err);
    document.getElementById('results-tbody').innerHTML =
      emptyRow(7, `⚠ Entries load failed: ${err.displayMessage || err.message || 'unknown error'}`);
    return;
  }

  // ── Fetch results ────────────────────────────────────────────────────────
  let resultsResp;
  try {
    resultsResp = await adminGet(`/api/admin/tournaments/${id}/results`);
    console.log('[admin-results] results response:', resultsResp);
  } catch (err) {
    console.error('[admin-results] /results failed:', err);
    document.getElementById('results-tbody').innerHTML =
      emptyRow(7, `⚠ Results load failed: ${err.displayMessage || err.message || 'unknown error'}`);
    return;
  }

  // ── Parse responses ──────────────────────────────────────────────────────
  // /entries  → { items: [...], total, limit, offset }
  _entries = entriesResp.items || [];

  // /results  → { tournament_id, results: [...], total }
  // NOTE: the key is "results", NOT "items"
  _results = resultsResp.results || resultsResp.items || [];

  console.log(`[admin-results] _entries: ${_entries.length}, _results: ${_results.length}`);

  // Build entry_id → entry lookup for use in results table
  _entryMap = {};
  for (const e of _entries) {
    _entryMap[e.entry_id] = e;
  }

  _renderResultsTable();
  _populateEntrySelector();
}

// ── Player Entry dropdown ─────────────────────────────────────────────────────

function _populateEntrySelector() {
  const select = document.getElementById('inp-entry-select');
  if (!select) return;

  // Filter out entries that already have a result
  const resultedEntryIds = new Set(_results.map(r => r.entry_id));
  const pending = _entries.filter(e => !resultedEntryIds.has(e.entry_id));

  console.log(`[admin-results] pending entries for dropdown: ${pending.length}`);

  select.innerHTML =
    '<option value="">Select player…</option>' +
    pending.map(e => {
      // Show ff_name (if set) or fall back to username in the dropdown label
      const displayName = e.ff_name || e.username;
      const label = e.ff_name
        ? `[#${e.sheet_number}] ${e.username} / FF: ${e.ff_name} (${e.ff_uid})`
        : `[#${e.sheet_number}] ${e.username} (${e.ff_uid})`;
      return `<option value="${escHtml(e.entry_id)}" data-ff="${escHtml(e.ff_uid)}">${escHtml(label)}</option>`;
    }).join('');
}

// ── Entered Results table ─────────────────────────────────────────────────────

function _renderResultsTable() {
  const tbody = document.getElementById('results-tbody');
  if (!tbody) return;

  if (!_results.length) {
    tbody.innerHTML = emptyRow(7, 'No results entered yet. Use the form on the right.');
    return;
  }

  tbody.innerHTML = _results.map(r => {
    // Look up entry for username/ff_uid fallback (entries loaded with ff_name)
    const entry = _entryMap[r.entry_id];
    const username  = entry ? entry.username : '—';
    // ff_name_snapshot = name at result-entry time; fall back to live entry.ff_name
    const ffName    = r.ff_name_snapshot || (entry ? entry.ff_name : null);
    const ffUid     = escHtml(r.ff_uid_snapshot || (entry ? entry.ff_uid : '') || '—');

    // FF Name is the primary visible player name; username is secondary/internal ID
    const playerCell = ffName
      ? `<div style="font-weight:600">${escHtml(ffName)}</div>
         <div style="font-size:11px;color:var(--color-text-muted)">${escHtml(username)}</div>`
      : `<div style="font-weight:600">${escHtml(username)}</div>`;

    return `
      <tr>
        <td style="font-weight:700;color:var(--color-blue)">#${r.placement}</td>
        <td>${playerCell}</td>
        <td class="td-mono">${ffUid}</td>
        <td style="text-align:right">${r.kills}</td>
        <td class="td-amount">₹${r.prize_amount ?? '0.00'}</td>
        <td class="td-amount">₹${r.kill_reward_amount ?? '0.00'}</td>
        <td>${statusChip(r.result_status)}</td>
      </tr>`;
  }).join('');
}

// ── Enter result form ─────────────────────────────────────────────────────────

export function setupResultEntryForm() {
  const form = document.getElementById('result-entry-form');
  if (!form) return;

  // Live kill reward preview when kills or placement changes
  ['inp-kills', 'inp-placement'].forEach(id => {
    document.getElementById(id)?.addEventListener('input', _previewReward);
  });

  form.addEventListener('submit', async e => {
    e.preventDefault();
    if (!_selectedTournament) { showToast('Select a tournament first', 'warning'); return; }

    const entryId   = document.getElementById('inp-entry-select').value;
    const placement = parseInt(document.getElementById('inp-placement').value, 10);
    const kills     = parseInt(document.getElementById('inp-kills').value, 10) || 0;

    if (!entryId)               { showToast('Select a player entry', 'warning'); return; }
    if (!placement || placement < 1) { showToast('Placement must be ≥ 1', 'warning'); return; }

    const entry = _entryMap[entryId];
    const label = entry ? `${entry.username} (#${entry.sheet_number})` : entryId;
    if (!confirmAction(`Enter result for ${label}:\n  Placement: #${placement}\n  Kills: ${kills}`)) return;

    const btn = document.getElementById('btn-submit-result');
    btn.disabled = true;
    btn.textContent = 'Saving…';

    try {
      await adminPost(`/api/admin/tournaments/${_selectedTournament}/results`, {
        entry_id:  entryId,
        placement,
        kills,
      });
      showToast('Result saved successfully', 'success');
      form.reset();
      document.getElementById('reward-preview').innerHTML = '';
      await _loadEntriesAndResults(_selectedTournament);
    } catch (err) {
      console.error('[admin-results] enter result failed:', err);
      showToast(err.displayMessage || err.message || 'Failed to save result', 'error');
    } finally {
      btn.disabled = false;
      btn.textContent = 'Save Result';
    }
  });
}

// ── Kill reward preview ───────────────────────────────────────────────────────

async function _previewReward() {
  const kills     = parseInt(document.getElementById('inp-kills')?.value, 10) || 0;
  const placement = parseInt(document.getElementById('inp-placement')?.value, 10) || 1;
  const previewEl = document.getElementById('reward-preview');
  if (!previewEl || !_selectedTournament) return;

  const sel = document.getElementById('results-tourn-select');
  const fee = sel?.selectedOptions[0]?.dataset?.fee;
  if (!fee) return;

  try {
    const data = await adminGet('/api/admin/prize-configs/kill-reward-preview', {
      placement, kills, entry_fee: fee,
    });
    previewEl.innerHTML = `
      <div class="reward-breakdown">
        <div class="reward-row">
          <span>Kill Reward (${kills} kills)</span>
          <span class="td-amount">₹${data.reward ?? '0.00'}</span>
        </div>
        ${data.breakdown ? `<div style="font-size:11px;color:var(--color-text-dim);padding:4px 0">${escHtml(data.breakdown)}</div>` : ''}
      </div>
      <p style="font-size:11px;color:var(--color-text-dim);margin-top:6px">
        Preview only — backend calculates final amounts on save.
      </p>`;
  } catch {
    previewEl.innerHTML = '';
  }
}

// ── Verify result (called from results table if verify button added) ───────────

export async function verifyResult(tournId, resultId) {
  if (!confirmAction('Verify this result?')) return;
  try {
    await adminPost(`/api/admin/tournaments/${tournId}/results/${resultId}/verify`, {});
    showToast('Result verified', 'success');
    await _loadEntriesAndResults(tournId);
  } catch (err) {
    showToast(err.displayMessage || err.message || 'Verify failed', 'error');
  }
}

// ── Clear view when no tournament selected ────────────────────────────────────

function _clearResultsView() {
  _selectedTournament = null;
  _entries = [];
  _results = [];
  _entryMap = {};
  const s = document.getElementById('results-section');
  if (s) s.style.display = 'none';
}


// ══════════════════════════════════════════════════════════════════════════════
// COMPLETED RESULTS HISTORY  (Phase 11 — read-only)
// ══════════════════════════════════════════════════════════════════════════════

let _historyOffset = 0;
const HISTORY_LIMIT = 20;
let _historySearchTimer = null;

/**
 * Called once when the "Completed History" tab is first activated.
 * Sets up search input, refresh button, and initial data load.
 */
export function initHistoryTab() {
  // Search with debounce
  document.getElementById('history-search')?.addEventListener('input', () => {
    clearTimeout(_historySearchTimer);
    _historySearchTimer = setTimeout(() => {
      _historyOffset = 0;
      loadCompletedHistory();
    }, 400);
  });

  // Refresh button
  document.getElementById('btn-history-refresh')?.addEventListener('click', () => {
    _historyOffset = 0;
    loadCompletedHistory();
  });

  // Modal close button + backdrop click
  document.getElementById('detail-modal-close')?.addEventListener('click', _closeDetailModal);
  document.getElementById('history-detail-modal')?.addEventListener('click', e => {
    if (e.target === e.currentTarget) _closeDetailModal();
  });

  // Initial load
  loadCompletedHistory();
}

// ── Load paginated list of completed tournaments ───────────────────────────────

export async function loadCompletedHistory() {
  const tbody  = document.getElementById('history-tbody');
  const pagEl  = document.getElementById('history-pagination');
  const badge  = document.getElementById('history-total-badge');

  if (!tbody) return;

  // Show skeleton
  showTableSkeleton('history-tbody', 7, 4);
  if (pagEl) pagEl.innerHTML = '';

  const search = document.getElementById('history-search')?.value?.trim() || '';

  const params = { limit: HISTORY_LIMIT, offset: _historyOffset };
  if (search) params.search = search;

  let data;
  try {
    data = await adminGet('/api/admin/results/history', params);
  } catch (err) {
    console.error('[admin-results] history load failed:', err);
    tbody.innerHTML = emptyRow(7, `⚠ Failed to load history: ${err.displayMessage || err.message || 'Unknown error'}`);
    return;
  }

  const items = data?.items || [];
  const total = data?.total ?? 0;

  if (badge) {
    badge.textContent = `${total} completed`;
    badge.style.display = total > 0 ? 'inline-block' : 'none';
  }

  _renderHistoryList(tbody, items);

  // Pagination
  if (total > HISTORY_LIMIT && pagEl) {
    renderPagination('history-pagination', total, HISTORY_LIMIT, _historyOffset, newOffset => {
      _historyOffset = newOffset;
      loadCompletedHistory();
    });
  } else if (pagEl) {
    pagEl.innerHTML = total > 0
      ? `<span style="font-size:13px;color:var(--color-text-muted)">Showing all ${total} completed tournament${total !== 1 ? 's' : ''}</span>`
      : '';
  }
}

// ── Render history list table rows ────────────────────────────────────────────

function _renderHistoryList(tbody, items) {
  if (!items.length) {
    tbody.innerHTML = emptyRow(7, 'No completed tournaments found.');
    return;
  }

  tbody.innerHTML = items.map(t => {
    const paidCount  = t.payout_distributed_count ?? 0;
    const totalCount = t.result_count ?? 0;
    const allPaid    = totalCount > 0 && paidCount === totalCount;
    const nonePaid   = paidCount === 0;

    const payoutChipClass = allPaid ? 'chip-finalized' : nonePaid ? 'chip-pending' : 'chip-entered';
    const payoutLabel     = allPaid ? 'All Paid' : nonePaid ? 'Pending' : `${paidCount}/${totalCount} Paid`;

    return `
      <tr class="history-tourn-row" data-tournament-id="${escHtml(String(t.tournament_id))}"
          title="Click to view results for ${escHtml(t.tournament_code)}">
        <td>
          <div style="font-weight:700;color:var(--color-blue)">${escHtml(t.tournament_code)}</div>
          <div style="font-size:12px;color:var(--color-text-muted);margin-top:2px">${escHtml(t.tournament_title)}</div>
        </td>
        <td class="td-amount">₹${escHtml(t.entry_fee)}</td>
        <td style="text-align:right">${t.player_count ?? '—'}</td>
        <td style="text-align:right">${totalCount > 0 ? totalCount : '—'}</td>
        <td><span class="chip ${payoutChipClass}">${payoutLabel}</span></td>
        <td class="td-mono" style="font-size:12px">${fmtDateTime(t.completed_at)}</td>
        <td>
          <button class="btn btn-ghost btn-sm"
                  onclick="window._openCompletedDetail('${escHtml(String(t.tournament_id))}', '${escHtml(t.tournament_code)}')"
                  title="View results">
            View →
          </button>
        </td>
      </tr>`;
  }).join('');

  // Also make the whole row clickable
  tbody.querySelectorAll('.history-tourn-row').forEach(row => {
    row.addEventListener('click', e => {
      if (e.target.tagName === 'BUTTON') return; // button has its own handler
      const id   = row.dataset.tournamentId;
      const code = row.querySelector('td div')?.textContent?.trim() || '';
      openCompletedTournamentDetail(id, code);
    });
  });
}

// ── Open detail modal ─────────────────────────────────────────────────────────

// Expose globally so inline onclick handlers work
window._openCompletedDetail = (tournamentId, tournamentCode) => {
  openCompletedTournamentDetail(tournamentId, tournamentCode);
};

export async function openCompletedTournamentDetail(tournamentId, tournamentCode) {
  const modal    = document.getElementById('history-detail-modal');
  const metaEl   = document.getElementById('detail-modal-meta');
  const tbody    = document.getElementById('detail-results-tbody');
  const titleEl  = document.getElementById('detail-modal-title');

  if (!modal || !tbody) return;

  // Open modal and show skeleton
  modal.classList.add('open');
  if (titleEl) titleEl.textContent = `Results — ${tournamentCode || 'Tournament'}`;
  if (metaEl)  metaEl.innerHTML   = '<span style="color:var(--color-text-muted)">Loading…</span>';
  showTableSkeleton('detail-results-tbody', 8, 5);

  let data;
  try {
    data = await adminGet(`/api/admin/results/history/${tournamentId}`);
  } catch (err) {
    console.error('[admin-results] history detail load failed:', err);
    tbody.innerHTML = emptyRow(8, `⚠ Failed to load results: ${err.displayMessage || err.message || 'Unknown error'}`);
    if (metaEl) metaEl.innerHTML = '';
    return;
  }

  // Update modal title and meta bar
  if (titleEl) {
    titleEl.textContent = `Results — ${data.tournament_code}: ${data.tournament_title}`;
  }
  if (metaEl) {
    metaEl.innerHTML = `
      <div class="modal-meta-item">
        <span class="modal-meta-label">Tournament</span>
        <span class="modal-meta-value">${escHtml(data.tournament_code)}</span>
      </div>
      <div class="modal-meta-item">
        <span class="modal-meta-label">Title</span>
        <span class="modal-meta-value">${escHtml(data.tournament_title)}</span>
      </div>
      <div class="modal-meta-item">
        <span class="modal-meta-label">Entry Fee</span>
        <span class="modal-meta-value td-amount">₹${escHtml(data.entry_fee)}</span>
      </div>
      <div class="modal-meta-item">
        <span class="modal-meta-label">Players</span>
        <span class="modal-meta-value">${data.player_count ?? data.total ?? '—'}</span>
      </div>
      <div class="modal-meta-item">
        <span class="modal-meta-label">Status</span>
        <span class="chip chip-completed">Completed</span>
      </div>
      <div class="modal-meta-item">
        <span class="modal-meta-label">Completed At</span>
        <span class="modal-meta-value" style="font-size:12px">${fmtDateTime(data.completed_at)}</span>
      </div>`;
  }

  // Render results table
  _renderDetailResultsTable(tbody, data.results || []);
}

function _renderDetailResultsTable(tbody, results) {
  if (!results.length) {
    tbody.innerHTML = emptyRow(8, 'No finalized results for this tournament.');
    return;
  }

  tbody.innerHTML = results.map(r => {
    // Payout status derived from backend field — no recalculation
    const paidLabel   = r.payout_distributed ? 'Paid' : 'Pending';
    const paidClass   = r.payout_distributed ? 'chip-finalized' : 'chip-pending';
    const rankStyle   = r.placement <= 3
      ? 'font-weight:800;color:var(--color-blue)'
      : 'font-weight:600';

    // Preferred display order: ff_name_snapshot (historical) → ff_name (live) → username
    // ff_name_snapshot is NULL for results entered before migration 0012
    const primaryName = r.ff_name_snapshot || r.ff_name || null;
    const playerCell  = primaryName
      ? `<div style="font-weight:600">${escHtml(primaryName)}</div>
         <div style="font-size:11px;color:var(--color-text-muted)">${escHtml(r.username || '—')}</div>`
      : `<div style="font-weight:600">${escHtml(r.username || '—')}</div>`;

    return `
      <tr>
        <td style="${rankStyle}">#${r.placement}</td>
        <td>${playerCell}</td>
        <td class="td-mono" style="font-size:12px">${escHtml(r.ff_uid_snapshot || '—')}</td>
        <td style="text-align:right">${r.kills ?? 0}</td>
        <td class="td-amount" style="text-align:right">₹${escHtml(r.prize_amount ?? '0.00')}</td>
        <td class="td-amount" style="text-align:right">
          ₹${escHtml(r.kill_reward_amount ?? '0.00')}
          ${r.kill_reward_breakdown
            ? `<div style="font-size:10px;color:var(--color-text-dim)">${escHtml(r.kill_reward_breakdown)}</div>`
            : ''}
        </td>
        <td class="td-amount" style="text-align:right;font-weight:700">₹${escHtml(r.total_payout ?? '0.00')}</td>
        <td><span class="chip ${paidClass}">${paidLabel}</span></td>
      </tr>`;
  }).join('');
}

function _closeDetailModal() {
  document.getElementById('history-detail-modal')?.classList.remove('open');
}
