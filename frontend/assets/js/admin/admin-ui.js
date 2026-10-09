/**
 * AuraPlayFF Admin — Shared UI Utilities
 * Toasts, modals, loading states, formatters.
 */

// ── Toast notifications ───────────────────────────────────────────────────────
export function showToast(message, type = 'info', duration = 3500) {
  const container = document.getElementById('toast-container');
  if (!container) return;

  const icons = { success: '✅', error: '❌', warning: '⚠️', info: 'ℹ️' };
  const t = document.createElement('div');
  t.className = `toast ${type}`;
  t.innerHTML = `<span>${icons[type] || 'ℹ️'}</span><span>${escHtml(message)}</span>`;
  container.appendChild(t);
  setTimeout(() => t.remove(), duration);
}

// ── Modal helpers ────────────────────────────────────────────────────────────
export function openModal(id) {
  document.getElementById(id)?.classList.add('open');
}

export function closeModal(id) {
  document.getElementById(id)?.classList.remove('open');
}

export function setupModalClose(overlayId) {
  const overlay = document.getElementById(overlayId);
  if (!overlay) return;
  overlay.addEventListener('click', e => {
    if (e.target === overlay) closeModal(overlayId);
  });
}

// ── Skeleton rows ────────────────────────────────────────────────────────────
export function showTableSkeleton(tbodyId, cols = 5, rows = 6) {
  const tbody = document.getElementById(tbodyId);
  if (!tbody) return;
  tbody.innerHTML = Array.from({ length: rows }, () =>
    `<tr>${Array.from({ length: cols }, () =>
      `<td><div class="skeleton" style="height:14px;width:80%;border-radius:4px"></div></td>`
    ).join('')}</tr>`
  ).join('');
}

export function showStatSkeleton(containerSelector, count = 6) {
  const el = document.querySelector(containerSelector);
  if (!el) return;
  el.innerHTML = Array.from({ length: count }, () =>
    `<div class="skeleton skeleton-stat"></div>`
  ).join('');
}

// ── Empty state ──────────────────────────────────────────────────────────────
export function emptyRow(cols, message = 'No records found.') {
  return `<tr><td colspan="${cols}" class="empty-state" style="padding:40px;text-align:center;color:var(--color-text-muted)">${message}</td></tr>`;
}

// ── Status chip ───────────────────────────────────────────────────────────────
export function statusChip(status) {
  const s = (status || 'unknown').toLowerCase().replace(' ', '_');
  return `<span class="chip chip-${s}">${status || '—'}</span>`;
}

// ── Formatters ────────────────────────────────────────────────────────────────
export function fmtCurrency(val, currency = '₹') {
  const n = parseFloat(val);
  if (isNaN(n)) return `${currency}0.00`;
  return `${currency}${n.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

export function fmtDate(iso) {
  if (!iso) return '—';
  try {
    return new Date(iso).toLocaleDateString('en-IN', {
      day: '2-digit', month: 'short', year: 'numeric',
    });
  } catch { return iso; }
}

export function fmtDateTime(iso) {
  if (!iso) return '—';
  try {
    return new Date(iso).toLocaleString('en-IN', {
      day: '2-digit', month: 'short', year: 'numeric',
      hour: '2-digit', minute: '2-digit', hour12: true,
    });
  } catch { return iso; }
}

export function fmtRelative(iso) {
  if (!iso) return '—';
  const diff = Date.now() - new Date(iso).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1)  return 'just now';
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24)  return `${hrs}h ago`;
  const days = Math.floor(hrs / 24);
  return `${days}d ago`;
}

export function truncate(str, len = 30) {
  if (!str) return '';
  return str.length > len ? str.slice(0, len) + '…' : str;
}

export function escHtml(str) {
  const d = document.createElement('div');
  d.textContent = str;
  return d.innerHTML;
}

// ── Pagination helper ─────────────────────────────────────────────────────────
export function renderPagination(containerId, total, limit, offset, onPage) {
  const el = document.getElementById(containerId);
  if (!el) return;

  const page      = Math.floor(offset / limit);
  const totalPages= Math.ceil(total / limit);
  const from      = total === 0 ? 0 : offset + 1;
  const to        = Math.min(offset + limit, total);

  el.innerHTML = `
    <span class="pagination-info">Showing ${from}–${to} of ${total}</span>
    <div class="pagination-btns">
      <button class="btn btn-ghost btn-sm" id="pg-prev" ${page === 0 ? 'disabled' : ''}>← Prev</button>
      <button class="btn btn-ghost btn-sm" id="pg-next" ${page >= totalPages - 1 ? 'disabled' : ''}>Next →</button>
    </div>`;

  document.getElementById('pg-prev')?.addEventListener('click', () => onPage(Math.max(0, offset - limit)));
  document.getElementById('pg-next')?.addEventListener('click', () => onPage(offset + limit));
}

// ── Confirm dialog ────────────────────────────────────────────────────────────
export function confirmAction(message) {
  return confirm(message);
}

// ── Copy to clipboard ─────────────────────────────────────────────────────────
export async function copyToClipboard(text, successMsg = 'Copied!') {
  try {
    await navigator.clipboard.writeText(text);
    showToast(successMsg, 'success', 1500);
  } catch {
    showToast('Copy failed.', 'error');
  }
}
