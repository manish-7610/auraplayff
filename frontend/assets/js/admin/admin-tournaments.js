/**
 * AuraPlayFF Admin — Tournament Management
 *
 * APIs used:
 *   GET    /api/admin/tournaments               list
 *   POST   /api/admin/tournaments               create
 *   GET    /api/admin/tournaments/{id}          detail
 *   GET    /api/admin/tournaments/{id}/entries  participants
 *   POST   /api/admin/tournaments/{id}/open
 *   POST   /api/admin/tournaments/{id}/confirm
 *   POST   /api/admin/tournaments/{id}/release-room
 *   POST   /api/admin/tournaments/{id}/go-live
 *   POST   /api/admin/tournaments/{id}/enter-results
 *   POST   /api/admin/tournaments/{id}/complete
 *   POST   /api/admin/tournaments/{id}/cancel
 */

import { adminGet, adminPost } from './admin-api.js';
import { showToast, statusChip, fmtDateTime, fmtRelative, escHtml, renderPagination, showTableSkeleton, emptyRow, confirmAction, copyToClipboard } from './admin-ui.js';

const ENTRY_FEES = [10, 20, 30, 40, 50, 100];
const STATUS_TRANSITIONS = {
  draft:          { next: 'open',           action: 'open',          label: 'Open for Registration', perm: 'tournament.create' },
  open:           { next: 'confirmed',      action: 'confirm',       label: 'Confirm Tournament',    perm: 'tournament.create' },
  confirmed:      { next: 'room_released',  action: 'release-room',  label: 'Release Room',          perm: 'tournament.release_room' },
  room_released:  { next: 'live',           action: 'go-live',       label: 'Go Live',               perm: 'tournament.manage_players' },
  live:           { next: 'result_entry',   action: 'enter-results', label: 'Enter Results Phase',   perm: 'tournament.enter_results' },
  result_entry:   { next: 'completed',      action: 'complete',      label: 'Complete Tournament',   perm: 'tournament.verify_results' },
};

// ── Tournament List ───────────────────────────────────────────────────────────
let _offset = 0;
const _limit = 20;
let _filters = { status: '', entry_fee: '' };

export async function loadTournamentList() {
  showTableSkeleton('tourn-tbody', 6, 8);
  try {
    const params = { limit: _limit, offset: _offset };
    if (_filters.status)    params.status = _filters.status;
    // BUG #5 FIX: entry_fee filter was captured but never included in the
    // API request params, so pagination totals always reflected the full
    // unfiltered table.  Now included so both results and totals are correct.
    if (_filters.entry_fee) params.entry_fee = _filters.entry_fee;
    const data = await adminGet('/api/admin/tournaments', params);
    _renderTournamentTable(data.items || [], data.total || 0);
  } catch (err) {
    document.getElementById('tourn-tbody').innerHTML = emptyRow(6, '⚠ ' + (err.displayMessage || 'Failed to load'));
  }
}

function _renderTournamentTable(items, total) {
  const tbody = document.getElementById('tourn-tbody');
  if (!tbody) return;

  if (!items.length) {
    tbody.innerHTML = emptyRow(6, 'No tournaments found. Create the first one!');
    document.getElementById('tourn-pagination') && renderPagination('tourn-pagination', 0, _limit, _offset, () => {});
    return;
  }

  tbody.innerHTML = items.map(t => {
    const detailUrl = `/admin/tournament-details.html?id=${encodeURIComponent(t.tournament_id || t.id)}`;
    return `
    <tr style="cursor:pointer" onclick="location.href='${detailUrl}'">
      <td>
        <span class="td-link">${escHtml(t.tournament_code)}</span>
        <div style="font-size:11px;color:var(--color-text-muted)">${escHtml(t.title || '')}</div>
      </td>
      <td style="font-weight:600">₹${t.entry_fee}</td>
      <td>${statusChip(t.status)}</td>
      <td>${t.player_count ?? 0} <span style="color:var(--color-text-dim)">/ ${t.max_players ?? 50}</span></td>
      <td style="font-size:12px;color:var(--color-text-muted)">${fmtDateTime(t.scheduled_at)}</td>
      <td style="font-size:12px;color:var(--color-text-muted)">${escHtml(t.created_by_display_name || t.created_by_agent_id || '—')}</td>
    </tr>`;
  }).join('');

  renderPagination('tourn-pagination', total, _limit, _offset, off => {
    _offset = off;
    loadTournamentList();
  });
}

// ── Prize structure preview data (mirrors backend _ALL_PRIZE_CONFIGS) ─────────
// This is display-only. The backend is always the authoritative source for
// actual prize calculation. Values here must match prize_service.py exactly.
const _PRIZE_PREVIEW = {
  10:  { first: 200,  second: 60,   third: 20,  tiers: [[1,3,'₹3'],[4,7,'₹5'],[8,null,'₹7']] },
  20:  { first: 400,  second: 120,  third: 40,  tiers: [[1,3,'₹3'],[4,7,'₹5'],[8,null,'₹7']] },
  30:  { first: 600,  second: 180,  third: 60,  tiers: [[1,3,'₹3'],[4,7,'₹5'],[8,null,'₹7']] },
  40:  { first: 800,  second: 240,  third: 80,  tiers: [[1,3,'₹4'],[4,7,'₹5'],[8,null,'₹8']] },
  50:  { first: 1000, second: 300,  third: 100, tiers: [[1,3,'₹5'],[4,6,'₹7'],[7,null,'₹10']] },
  100: { first: 2000, second: 600,  third: 200, tiers: [[1,5,'₹10'],[6,null,'₹15']] },
};

function _renderPrizePreview(fee) {
  const previewBox = document.getElementById('create-prize-preview');
  const previewContent = document.getElementById('create-prize-preview-content');
  if (!previewBox || !previewContent) return;

  const cfg = _PRIZE_PREVIEW[parseInt(fee, 10)];
  if (!cfg) { previewBox.style.display = 'none'; return; }

  const tiersHtml = cfg.tiers.map(([from, to, reward]) => {
    const range = to !== null ? `Kills ${from}–${to}` : `Kills ${from}+`;
    return `<span style="margin-right:12px">${range}: <strong>${reward}/kill</strong></span>`;
  }).join('');

  previewContent.innerHTML = `
    <div style="display:flex;gap:16px;flex-wrap:wrap;margin-bottom:6px">
      <span>🥇 <strong>₹${cfg.first}</strong></span>
      <span>🥈 <strong>₹${cfg.second}</strong></span>
      <span>🥉 <strong>₹${cfg.third}</strong></span>
    </div>
    <div style="color:var(--color-text-dim)">${tiersHtml}</div>
    <div style="font-size:11px;margin-top:4px;color:var(--color-text-dim)">Progressive calculation. Kill rewards for Top 10 players.</div>`;
  previewBox.style.display = 'block';
}

// ── Create Tournament Modal ───────────────────────────────────────────────────
export function setupCreateTournamentModal() {
  const form = document.getElementById('create-tourn-form');
  if (!form) return;

  // Wire entry fee select → prize preview
  const feeSelect = form.querySelector('[name="entry_fee"]');
  if (feeSelect) {
    feeSelect.addEventListener('change', () => _renderPrizePreview(feeSelect.value));
  }

  form.addEventListener('submit', async e => {
    e.preventDefault();
    const btn = document.getElementById('btn-create-submit');
    btn.disabled = true;
    btn.textContent = 'Creating…';

    try {
      const scheduledDate = form.scheduled_date.value;
      const scheduledTime = form.scheduled_time.value;
      const scheduledAt   = new Date(`${scheduledDate}T${scheduledTime}:00`).toISOString();

      const payload = {
        title:       form.title.value.trim(),
        entry_fee:   parseFloat(form.entry_fee.value),
        scheduled_at: scheduledAt,
        max_players: parseInt(form.max_players.value, 10) || 50,
        min_players: parseInt(form.min_players.value, 10) || 2,
        game:        'free_fire',
        mode:        'solo',
      };

      // Step 1: create in DRAFT
      const created = await adminPost('/api/admin/tournaments', payload);
      const tournamentId = created.id;

      // Step 2: immediately open for registration (DRAFT → OPEN)
      // This makes the tournament visible to players right away.
      // If this fails the tournament still exists in DRAFT — admin can open it manually.
      try {
        btn.textContent = 'Opening…';
        await adminPost(`/api/admin/tournaments/${tournamentId}/open`, {});
        showToast('Tournament created and opened for registration!', 'success');
      } catch (openErr) {
        showToast(
          'Tournament created but could not be opened automatically. ' +
          'Open it manually from the tournament details page.',
          'warning'
        );
      }

      import('./admin-ui.js').then(({ closeModal }) => closeModal('modal-create-tourn'));
      form.reset();
      // Reset prize preview after form reset
      const previewBox = document.getElementById('create-prize-preview');
      if (previewBox) previewBox.style.display = 'none';
      _offset = 0;
      await loadTournamentList();
    } catch (err) {
      showToast(err.displayMessage || 'Create failed', 'error');
    } finally {
      btn.disabled = false;
      btn.textContent = 'Create & Open Tournament';
    }
  });
}

// ── Filter setup ──────────────────────────────────────────────────────────────
export function setupTournamentFilters() {
  document.getElementById('filter-status')?.addEventListener('change', e => {
    _filters.status = e.target.value;
    _offset = 0;
    loadTournamentList();
  });
  document.getElementById('filter-fee')?.addEventListener('change', e => {
    _filters.entry_fee = e.target.value;
    _offset = 0;
    loadTournamentList();
  });
  document.getElementById('btn-filter-reset')?.addEventListener('click', () => {
    document.getElementById('filter-status').value = '';
    document.getElementById('filter-fee').value = '';
    _filters = { status: '', entry_fee: '' };
    _offset = 0;
    loadTournamentList();
  });
}

// ── Tournament Detail ─────────────────────────────────────────────────────────
export async function loadTournamentDetail(id) {
  try {
    const [t, entriesData] = await Promise.all([
      adminGet(`/api/admin/tournaments/${id}`),
      adminGet(`/api/admin/tournaments/${id}/entries`, { limit: 60 }),
    ]);

    // Fetch prize config for this tournament's entry fee — non-fatal on miss
    let prizeConfig = null;
    if (t && t.entry_fee) {
      try {
        prizeConfig = await adminGet(
          `/api/admin/prize-configs/${encodeURIComponent(parseFloat(t.entry_fee))}`
        );
      } catch { /* Prize config not yet configured for this tier */ }
    }

    _renderDetail(t, prizeConfig);
    _renderEntries(entriesData.items || []);
  } catch (err) {
    document.getElementById('detail-content')?.classList.add('hidden');
    showToast(err.displayMessage || 'Failed to load tournament', 'error');
  }
}

function _renderDetail(t, prizeConfig = null) {
  const setEl = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val ?? '—'; };
  const setHtml = (id, val) => { const el = document.getElementById(id); if (el) el.innerHTML = val; };

  setEl('det-code',       t.tournament_code);
  setEl('det-title',      t.title);
  setEl('det-fee',        `₹${t.entry_fee}`);
  setEl('det-max',        t.max_players);
  setEl('det-min',        t.min_players);
  setEl('det-players',    `${t.player_count} / ${t.max_players}`);
  setEl('det-scheduled',  fmtDateTime(t.scheduled_at));
  setEl('det-creator',    t.created_by_display_name || t.created_by_agent_id || '—');
  setEl('det-created',    fmtDateTime(t.created_at));
  setHtml('det-status',   statusChip(t.status));

  // Room credentials — only show if released
  const roomSection = document.getElementById('room-section');
  if (roomSection) {
    if (t.room_id || t.room_password) {
      roomSection.style.display = 'block';
      setEl('det-room-id',   t.room_id || '(not set)');
      setEl('det-room-pass', t.room_password || '(not set)');
      setEl('det-room-released', t.room_released_at ? fmtDateTime(t.room_released_at) : '—');
    } else {
      roomSection.style.display = 'none';
    }
  }

  // Action button based on status
  _renderActionBtn(t);
  _renderCancelBtn(t);
  // Prize config card
  _renderPrizeConfig(prizeConfig);
}

function _renderActionBtn(t) {
  const container = document.getElementById('detail-action-area');
  if (!container) return;

  const transition = STATUS_TRANSITIONS[t.status];
  if (!transition) {
    container.innerHTML = '';
    return;
  }

  if (t.status === 'confirmed') {
    // Room release — special form
    container.innerHTML = `
      <div class="card" style="margin-top:16px">
        <div class="card-header"><span class="card-title">🔐 Release Room Credentials</span></div>
        <form id="room-release-form">
          <div class="form-row">
            <div class="form-group">
              <label class="form-label">Room ID</label>
              <input type="text" id="inp-room-id" class="form-input" placeholder="e.g. 123456789" required maxlength="50">
            </div>
            <div class="form-group">
              <label class="form-label">Room Password</label>
              <input type="text" id="inp-room-pass" class="form-input" placeholder="e.g. AURA50" required maxlength="50">
            </div>
          </div>
          <p style="font-size:12px;color:var(--color-warning);margin-bottom:12px">⚠ Credentials will be visible to all joined players after release.</p>
          <button type="submit" class="btn btn-warning" id="btn-release-room">🔓 Release Room to Players</button>
        </form>
      </div>`;
    document.getElementById('room-release-form').addEventListener('submit', async e => {
      e.preventDefault();
      if (!confirmAction('Release room credentials to all joined players?')) return;
      const btn = document.getElementById('btn-release-room');
      btn.disabled = true; btn.textContent = 'Releasing…';
      try {
        await adminPost(`/api/admin/tournaments/${t.id}/release-room`, {
          room_id:       document.getElementById('inp-room-id').value.trim(),
          room_password: document.getElementById('inp-room-pass').value.trim(),
        });
        showToast('Room released to players!', 'success');
        await loadTournamentDetail(t.id);
      } catch (err) {
        showToast(err.displayMessage || 'Release failed', 'error');
        btn.disabled = false; btn.textContent = '🔓 Release Room to Players';
      }
    });
  } else if (t.status === 'room_released') {
    // Room has been released — tournament is locked for cancellation/editing.
    // Only the Go Live transition is permitted from this state.
    const releasedAt = t.room_released_at ? ` at ${fmtDateTime(t.room_released_at)}` : '';
    container.innerHTML = `
      <div class="card" style="margin-top:16px;border-color:var(--color-warning)">
        <div class="card-header" style="background:rgba(255,170,0,.06)">
          <span class="card-title" style="color:var(--color-warning)">🔒 Room Released — Tournament Locked</span>
        </div>
        <p style="font-size:13px;color:var(--color-text-muted);margin-bottom:var(--space-4)">
          Room credentials were released to players${escHtml(releasedAt)}.
          The tournament is now <strong>locked</strong> — cancellation is no longer allowed.
          Players may continue joining until the tournament goes live.
        </p>
        <button class="btn btn-primary" id="btn-next-action">▶ ${escHtml(transition.label)}</button>
      </div>`;
    document.getElementById('btn-next-action').addEventListener('click', async () => {
      if (!confirmAction(`${transition.label}?`)) return;
      const btn = document.getElementById('btn-next-action');
      btn.disabled = true; btn.textContent = 'Processing…';
      try {
        await adminPost(`/api/admin/tournaments/${t.id}/${transition.action}`, {});
        showToast(`Done: ${transition.label}`, 'success');
        await loadTournamentDetail(t.id);
      } catch (err) {
        showToast(err.displayMessage || 'Action failed', 'error');
        btn.disabled = false; btn.textContent = `▶ ${transition.label}`;
      }
    });
  } else {
    container.innerHTML = `
      <button class="btn btn-primary" id="btn-next-action" style="margin-top:16px">
        ▶ ${escHtml(transition.label)}
      </button>`;
    document.getElementById('btn-next-action').addEventListener('click', async () => {
      if (!confirmAction(`${transition.label}?`)) return;
      const btn = document.getElementById('btn-next-action');
      btn.disabled = true; btn.textContent = 'Processing…';
      try {
        await adminPost(`/api/admin/tournaments/${t.id}/${transition.action}`, {});
        showToast(`Done: ${transition.label}`, 'success');
        await loadTournamentDetail(t.id);
      } catch (err) {
        showToast(err.displayMessage || 'Action failed', 'error');
        btn.disabled = false; btn.textContent = `▶ ${transition.label}`;
      }
    });
  }
}

function _renderCancelBtn(t) {
  const btn = document.getElementById('btn-cancel-tourn');
  if (!btn) return;
  // Cancel is only available before room release (open or confirmed).
  // Once room credentials are released the tournament is locked — the backend
  // will also reject any attempt, but we hide the button to avoid confusion.
  const cancellable = ['open', 'confirmed'].includes(t.status);
  btn.style.display = cancellable ? 'inline-flex' : 'none';
  btn.onclick = () => {
    const reason = prompt('Cancellation reason (required):');
    if (!reason || !reason.trim()) return;
    if (!confirmAction('Cancel tournament and refund all players?')) return;
    adminPost(`/api/admin/tournaments/${t.id}/cancel`, { reason: reason.trim() })
      .then(() => { showToast('Tournament cancelled and refunds issued', 'success'); loadTournamentDetail(t.id); })
      .catch(err => showToast(err.displayMessage || 'Cancel failed', 'error'));
  };
}

function _renderEntries(entries) {
  const tbody = document.getElementById('entries-tbody');
  if (!tbody) return;
  if (!entries.length) {
    tbody.innerHTML = emptyRow(7, 'No players joined yet.');
    return;
  }
  tbody.innerHTML = entries.map(e => {
    // PLAYER  = immutable login username (never ff_name)
    // FF NAME = player's Free Fire gaming name (ff_name) — separate field
    // FF UID  = numeric Free Fire account identifier (ff_uid) — separate field
    const ffNameCell = e.ff_name
      ? escHtml(e.ff_name)
      : `<span style="color:var(--color-text-muted);font-style:italic">—</span>`;
    return `
    <tr>
      <td style="font-weight:700;color:var(--color-blue)">#${e.sheet_number}</td>
      <td style="font-weight:600">${escHtml(e.username)}</td>
      <td>${ffNameCell}</td>
      <td class="td-mono">${escHtml(e.ff_uid)}</td>
      <td>₹${e.entry_fee || e.entry_fee_snapshot}</td>
      <td>${statusChip(e.status)}</td>
      <td style="font-size:12px;color:var(--color-text-muted)">${fmtDateTime(e.joined_at)}</td>
    </tr>`;
  }).join('');
}

// ── Prize Config Display ──────────────────────────────────────────────────────
function _renderPrizeConfig(cfg) {
  const card    = document.getElementById('prize-config-card');
  const content = document.getElementById('prize-config-content');
  const badge   = document.getElementById('prize-config-status-badge');
  if (!card || !content) return;

  if (!cfg) {
    card.style.display = 'none';
    return;
  }

  card.style.display = 'block';

  // Active/inactive badge
  if (badge) {
    badge.innerHTML = cfg.is_active
      ? '<span class="status-chip chip-active" style="font-size:11px">Active</span>'
      : '<span class="status-chip chip-cancelled" style="font-size:11px">Inactive</span>';
  }

  // Placement prizes
  let html = `
    <div class="detail-grid" style="margin-bottom:16px">
      <div class="detail-item">
        <div class="detail-label">🥇 1st Place</div>
        <div class="detail-value" style="color:var(--color-warning);font-weight:700">₹${parseFloat(cfg.first_place_prize).toFixed(2)}</div>
      </div>
      <div class="detail-item">
        <div class="detail-label">🥈 2nd Place</div>
        <div class="detail-value">₹${parseFloat(cfg.second_place_prize).toFixed(2)}</div>
      </div>
      <div class="detail-item">
        <div class="detail-label">🥉 3rd Place</div>
        <div class="detail-value">₹${parseFloat(cfg.third_place_prize).toFixed(2)}</div>
      </div>
      <div class="detail-item">
        <div class="detail-label">Kill Reward Eligibility</div>
        <div class="detail-value">Top ${cfg.kill_reward_top_n || 10} players</div>
      </div>
    </div>`;

  // Kill reward tiers
  if (cfg.kill_reward_tiers && cfg.kill_reward_tiers.length > 0) {
    html += `<div style="font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;color:var(--color-text-muted);margin-bottom:8px">⚔ Kill Reward Tiers (Progressive)</div>`;
    html += `<table style="width:100%;font-size:13px;border-collapse:collapse">
      <thead><tr style="border-bottom:1px solid var(--color-border)">
        <th style="text-align:left;padding:6px 8px;color:var(--color-text-muted);font-weight:600">Kill Range</th>
        <th style="text-align:right;padding:6px 8px;color:var(--color-text-muted);font-weight:600">Reward / Kill</th>
      </tr></thead><tbody>`;
    for (const tier of cfg.kill_reward_tiers) {
      const range = tier.to_kill != null
        ? `Kills ${tier.from_kill} – ${tier.to_kill}`
        : `Kills ${tier.from_kill}+`;
      html += `<tr style="border-bottom:1px solid var(--color-border)">
        <td style="padding:6px 8px">${escHtml(range)}</td>
        <td style="padding:6px 8px;text-align:right;font-family:var(--font-mono);font-weight:700">₹${parseFloat(tier.reward_per_kill).toFixed(2)}</td>
      </tr>`;
    }
    html += `</tbody></table>`;
    html += `<div style="font-size:11px;color:var(--color-text-dim);margin-top:8px">Calculation is progressive — each bracket only covers its own kills, not all kills.</div>`;
  }

  content.innerHTML = html;
}
