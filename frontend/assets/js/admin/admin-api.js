/**
 * AuraPlayFF Admin — API Client
 *
 * Separate from the player api.js.
 * Uses 'staff_refresh_token' httpOnly cookie (scoped to /api/admin/auth).
 * Access token stored in memory ONLY — never localStorage.
 * Audience = "staff".
 */

// ── Token store ──────────────────────────────────────────────────────────────
let _accessToken = null;
let _refreshPromise = null;
let _staffProfile = null; // { agent_id, display_name, role, id }

export function setAccessToken(t) { _accessToken = t; }
export function getAccessToken()  { return _accessToken; }
export function clearAccessToken(){ _accessToken = null; }
export function isLoggedIn()      { return _accessToken !== null; }

export function setStaffProfile(p) { _staffProfile = p; }
export function getStaffProfile()  { return _staffProfile; }
export function clearStaffProfile(){ _staffProfile = null; }

export function isSuper() {
  return _staffProfile?.role === 'super_admin';
}

export function isAgent() {
  return _staffProfile?.role === 'agent';
}

// ── Core fetch ────────────────────────────────────────────────────────────────
export async function adminFetch(path, options = {}, retry = false) {
  const url = ADMIN_CONFIG.API_BASE_URL + path;

  const headers = {
    'Content-Type': 'application/json',
    ...(options.headers || {}),
  };

  if (_accessToken) headers['Authorization'] = `Bearer ${_accessToken}`;

  if (options._isMultipart) {
    delete headers['Content-Type'];
    delete options._isMultipart;
  }

  let resp;
  try {
    resp = await fetch(url, {
      credentials: 'include',
      ...options,
      headers,
    });
  } catch {
    throw new AdminApiError(0, 'network_error', 'Network error — check connection.');
  }

  if (resp.status === 401 && !retry) {
    try {
      await adminSilentRefresh();
      return adminFetch(path, options, true);
    } catch {
      clearAccessToken();
      clearStaffProfile();
      redirectToAdminLogin();
      throw new AdminApiError(401, 'session_expired', 'Session expired.');
    }
  }

  let body;
  const ct = resp.headers.get('content-type') || '';
  if (ct.includes('application/json')) {
    body = await resp.json();
  } else {
    body = await resp.text();
  }

  if (!resp.ok) {
    // AuraPlayFF backend error format: { error: { code, message } }
    const code    = body?.error?.code || body?.code || 'api_error';
    const message = _extractMessage(body, resp.status);
    throw new AdminApiError(resp.status, code, message, body);
  }

  return body;
}

export function adminGet(path, params = {}) {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== '') qs.set(k, v);
  }
  const q = qs.toString();
  return adminFetch(q ? `${path}?${q}` : path, { method: 'GET' });
}

export function adminPost(path, body = {}) {
  return adminFetch(path, { method: 'POST', body: JSON.stringify(body) });
}

export function adminPatch(path, body = {}) {
  return adminFetch(path, { method: 'PATCH', body: JSON.stringify(body) });
}

export function adminPut(path, body = {}) {
  return adminFetch(path, { method: 'PUT', body: JSON.stringify(body) });
}

export function adminDelete(path) {
  return adminFetch(path, { method: 'DELETE' });
}

// ── Silent refresh ─────────────────────────────────────────────────────────
export async function adminSilentRefresh() {
  if (_refreshPromise) return _refreshPromise;

  _refreshPromise = (async () => {
    const resp = await fetch(
      ADMIN_CONFIG.API_BASE_URL + '/api/admin/auth/refresh',
      { method: 'POST', credentials: 'include', headers: { 'Content-Type': 'application/json' } }
    );
    if (!resp.ok) throw new Error('refresh_failed');
    const data = await resp.json();
    setAccessToken(data.access_token);
  })().finally(() => { _refreshPromise = null; });

  return _refreshPromise;
}

export async function tryRestoreAdminSession() {
  try {
    await adminSilentRefresh();
    return true;
  } catch {
    return false;
  }
}

// ── Error ─────────────────────────────────────────────────────────────────────
export class AdminApiError extends Error {
  constructor(status, code, message, raw = null) {
    super(message);
    this.status  = status;
    this.code    = code;
    this.message = message;
    this.raw     = raw;
    this.name    = 'AdminApiError';
  }
  is(s) { return this.status === s; }
  get displayMessage() { return this.message; }
}

// ── Helpers ──────────────────────────────────────────────────────────────────
function _extractMessage(body, status) {
  if (!body) return _defaultMessage(status);
  if (typeof body === 'string') return body;
  // AuraPlayFF backend error format: { error: { code, message } }
  if (body.error && body.error.message) return body.error.message;
  if (body.detail) {
    if (typeof body.detail === 'string') return body.detail;
    if (Array.isArray(body.detail)) return body.detail.map(e => e.msg || JSON.stringify(e)).join('; ');
  }
  if (body.message) return body.message;
  return _defaultMessage(status);
}

function _defaultMessage(status) {
  const m = {
    400: 'Bad request.',
    401: 'Authentication required.',
    403: 'Permission denied.',
    404: 'Not found.',
    409: 'Conflict.',
    422: 'Validation error.',
    429: 'Too many requests.',
    500: 'Server error.',
  };
  return m[status] || `Error (${status}).`;
}

export function redirectToAdminLogin() {
  if (window.location.pathname.includes('login.html')) return;
  window.location.href = '/admin/login.html';
}
