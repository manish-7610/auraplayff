/**
 * AuraPlayFF — Help & Support (Player)
 *
 * APIs:
 *   POST /api/support            — create support request
 *   GET  /api/support            — list my support requests
 *   GET  /api/support/{id}       — view single request
 */
import { apiGet, apiPost } from './api.js';
import {
  escapeHtml,
  formatDateTime,
  setButtonLoading,
  toast,
} from './ui.js';
import { listTournaments } from './tournaments.js';

// ── API helpers ───────────────────────────────────────────────────────────────

function createSupportRequest(body) {
  return apiPost('/api/support', body);
}

function listMySupportRequests(params = {}) {
  return apiGet('/api/support', params);
}

// ── Status helpers ────────────────────────────────────────────────────────────

const STATUS_LABELS = {
  pending:     '⏳ Pending',
  in_progress: '🔄 In Progress',
  resolved:    '✅ Resolved',
};

const STATUS_CLASSES = {
  pending:     'status-pending',
  in_progress: 'status-in_progress',
  resolved:    'status-resolved',
};

function statusLabel(s) {
  return STATUS_LABELS[s] || s;
}

function statusClass(s) {
  return STATUS_CLASSES[s] || '';
}

// ── Populate tournament dropdown ──────────────────────────────────────────────

async function loadTournamentDropdown() {
  const sel = document.getElementById('field-tournament');
  if (!sel) return;

  try {
    // Fetch all tournaments and filter to those the player joined
    const data = await listTournaments({ status: 'all', limit: 100 });
    const all = data.items || data.tournaments || [];
    const joined = all.filter(t => t.already_joined);

    if (!joined.length) {
      sel.innerHTML =
        '<option value="">— General / Other Issue —</option>';
      return;
    }

    // Sort: most recent first
    joined.sort((a, b) => new Date(b.scheduled_at) - new Date(a.scheduled_at));

    let opts = '<option value="">— General / Other Issue —</option>';
    for (const t of joined) {
      const id  = t.id || t.tournament_id;
      const fee = t.entry_fee != null ? ` · ₹${t.entry_fee}` : '';
      const date = t.scheduled_at
        ? ' · ' + new Date(t.scheduled_at).toLocaleDateString('en-IN', { day: 'numeric', month: 'short' })
        : '';
      const label = `[${escapeHtml(t.tournament_code || '')}] ${escapeHtml(t.title || '')}${fee}${date}`;
      opts += `<option value="${escapeHtml(id)}">${label}</option>`;
    }
    sel.innerHTML = opts;
  } catch {
    sel.innerHTML = '<option value="">— General / Other Issue —</option>';
  }
}

// ── Render request list ───────────────────────────────────────────────────────

function renderRequests(requests) {
  const listEl    = document.getElementById('requests-list');
  const emptyEl   = document.getElementById('requests-empty');
  const loadingEl = document.getElementById('requests-loading');

  loadingEl.classList.add('hidden');

  if (!requests || !requests.length) {
    listEl.classList.add('hidden');
    emptyEl.classList.remove('hidden');
    return;
  }

  emptyEl.classList.add('hidden');
  listEl.classList.remove('hidden');

  listEl.innerHTML = requests.map(req => {
    const tournLine = req.tournament_title
      ? `<span>🏆 ${escapeHtml(req.tournament_title)}</span> · `
      : `<span>General / Other Issue</span> · `;

    const shortMsg = escapeHtml(req.message.length > 120
      ? req.message.slice(0, 120) + '…'
      : req.message);

    const replyHtml = req.admin_response
      ? `<div class="req-reply">
           <div class="req-reply-label">Support Response</div>
           <div>${escapeHtml(req.admin_response)}</div>
         </div>`
      : '';

    return `
      <div class="req-card">
        <div class="req-card-body">
          <div style="display:flex;align-items:center;justify-content:space-between;gap:var(--space-3);flex-wrap:wrap">
            <div style="font-size:var(--text-xs);font-family:var(--font-mono);color:var(--color-text-muted)">
              #${escapeHtml(String(req.id).slice(0, 8))}…
            </div>
            <span class="${statusClass(req.status)}" style="font-size:var(--text-xs);font-weight:600">
              ${statusLabel(req.status)}
            </span>
          </div>
          <div class="req-meta">
            ${tournLine}${formatDateTime(req.created_at)}
          </div>
          <div class="req-msg">${shortMsg}</div>
          ${replyHtml}
        </div>
      </div>`;
  }).join('');
}

// ── Load requests ─────────────────────────────────────────────────────────────

async function loadRequests() {
  const loadingEl = document.getElementById('requests-loading');
  const listEl    = document.getElementById('requests-list');
  const emptyEl   = document.getElementById('requests-empty');

  loadingEl.classList.remove('hidden');
  listEl.classList.add('hidden');
  emptyEl.classList.add('hidden');

  try {
    const data = await listMySupportRequests({ limit: 50 });
    renderRequests(data.requests || []);
  } catch (err) {
    loadingEl.classList.add('hidden');
    listEl.classList.add('hidden');
    emptyEl.classList.remove('hidden');
    emptyEl.innerHTML = `
      <div style="font-size:36px;margin-bottom:var(--space-3)">⚠</div>
      <div>${escapeHtml(err.displayMessage || 'Failed to load requests.')}</div>`;
  }
}

// ── Form validation ───────────────────────────────────────────────────────────

function showErr(id, msg) {
  const el = document.getElementById(id);
  if (el) { el.textContent = msg; el.classList.remove('hidden'); }
}

function clearErrs() {
  ['error-message', 'error-contact', 'error-general'].forEach(id => {
    const el = document.getElementById(id);
    if (el) { el.textContent = ''; el.classList.add('hidden'); }
  });
}

function validate(msg, wa, ig) {
  let ok = true;
  if (!msg || msg.trim().length < 10) {
    showErr('error-message', 'Please describe your problem (at least 10 characters).');
    ok = false;
  }
  if (!wa.trim() && !ig.trim()) {
    showErr('error-contact', 'Please provide at least one contact method: WhatsApp number or Instagram ID.');
    ok = false;
  }
  return ok;
}

// ── Main init ─────────────────────────────────────────────────────────────────

export async function initSupportPage() {
  // Load tournament dropdown and existing requests in parallel
  await Promise.allSettled([
    loadTournamentDropdown(),
    loadRequests(),
  ]);

  // Refresh button
  document.getElementById('btn-refresh')?.addEventListener('click', loadRequests);

  // Form submit
  const form = document.getElementById('support-form');
  if (!form) return;

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    clearErrs();

    const tournamentId = document.getElementById('field-tournament')?.value || null;
    const message      = document.getElementById('field-message')?.value || '';
    const whatsapp     = document.getElementById('field-whatsapp')?.value || '';
    const instagram    = document.getElementById('field-instagram')?.value || '';

    if (!validate(message, whatsapp, instagram)) return;

    const btn = document.getElementById('btn-submit');
    setButtonLoading(btn, true);

    try {
      const body = {
        message: message.trim(),
        whatsapp_number: whatsapp.trim() || null,
        instagram_id:    instagram.trim() || null,
      };
      if (tournamentId) body.tournament_id = tournamentId;

      await createSupportRequest(body);

      // Show success message
      document.getElementById('form-success')?.classList.remove('hidden');
      form.reset();
      // Reset select to default
      const sel = document.getElementById('field-tournament');
      if (sel) sel.selectedIndex = 0;

      // Reload requests list
      await loadRequests();
      // Scroll to success message
      document.getElementById('form-success')?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });

    } catch (err) {
      const msg = err.displayMessage || err.message || 'Failed to submit. Please try again.';
      showErr('error-general', msg);
      setButtonLoading(btn, false);
    }
  });
}
