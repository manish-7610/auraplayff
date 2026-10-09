/**
 * AuraPlayFF Admin — Authentication
 *
 * Handles staff login/logout using the existing staff auth endpoints.
 * Access token lives in memory only — never localStorage.
 * Refresh cookie 'staff_refresh_token' is httpOnly (scoped to /api/admin/auth).
 *
 * Role separation:
 *   super_admin — full access
 *   agent       — limited access (no financial, no agent management)
 *
 * SECURITY: Role-based UI hiding is UI-only convenience.
 * Every sensitive operation is also protected by backend RBAC.
 */

import {
  adminPost, adminGet,
  setAccessToken, clearAccessToken,
  setStaffProfile, clearStaffProfile,
  getStaffProfile,
  tryRestoreAdminSession,
  AdminApiError,
  redirectToAdminLogin,
} from './admin-api.js';

// ── Login ─────────────────────────────────────────────────────────────────────
export async function adminLogin(agentId, password) {
  const data = await adminPost('/api/admin/auth/login', {
    agent_id: agentId,
    password,
  });
  setAccessToken(data.access_token);

  // Decode role from JWT payload (not signature — just display)
  const profile = _decodeJwtPayload(data.access_token);
  if (!profile) throw new Error('Invalid token received.');

  // BUG #9 FIX: JWT now carries display_name and agent_id claims.
  // Fall back to the form input for agent_id if not in token (backward compat).
  setStaffProfile({
    id:           profile.sub,
    agent_id:     profile.agent_id || agentId,
    display_name: profile.display_name || agentId,
    role:         profile.role || 'agent',
  });

  return getStaffProfile();
}

// ── Logout ────────────────────────────────────────────────────────────────────
export async function adminLogout() {
  try {
    await adminPost('/api/admin/auth/logout', {});
  } catch { /* ignore errors — clear anyway */ }
  clearAccessToken();
  clearStaffProfile();
  window.location.href = '/admin/login.html';
}

// ── Session restore on page load ──────────────────────────────────────────────
export async function restoreAdminSession() {
  const ok = await tryRestoreAdminSession();
  if (!ok) return null;

  // Decode token to get profile
  const { getAccessToken } = await import('./admin-api.js');
  const token = getAccessToken();
  if (!token) return null;

  const profile = _decodeJwtPayload(token);
  if (!profile) return null;

  // BUG #1/#6/#9 FIX: JWT now contains agent_id and display_name claims
  // (added to create_access_token for staff tokens). Fall back gracefully
  // for any token issued before this fix.
  const p = {
    id:           profile.sub,
    agent_id:     profile.agent_id || profile.sub,
    display_name: profile.display_name || profile.agent_id || profile.sub,
    role:         profile.role || 'agent',
  };
  setStaffProfile(p);
  return p;
}

// ── Guard — call on every protected page ─────────────────────────────────────
export async function requireAdminSession() {
  let profile = getStaffProfile();
  if (!profile) {
    profile = await restoreAdminSession();
  }
  if (!profile) {
    redirectToAdminLogin();
    return null;
  }
  return profile;
}

// ── Role-based DOM visibility ─────────────────────────────────────────────────
export function applyRoleVisibility(role) {
  document.body.classList.remove('role-super_admin', 'role-agent');
  document.body.classList.add(`role-${role}`);
}

// ── JWT payload decoder (NO signature verification — display only) ─────────────
function _decodeJwtPayload(token) {
  try {
    const parts = token.split('.');
    if (parts.length !== 3) return null;
    const payload = JSON.parse(atob(parts[1].replace(/-/g, '+').replace(/_/g, '/')));
    return payload;
  } catch {
    return null;
  }
}
