/**
 * AuraPlayFF — Auth Module
 * Wraps auth-related API calls. Keeps auth logic out of page scripts.
 */
import { apiPost, apiGet, setAccessToken, clearAccessToken, tryRestoreSession } from './api.js';

// ── Register ──────────────────────────────────────────────────────────────────

/**
 * @param {{ username, password, confirm_password, ff_uid, referral_code? }} data
 * @returns {Promise<{ id, username, ff_uid, referral_code, message }>}
 */
export async function register(data) {
  return apiPost('/api/auth/register', data);
}

// ── Login ─────────────────────────────────────────────────────────────────────

/**
 * @param {{ username, password }} credentials
 * @returns {Promise<void>}
 */
export async function login({ username, password }) {
  const data = await apiPost('/api/auth/login', { username, password });
  setAccessToken(data.access_token);
  // Store username for display purposes only — never store tokens or sensitive values here
  sessionStorage.setItem('aura_username', username);
  return data;
}

// ── Logout ────────────────────────────────────────────────────────────────────

export async function logout() {
  try {
    await apiPost('/api/auth/logout', {});
  } catch { /* best effort */ }
  clearAccessToken();
  sessionStorage.removeItem('aura_username');
}

// ── Reset password ────────────────────────────────────────────────────────────

/**
 * @param {{ token, new_password, confirm_password }} data
 */
export async function resetPassword(data) {
  return apiPost('/api/auth/reset-password', data);
}

// ── Session guard ─────────────────────────────────────────────────────────────

/**
 * Call on every protected page's DOMContentLoaded.
 * Attempts silent session restore. Redirects to login if it fails.
 * Returns true if the session is valid.
 */
export async function requireAuth() {
  const ok = await tryRestoreSession();
  if (!ok) {
    const next = encodeURIComponent(window.location.pathname + window.location.search);
    window.location.href = `/login.html?next=${next}`;
    return false;
  }
  return true;
}

/**
 * For pages that are public but need to know if the user is logged in
 * (e.g. index.html). Returns true/false without redirecting.
 */
export async function checkSession() {
  return tryRestoreSession();
}
