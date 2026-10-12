/**
 * AuraPlayFF Admin — Finance (Deposits & Withdrawals)
 * SUPER ADMIN ONLY — agents cannot access these operations.
 *
 * Deposit APIs:
 *   GET  /api/admin/deposits/pending-manual
 *   GET  /api/admin/deposits
 *   GET  /api/admin/deposits/{id}
 *   POST /api/admin/deposits/{id}/approve
 *   POST /api/admin/deposits/{id}/reject
 *
 * Withdrawal APIs:
 *   GET  /api/admin/withdrawals/pending
 *   GET  /api/admin/withdrawals
 *   GET  /api/admin/withdrawals/{id}
 *   POST /api/admin/withdrawals/{id}/process
 *   POST /api/admin/withdrawals/{id}/mark-paid
 *   POST /api/admin/withdrawals/{id}/reject
 */

import { adminGet, adminPost, isSuper } from './admin-api.js';
import {
  showToast, statusChip, fmtDateTime, fmtCurrency,
  escHtml, renderPagination, showTableSkeleton, emptyRow, confirmAction
} from './admin-ui.js';

// ─────────────────────────────────────────────────────────────────────────────
// DEPOSITS
// ─────────────────────────────────────────────────────────────────────────────
let _dOffset = 0;
const _dLimit = 25;
let _dStatus = '';
let _dMethod = '';

export async function loadDeposits() {
  if (!isSuper()) { _showNoAccess('deposits-tbody', 9); return; }
  showTableSkeleton('deposits-tbody', 9, 8);
  try {
    const params = { limit: _dLimit, offset: _dOffset };
    if (_dStatus) params.status = _dStatus;
    if (_dMethod) params.method = _dMethod;
    const data = await adminGet('/api/admin/deposits', params);
    _renderDepositsTable(data.items || [], data.total || 0);
  } catch (err) {
    document.getElementById('deposits-tbody').innerHTML = emptyRow(9, '⚠ ' + (err.displayMessage || 'Load failed'));
  }
}

function _renderDepositsTable(items, total) {
  const tbody = document.getElementById('deposits-tbody');
  if (!tbody) return;
  if (!items.length) {
    tbody.innerHTML = emptyRow(9, 'No deposits found.');
    renderPagination('deposits-pagination', 0, _dLimit, 0, () => {});
    return;
  }
  tbody.innerHTML = items.map(d => {
    const canApprove = d.status === 'pending_manual_verification';
    // BUG #20 FIX: backend deposit_service.reject_manual_deposit() only allows
    // rejecting deposits in 'pending_manual_verification' status. Including
    // 'pending' here caused the Reject button to appear for online gateway
    // deposits (status='pending'), and clicking it produced a 409 error from
    // the backend — confusing admins and implying the action was possible.
    const canReject  = d.status === 'pending_manual_verification';
    return `<tr>
      <td class="td-mono" style="font-size:12px">${escHtml((d.deposit_id || d.id || '').slice(0,8))}…</td>
      <td style="font-weight:600">${escHtml(d.username || d.user_id || '—')}</td>
      <td style="font-weight:700;color:var(--color-success)">₹${d.amount}</td>
      <td>${statusChip(d.status)}</td>
      <td>${d.method === 'online' ? '💳 Online' : '📱 Manual UPI'}</td>
      <td class="td-mono" style="font-size:12px">${escHtml(d.utr || '—')}</td>
      <td class="td-mono" style="font-size:12px">${escHtml(d.provider_payment_id || '—')}</td>
      <td style="font-size:12px;color:var(--color-text-muted)">${fmtDateTime(d.created_at)}</td>
      <td class="td-actions">
        ${canApprove ? `<button class="btn btn-success btn-sm" data-action="approve" data-id="${d.deposit_id || d.id}">✓ Approve</button>` : ''}
        ${canReject  ? `<button class="btn btn-danger btn-sm" style="margin-left:4px" data-action="reject" data-id="${d.deposit_id || d.id}">✕ Reject</button>` : ''}
      </td>
    </tr>`;
  }).join('');

  // Wire action buttons
  tbody.querySelectorAll('[data-action]').forEach(btn => {
    btn.addEventListener('click', () => {
      if (btn.dataset.action === 'approve') approveDeposit(btn.dataset.id);
      else if (btn.dataset.action === 'reject') rejectDeposit(btn.dataset.id);
    });
  });

  renderPagination('deposits-pagination', total, _dLimit, _dOffset, off => {
    _dOffset = off; loadDeposits();
  });
}

export function setupDepositFilters() {
  document.getElementById('filter-dep-status')?.addEventListener('change', e => { _dStatus = e.target.value; _dOffset = 0; loadDeposits(); });
  document.getElementById('filter-dep-method')?.addEventListener('change', e => { _dMethod = e.target.value; _dOffset = 0; loadDeposits(); });
  document.getElementById('btn-dep-reset')?.addEventListener('click', () => {
    document.getElementById('filter-dep-status').value = '';
    document.getElementById('filter-dep-method').value = '';
    _dStatus = ''; _dMethod = ''; _dOffset = 0; loadDeposits();
  });
}

export async function approveDeposit(id) {
  const note = prompt('Approval note (optional):') ?? '';
  if (!confirmAction('Approve this deposit and credit player wallet?')) return;
  try {
    await adminPost(`/api/admin/deposits/${id}/approve`, { note });
    showToast('Deposit approved — wallet credited', 'success');
    loadDeposits();
  } catch (err) { showToast(err.displayMessage || 'Approval failed', 'error'); }
}

export async function rejectDeposit(id) {
  const reason = prompt('Rejection reason (required):');
  if (!reason?.trim()) return;
  if (!confirmAction('Reject this deposit?')) return;
  try {
    await adminPost(`/api/admin/deposits/${id}/reject`, { reason });
    showToast('Deposit rejected', 'warning');
    loadDeposits();
  } catch (err) { showToast(err.displayMessage || 'Rejection failed', 'error'); }
}

// ─────────────────────────────────────────────────────────────────────────────
// WITHDRAWALS
// ─────────────────────────────────────────────────────────────────────────────
let _wOffset = 0;
const _wLimit = 25;
let _wStatus = '';

export async function loadWithdrawals() {
  if (!isSuper()) { _showNoAccess('withdrawals-tbody', 9); return; }
  showTableSkeleton('withdrawals-tbody', 9, 8);
  try {
    const params = { limit: _wLimit, offset: _wOffset };
    if (_wStatus) params.status = _wStatus;
    const data = await adminGet('/api/admin/withdrawals', params);
    _renderWithdrawalsTable(data.items || [], data.total || 0);
  } catch (err) {
    document.getElementById('withdrawals-tbody').innerHTML = emptyRow(9, '⚠ ' + (err.displayMessage || 'Load failed'));
  }
}

function _renderWithdrawalsTable(items, total) {
  const tbody = document.getElementById('withdrawals-tbody');
  if (!tbody) return;
  if (!items.length) {
    tbody.innerHTML = emptyRow(9, 'No withdrawals found.');
    renderPagination('withdrawals-pagination', 0, _wLimit, 0, () => {});
    return;
  }
  tbody.innerHTML = items.map(w => {
    const canProcess  = w.status === 'pending';
    const canMarkPaid = w.status === 'processing';
    const canReject   = ['pending', 'processing'].includes(w.status);
    return `<tr>
      <td style="font-weight:600">${escHtml(w.username || '—')}</td>
      <td style="font-size:12px;color:var(--color-text-muted)">${escHtml(w.ff_name || 'Not set')}</td>
      <td class="td-mono" style="font-size:12px">${escHtml(w.upi_id || '—')}</td>
      <td style="font-weight:700;color:var(--color-warning)">₹${w.amount}</td>
      <td>${statusChip(w.status)}</td>
      <td class="td-mono" style="font-size:12px">${escHtml(w.payout_utr || '—')}</td>
      <td style="font-size:12px;color:var(--color-text-muted)">${fmtDateTime(w.created_at)}</td>
      <td style="font-size:12px;color:var(--color-text-muted)">${w.processed_at ? fmtDateTime(w.processed_at) : '—'}</td>
      <td class="td-actions">
        ${canProcess  ? `<button class="btn btn-primary btn-sm" data-action="process" data-id="${w.withdrawal_id || w.id}">▶ Process</button>` : ''}
        ${canMarkPaid ? `<button class="btn btn-success btn-sm" data-action="mark-paid" data-id="${w.withdrawal_id || w.id}">✓ Mark Paid</button>` : ''}
        ${canReject   ? `<button class="btn btn-danger btn-sm" style="margin-left:4px" data-action="reject" data-id="${w.withdrawal_id || w.id}">✕ Reject</button>` : ''}
      </td>
    </tr>`;
  }).join('');

  tbody.querySelectorAll('[data-action]').forEach(btn => {
    btn.addEventListener('click', () => {
      const id = btn.dataset.id;
      if (btn.dataset.action === 'process')   processWithdrawal(id);
      else if (btn.dataset.action === 'mark-paid') markWithdrawalPaid(id);
      else if (btn.dataset.action === 'reject')    rejectWithdrawal(id);
    });
  });

  renderPagination('withdrawals-pagination', total, _wLimit, _wOffset, off => {
    _wOffset = off; loadWithdrawals();
  });
}

export function setupWithdrawalFilters() {
  document.getElementById('filter-wd-status')?.addEventListener('change', e => { _wStatus = e.target.value; _wOffset = 0; loadWithdrawals(); });
  document.getElementById('btn-wd-reset')?.addEventListener('click', () => {
    document.getElementById('filter-wd-status').value = '';
    _wStatus = ''; _wOffset = 0; loadWithdrawals();
  });
}

export async function processWithdrawal(id) {
  const note = prompt('Processing note (optional):') ?? '';
  if (!confirmAction('Mark as PROCESSING?')) return;
  try {
    await adminPost(`/api/admin/withdrawals/${id}/process`, { note });
    showToast('Withdrawal marked as processing', 'info');
    loadWithdrawals();
  } catch (err) { showToast(err.displayMessage || 'Failed', 'error'); }
}

export async function markWithdrawalPaid(id) {
  const utr = prompt('Enter Payout UTR / Transaction Reference:');
  if (!utr?.trim()) { showToast('UTR is required to mark as paid', 'warning'); return; }
  const note = prompt('Note (optional):') ?? '';
  if (!confirmAction(`Mark as PAID with UTR: ${utr}?`)) return;
  try {
    await adminPost(`/api/admin/withdrawals/${id}/mark-paid`, { payout_utr: utr.trim(), note });
    showToast('Withdrawal marked as paid — balance finalised', 'success');
    loadWithdrawals();
  } catch (err) { showToast(err.displayMessage || 'Failed', 'error'); }
}

export async function rejectWithdrawal(id) {
  const reason = prompt('Rejection reason (required):');
  if (!reason?.trim()) return;
  if (!confirmAction('Reject withdrawal and release hold back to player?')) return;
  try {
    await adminPost(`/api/admin/withdrawals/${id}/reject`, { reason });
    showToast('Withdrawal rejected — balance released', 'warning');
    loadWithdrawals();
  } catch (err) { showToast(err.displayMessage || 'Failed', 'error'); }
}

// ── Shared helper ─────────────────────────────────────────────────────────────
function _showNoAccess(tbodyId, cols) {
  const el = document.getElementById(tbodyId);
  if (el) el.innerHTML = emptyRow(cols, '🔒 Super Admin access required for financial operations.');
}
