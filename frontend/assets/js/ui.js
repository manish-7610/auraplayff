/**
 * AuraPlayFF — UI Utility Module
 * Toast notifications, modals, skeletons, status formatting, helpers.
 */

// ── Toast ─────────────────────────────────────────────────────────────────────

let _toastContainer = null;

function _getToastContainer() {
  if (!_toastContainer) {
    _toastContainer = document.createElement('div');
    _toastContainer.className = 'toast-container';
    document.body.appendChild(_toastContainer);
  }
  return _toastContainer;
}

/**
 * Show a toast notification.
 * @param {string} message
 * @param {'success'|'error'|'warning'|'info'} type
 * @param {number} duration  ms before auto-dismiss (0 = manual)
 */
export function toast(message, type = 'info', duration = 4000) {
  const icons = { success: '✓', error: '✕', warning: '⚠', info: 'ℹ' };
  const container = _getToastContainer();

  const el = document.createElement('div');
  el.className = `toast ${type}`;
  el.innerHTML = `<span>${icons[type]}</span><span class="toast-msg">${escapeHtml(message)}</span>`;
  container.appendChild(el);

  const dismiss = () => {
    el.classList.add('exiting');
    el.addEventListener('animationend', () => el.remove(), { once: true });
  };

  if (duration > 0) setTimeout(dismiss, duration);
  el.addEventListener('click', dismiss);
  return dismiss;
}

export const toastSuccess = (msg, d) => toast(msg, 'success', d);
export const toastError   = (msg, d) => toast(msg, 'error', d);
export const toastWarning = (msg, d) => toast(msg, 'warning', d);
export const toastInfo    = (msg, d) => toast(msg, 'info', d);

// ── Modal ─────────────────────────────────────────────────────────────────────

/**
 * Open a modal overlay element by its ID.
 */
export function openModal(id) {
  const el = document.getElementById(id);
  if (el) el.classList.add('open');
}

/**
 * Close a modal overlay element by its ID.
 */
export function closeModal(id) {
  const el = document.getElementById(id);
  if (el) el.classList.remove('open');
}

/**
 * Wire close button and backdrop click for a modal.
 */
export function initModal(overlayId) {
  const overlay = document.getElementById(overlayId);
  if (!overlay) return;

  overlay.addEventListener('click', (e) => {
    if (e.target === overlay) closeModal(overlayId);
  });

  overlay.querySelectorAll('[data-close-modal]').forEach(btn => {
    btn.addEventListener('click', () => closeModal(overlayId));
  });
}

// ── Confirm dialog ────────────────────────────────────────────────────────────

/**
 * Show a simple confirm modal and return a Promise<boolean>.
 */
export function confirm(message, { title = 'Confirm', confirmText = 'Confirm', cancelText = 'Cancel', danger = false } = {}) {
  return new Promise(resolve => {
    // Remove any existing confirm modal
    document.getElementById('_confirm-modal')?.remove();

    const overlay = document.createElement('div');
    overlay.id = '_confirm-modal';
    overlay.className = 'modal-overlay';
    overlay.innerHTML = `
      <div class="modal">
        <div class="modal-header">
          <h3 class="modal-title">${escapeHtml(title)}</h3>
          <button class="modal-close" data-action="cancel">✕</button>
        </div>
        <p style="color:var(--color-text-muted);font-size:var(--text-sm);margin-bottom:var(--space-6);line-height:1.6">${escapeHtml(message)}</p>
        <div style="display:flex;gap:var(--space-3);justify-content:flex-end">
          <button class="btn btn-outline" data-action="cancel">${escapeHtml(cancelText)}</button>
          <button class="btn ${danger ? 'btn-danger' : 'btn-primary'}" data-action="confirm">${escapeHtml(confirmText)}</button>
        </div>
      </div>`;

    document.body.appendChild(overlay);
    requestAnimationFrame(() => overlay.classList.add('open'));

    const finish = (result) => {
      overlay.classList.remove('open');
      overlay.addEventListener('transitionend', () => overlay.remove(), { once: true });
      resolve(result);
    };

    overlay.querySelectorAll('[data-action]').forEach(btn => {
      btn.addEventListener('click', () => finish(btn.dataset.action === 'confirm'));
    });
    overlay.addEventListener('click', e => { if (e.target === overlay) finish(false); });
  });
}

// ── Button loading state ──────────────────────────────────────────────────────

export function setButtonLoading(btn, loading) {
  if (!btn) return;
  if (loading) {
    btn._origText = btn.innerHTML;
    btn.classList.add('btn-loading');
    btn.disabled = true;
  } else {
    btn.classList.remove('btn-loading');
    btn.disabled = false;
    if (btn._origText !== undefined) btn.innerHTML = btn._origText;
  }
}

// ── Status badge ──────────────────────────────────────────────────────────────

const STATUS_LABELS = {
  open:          'Open',
  confirmed:     'Confirmed',
  room_released: 'Room Released',
  live:          'Live',
  result_entry:  'Result Entry',
  completed:     'Completed',
  cancelled:     'Cancelled',
  pending:       'Pending',
  paid:          'Paid',
  approved:      'Approved',
  rejected:      'Rejected',
  processing:    'Processing',
  active:        'Active',
  refunded:      'Refunded',
  pending_manual_verification: 'Under Review',
  finalized:     'Finalized',
  verified:      'Verified',
  entered:       'Entered',
  draft:         'Draft',
};

export function statusBadge(status) {
  const label = STATUS_LABELS[status] || status;
  return `<span class="badge badge-${status}">${label}</span>`;
}

// ── Currency formatting ───────────────────────────────────────────────────────

export function formatCurrency(value, symbol = '₹') {
  const num = parseFloat(value);
  if (isNaN(num)) return `${symbol}0.00`;
  return `${symbol}${num.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

export function formatAmount(value) { return formatCurrency(value, '₹'); }

// ── Date/time formatting ──────────────────────────────────────────────────────

export function formatDateTime(iso) {
  if (!iso) return '—';
  return new Date(iso).toLocaleString('en-IN', {
    day: '2-digit', month: 'short', year: 'numeric',
    hour: '2-digit', minute: '2-digit', hour12: true,
    timeZone: 'Asia/Kolkata',
  });
}

export function formatDate(iso) {
  if (!iso) return '—';
  return new Date(iso).toLocaleDateString('en-IN', {
    day: '2-digit', month: 'short', year: 'numeric',
    timeZone: 'Asia/Kolkata',
  });
}

export function timeAgo(iso) {
  if (!iso) return '';
  const diff = Date.now() - new Date(iso).getTime();
  const s = Math.floor(diff / 1000);
  if (s < 60)  return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s/60)}m ago`;
  if (s < 86400) return `${Math.floor(s/3600)}h ago`;
  return `${Math.floor(s/86400)}d ago`;
}

// ── Countdown ─────────────────────────────────────────────────────────────────

/**
 * Render a live countdown into an element.
 * @param {HTMLElement} el
 * @param {string|Date} targetIso  — ISO datetime string (UTC)
 * @returns {Function} stop  — call to stop the interval
 */
export function startCountdown(el, targetIso) {
  const target = new Date(targetIso).getTime();

  // Declare timer before render() so the closure never hits the TDZ.
  // (render() is called immediately below; if diff <= 0 on that first
  //  call it must be able to clearInterval — timer must already exist.)
  let timer;

  const render = () => {
    const diff = target - Date.now();
    if (diff <= 0) {
      el.textContent = 'Started';
      clearInterval(timer);
      return;
    }
    const h = Math.floor(diff / 3600000);
    const m = Math.floor((diff % 3600000) / 60000);
    const s = Math.floor((diff % 60000) / 1000);
    const pad = n => String(n).padStart(2, '0');
    el.innerHTML = `
      <span class="countdown">
        <span class="countdown-unit"><span class="countdown-val">${pad(h)}</span><span class="countdown-lbl">hr</span></span>
        <span style="color:var(--color-text-dim)">:</span>
        <span class="countdown-unit"><span class="countdown-val">${pad(m)}</span><span class="countdown-lbl">min</span></span>
        <span style="color:var(--color-text-dim)">:</span>
        <span class="countdown-unit"><span class="countdown-val">${pad(s)}</span><span class="countdown-lbl">sec</span></span>
      </span>`;
  };

  render();
  timer = setInterval(render, 1000);
  return () => clearInterval(timer);
}

// ── Skeleton loaders ──────────────────────────────────────────────────────────

export function skeletonCard(count = 3) {
  return Array.from({ length: count }, () => `
    <div class="card" style="padding:var(--space-5)">
      <span class="skeleton skeleton-line medium" style="margin-bottom:var(--space-3)"></span>
      <span class="skeleton skeleton-line short"></span>
      <span class="skeleton skeleton-line long" style="margin-top:var(--space-3)"></span>
      <span class="skeleton skeleton-line medium" style="margin-top:var(--space-2)"></span>
    </div>`).join('');
}

export function skeletonRow(count = 5) {
  return Array.from({ length: count }, () => `
    <tr>
      <td><span class="skeleton skeleton-line short"></span></td>
      <td><span class="skeleton skeleton-line medium"></span></td>
      <td><span class="skeleton skeleton-line short"></span></td>
      <td><span class="skeleton skeleton-line medium"></span></td>
    </tr>`).join('');
}

// ── Empty states ──────────────────────────────────────────────────────────────

export function emptyState({ icon = '📭', title, desc, action = '' }) {
  return `
    <div class="empty-state">
      <div class="empty-state-icon">${icon}</div>
      <div class="empty-state-title">${escapeHtml(title)}</div>
      <div class="empty-state-desc">${escapeHtml(desc)}</div>
      ${action}
    </div>`;
}

// ── Error state ───────────────────────────────────────────────────────────────

export function errorState(message, retryFn = null) {
  const id = 'retry-' + Math.random().toString(36).slice(2);
  const html = `
    <div class="empty-state">
      <div class="empty-state-icon">⚠️</div>
      <div class="empty-state-title">Something went wrong</div>
      <div class="empty-state-desc">${escapeHtml(message)}</div>
      ${retryFn ? `<button id="${id}" class="btn btn-outline">Retry</button>` : ''}
    </div>`;
  if (retryFn) {
    setTimeout(() => document.getElementById(id)?.addEventListener('click', retryFn), 0);
  }
  return html;
}

// ── Form validation helpers ───────────────────────────────────────────────────

export function showFieldError(inputEl, message) {
  clearFieldError(inputEl);
  inputEl.classList.add('error');
  const err = document.createElement('div');
  err.className = 'form-error';
  err.textContent = message;
  inputEl.parentNode.appendChild(err);
}

export function clearFieldError(inputEl) {
  inputEl.classList.remove('error');
  const prev = inputEl.parentNode.querySelector('.form-error');
  if (prev) prev.remove();
}

export function clearAllErrors(formEl) {
  formEl.querySelectorAll('.form-error').forEach(e => e.remove());
  formEl.querySelectorAll('.error').forEach(e => e.classList.remove('error'));
}

// ── Copy to clipboard ─────────────────────────────────────────────────────────

export async function copyToClipboard(text, btn = null) {
  try {
    await navigator.clipboard.writeText(text);
    if (btn) {
      const orig = btn.textContent;
      btn.textContent = 'Copied!';
      btn.classList.add('copied');
      setTimeout(() => { btn.textContent = orig; btn.classList.remove('copied'); }, 2000);
    }
    return true;
  } catch {
    return false;
  }
}

// ── Pagination ────────────────────────────────────────────────────────────────

/**
 * Render pagination controls.
 * @param {number} total
 * @param {number} limit
 * @param {number} offset
 * @param {Function} onPage  — callback(newOffset)
 */
export function renderPagination(container, total, limit, offset, onPage) {
  if (total <= limit) { container.innerHTML = ''; return; }

  const currentPage = Math.floor(offset / limit) + 1;
  const totalPages  = Math.ceil(total / limit);

  let html = `<div class="pagination">`;
  html += `<button class="pagination-btn" ${currentPage === 1 ? 'disabled' : ''} data-page="${currentPage-1}">‹</button>`;

  for (let p = 1; p <= totalPages; p++) {
    if (totalPages > 7 && Math.abs(p - currentPage) > 2 && p !== 1 && p !== totalPages) {
      if (p === 2 || p === totalPages - 1) html += `<span class="pagination-btn" style="pointer-events:none">…</span>`;
      continue;
    }
    html += `<button class="pagination-btn ${p === currentPage ? 'active' : ''}" data-page="${p}">${p}</button>`;
  }

  html += `<button class="pagination-btn" ${currentPage === totalPages ? 'disabled' : ''} data-page="${currentPage+1}">›</button>`;
  html += `</div>`;

  container.innerHTML = html;
  container.querySelectorAll('[data-page]').forEach(btn => {
    btn.addEventListener('click', () => {
      const page = parseInt(btn.dataset.page, 10);
      onPage((page - 1) * limit);
    });
  });
}

// ── HTML escape ───────────────────────────────────────────────────────────────

export function escapeHtml(str) {
  if (!str) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

// ── Set page title ────────────────────────────────────────────────────────────

export function setPageTitle(title) {
  document.title = title ? `${title} — ${CONFIG.APP_NAME}` : CONFIG.APP_NAME;
}

// ── Transaction direction icon ────────────────────────────────────────────────

export function txDirectionIcon(direction) {
  return direction === 'credit'
    ? '<span style="color:var(--color-success)">▲</span>'
    : '<span style="color:var(--color-danger)">▼</span>';
}

// ── Notification badge update ─────────────────────────────────────────────────

export function updateNotifBadges(count) {
  document.querySelectorAll('[data-notif-badge]').forEach(el => {
    if (count > 0) {
      el.textContent = count > 99 ? '99+' : count;
      el.classList.remove('hidden');
    } else {
      el.classList.add('hidden');
    }
  });
}
