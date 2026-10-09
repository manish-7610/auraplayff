/**
 * AuraPlayFF Admin — Help & Support Management
 *
 * APIs:
 *   GET   /api/admin/support              — list all requests
 *   GET   /api/admin/support/{id}         — view single request
 *   PATCH /api/admin/support/{id}         — update status / add response
 */
import { adminGet, adminPatch } from './admin-api.js';
import {
  showToast,
  statusChip,
  fmtDateTime,
  escHtml,
  renderPagination,
  showTableSkeleton,
  emptyRow,
  openModal,
  closeModal,
} from './admin-ui.js';

let _offset  = 0;
const _limit = 30;
let _filterStatus = '';
let _currentRequestId = null;

// ── Status labels ─────────────────────────────────────────────────────────────

const STATUS_LABELS = {
  pending:     '⏳ Pending',
  in_progress: '🔄 In Progress',
  resolved:    '✅ Resolved',
};

function statusLabel(s) {
  return STATUS_LABELS[s] || s || '—';
}

// ── Filters ───────────────────────────────────────────────────────────────────

export function setupSupportFilters() {
  document.getElementById('filter-support-status')
    ?.addEventListener('change', e => {
      _filterStatus = e.target.value;
      _offset = 0;
      loadSupportList();
    });
}

// ── List ──────────────────────────────────────────────────────────────────────

export async function loadSupportList() {
  showTableSkeleton('support-tbody', 7, 8);
  try {
    const params = { limit: _limit, offset: _offset };
    if (_filterStatus) params.status = _filterStatus;
    const data = await adminGet('/api/admin/support', params);
    const items = data.requests || [];
    const total = data.total ?? items.length;
    _renderTable(items, total);
  } catch (err) {
    document.getElementById('support-tbody').innerHTML =
      emptyRow(7, '⚠ ' + (err.displayMessage || 'Failed to load support requests.'));
  }
}

function _renderTable(items, total) {
  const tbody = document.getElementById('support-tbody');
  if (!tbody) return;

  if (!items.length) {
    tbody.innerHTML = emptyRow(7, 'No support requests found.');
    renderPagination('support-pagination', 0, _limit, 0, () => {});
    return;
  }

  tbody.innerHTML = items.map(req => {
    const shortId = escHtml(String(req.id).slice(0, 8)) + '…';
    const tournCol = req.tournament_title
      ? escHtml(req.tournament_title)
      : '<span style="color:var(--color-text-muted)">General</span>';
    const preview = escHtml(
      req.message && req.message.length > 60
        ? req.message.slice(0, 60) + '…'
        : (req.message || '—')
    );
    return `<tr>
      <td class="td-mono" style="font-size:11px">${shortId}</td>
      <td><strong>${escHtml(req.username || '—')}</strong></td>
      <td style="font-size:12px">${tournCol}</td>
      <td style="font-size:12px;color:var(--color-text-muted)">${preview}</td>
      <td>${statusChip(req.status || 'pending')}</td>
      <td style="font-size:12px;color:var(--color-text-muted)">${fmtDateTime(req.created_at)}</td>
      <td class="td-actions">
        <button class="btn btn-ghost btn-sm" data-action="view" data-id="${escHtml(req.id)}">View</button>
      </td>
    </tr>`;
  }).join('');

  tbody.querySelectorAll('[data-action="view"]').forEach(btn => {
    btn.addEventListener('click', () => _openDetail(btn.dataset.id));
  });

  renderPagination('support-pagination', total, _limit, _offset, off => {
    _offset = off;
    loadSupportList();
  });
}

// ── Detail modal ──────────────────────────────────────────────────────────────

async function _openDetail(requestId) {
  _currentRequestId = requestId;
  openModal('modal-support');
  _resetUpdateForm();

  const bodyEl = document.getElementById('modal-support-body');
  bodyEl.innerHTML = '<div style="text-align:center;padding:40px"><div class="loading-spinner"></div></div>';

  try {
    const req = await adminGet(`/api/admin/support/${requestId}`);
    _renderDetail(req);
    _prefillUpdateForm(req);
  } catch (err) {
    bodyEl.innerHTML = `<p style="color:var(--color-danger);padding:var(--space-5)">${escHtml(err.displayMessage || 'Failed to load request.')}</p>`;
  }
}

function _renderDetail(req) {
  const bodyEl = document.getElementById('modal-support-body');

  const tournLine = req.tournament_title
    ? `${escHtml(req.tournament_title)} <span style="color:var(--color-text-muted);font-size:11px">(${escHtml(String(req.tournament_id || '').slice(0, 8))}…)</span>`
    : '<span style="color:var(--color-text-muted)">General / Other Issue</span>';

  const contactRows = [];
  if (req.whatsapp_number) {
    contactRows.push(`
      <div class="detail-item">
        <div class="detail-label">WhatsApp</div>
        <div class="detail-value">${escHtml(req.whatsapp_number)}</div>
      </div>`);
  }
  if (req.instagram_id) {
    contactRows.push(`
      <div class="detail-item">
        <div class="detail-label">Instagram</div>
        <div class="detail-value">@${escHtml(req.instagram_id)}</div>
      </div>`);
  }

  const replySection = req.admin_response
    ? `<div style="margin-top:var(--space-4);padding:var(--space-4);background:var(--color-bg-3);border-left:3px solid var(--color-accent);border-radius:0 var(--radius-sm) var(--radius-sm) 0">
         <div style="font-size:var(--text-xs);font-weight:600;color:var(--color-accent);margin-bottom:var(--space-2);text-transform:uppercase;letter-spacing:.3px">Support Response</div>
         <div style="font-size:var(--text-sm);white-space:pre-wrap;word-break:break-word">${escHtml(req.admin_response)}</div>
       </div>`
    : '';

  bodyEl.innerHTML = `
    <div style="padding:var(--space-5)">
      <div class="detail-grid" style="grid-template-columns:1fr 1fr;gap:var(--space-3);margin-bottom:var(--space-4)">
        <div class="detail-item">
          <div class="detail-label">Username</div>
          <div class="detail-value"><strong>${escHtml(req.username || '—')}</strong></div>
        </div>
        <div class="detail-item">
          <div class="detail-label">Status</div>
          <div class="detail-value">${statusChip(req.status || 'pending')}</div>
        </div>
        <div class="detail-item">
          <div class="detail-label">Tournament</div>
          <div class="detail-value" style="font-size:12px">${tournLine}</div>
        </div>
        <div class="detail-item">
          <div class="detail-label">Submitted</div>
          <div class="detail-value" style="font-size:12px">${fmtDateTime(req.created_at)}</div>
        </div>
        ${contactRows.join('')}
        ${req.resolved_at ? `
        <div class="detail-item">
          <div class="detail-label">Resolved At</div>
          <div class="detail-value" style="font-size:12px">${fmtDateTime(req.resolved_at)}</div>
        </div>` : ''}
      </div>

      <div style="margin-bottom:var(--space-3)">
        <div style="font-size:var(--text-xs);font-weight:600;color:var(--color-text-muted);text-transform:uppercase;letter-spacing:.3px;margin-bottom:var(--space-2)">Problem Description</div>
        <div style="font-size:var(--text-sm);white-space:pre-wrap;word-break:break-word;padding:var(--space-4);background:var(--color-bg-3);border-radius:var(--radius-sm);">${escHtml(req.message || '—')}</div>
      </div>

      ${replySection}

      <div style="margin-top:var(--space-5)">
        <button class="btn btn-primary btn-sm" id="btn-show-update-form">
          ✏ Update Status / Add Response
        </button>
      </div>
    </div>`;

  document.getElementById('btn-show-update-form')?.addEventListener('click', () => {
    const form = document.getElementById('modal-update-form');
    if (form) {
      form.style.display = 'block';
      document.getElementById('btn-show-update-form').style.display = 'none';
    }
  });
}

// ── Update form ───────────────────────────────────────────────────────────────

function _prefillUpdateForm(req) {
  const statusSel = document.getElementById('update-status');
  const responseTA = document.getElementById('update-response');
  if (statusSel) statusSel.value = req.status || 'pending';
  if (responseTA) responseTA.value = req.admin_response || '';

  const errEl = document.getElementById('update-error');
  if (errEl) errEl.classList.add('hidden');
}

function _resetUpdateForm() {
  const statusSel = document.getElementById('update-status');
  const responseTA = document.getElementById('update-response');
  const form = document.getElementById('modal-update-form');
  if (statusSel)  statusSel.value   = '';
  if (responseTA) responseTA.value  = '';
  if (form)       form.style.display = 'none';

  const errEl = document.getElementById('update-error');
  if (errEl) errEl.classList.add('hidden');
}

// Wire update form buttons once (uses event delegation-style one-time setup)
let _updateFormWired = false;
export function wireUpdateForm() {
  if (_updateFormWired) return;
  _updateFormWired = true;

  document.getElementById('btn-update-submit')?.addEventListener('click', async () => {
    if (!_currentRequestId) return;

    const statusVal   = document.getElementById('update-status')?.value || null;
    const responseVal = document.getElementById('update-response')?.value?.trim() || null;

    const errEl = document.getElementById('update-error');

    if (!statusVal && !responseVal) {
      if (errEl) { errEl.textContent = 'Please update the status or add a response.'; errEl.classList.remove('hidden'); }
      return;
    }

    const body = {};
    if (statusVal)   body.status         = statusVal;
    if (responseVal) body.admin_response = responseVal;

    const btn = document.getElementById('btn-update-submit');
    const origText = btn.textContent;
    btn.disabled = true;
    btn.textContent = 'Saving…';

    try {
      await adminPatch(`/api/admin/support/${_currentRequestId}`, body);
      showToast('Support request updated.', 'success');
      closeModal('modal-support');
      await loadSupportList();
    } catch (err) {
      if (errEl) { errEl.textContent = err.displayMessage || 'Update failed.'; errEl.classList.remove('hidden'); }
    } finally {
      btn.disabled = false;
      btn.textContent = origText;
    }
  });

  document.getElementById('btn-update-cancel')?.addEventListener('click', () => {
    const form = document.getElementById('modal-update-form');
    if (form) form.style.display = 'none';
    const showBtn = document.getElementById('btn-show-update-form');
    if (showBtn) showBtn.style.display = '';
    const errEl = document.getElementById('update-error');
    if (errEl) errEl.classList.add('hidden');
  });
}
