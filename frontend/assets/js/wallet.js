/**
 * AuraPlayFF — Wallet Module
 * Wraps all wallet, deposit, withdrawal, and referral API calls.
 */
import { apiGet, apiPost } from './api.js';

// ── Wallet ────────────────────────────────────────────────────────────────────

export function getWallet() {
  return apiGet('/api/wallet/me');
}

export function getTransactions(params = {}) {
  return apiGet('/api/wallet/me/transactions', params);
}

// ── Referral ──────────────────────────────────────────────────────────────────

export function getReferralInfo() {
  return apiGet('/api/wallet/me/referral');
}

export function getReferralList(params = {}) {
  return apiGet('/api/wallet/me/referral/list', params);
}

export function getReferralStats() {
  return apiGet('/api/wallet/me/referral/stats');
}

// ── Deposits ──────────────────────────────────────────────────────────────────

export function createOnlineDeposit(amount) {
  return apiPost('/api/deposits/online', { amount });
}

export function verifyOnlinePayment(depositId, payload) {
  return apiPost(`/api/deposits/online/${depositId}/verify`, payload);
}

export function createManualDeposit(amount) {
  return apiPost('/api/deposits/manual', { amount });
}

export function submitUTR(depositId, { utr, payer_info }) {
  return apiPost(`/api/deposits/manual/${depositId}/utr`, { utr, payer_info });
}

export function getUpiInfo() {
  return apiGet('/api/deposits/upi-info');
}

export function getDeposit(depositId) {
  return apiGet(`/api/deposits/${depositId}`);
}

export function listDeposits(params = {}) {
  return apiGet('/api/deposits', params);
}

// ── Withdrawals ───────────────────────────────────────────────────────────────

export function getWithdrawalBalanceSummary() {
  return apiGet('/api/withdrawals/balance-summary');
}

export function createWithdrawal({ amount, upi_id }) {
  return apiPost('/api/withdrawals', { amount, upi_id });
}

export function getWithdrawal(withdrawalId) {
  return apiGet(`/api/withdrawals/${withdrawalId}`);
}

export function listWithdrawals(params = {}) {
  return apiGet('/api/withdrawals', params);
}

// ── Payment configuration (player-facing) ────────────────────────────────────

/**
 * Fetch the active manual payment configuration.
 * Returns { manual_payment_active, upi_id, upi_display_name, upi_account_name,
 *           instructions, qr_ref }
 * Nothing is hardcoded — all values come from platform settings.
 */
export function getPaymentConfig() {
  return apiGet('/api/deposits/payment-config');
}
