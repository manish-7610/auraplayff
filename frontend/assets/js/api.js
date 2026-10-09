/**
 * AuraPlayFF — Centralized API Client
 *
 * Handles:
 *  - Base URL from CONFIG
 *  - Authorization: Bearer <access_token> header
 *  - Automatic silent token refresh on 401
 *  - Logout on unrecoverable auth failure
 *  - JSON parsing and structured error handling
 *
 * Access token is stored ONLY in module-level memory (never localStorage).
 * Refresh token travels as httpOnly cookie — the browser sends it
 * automatically to /api/auth/* without JS intervention.
 *
 * NEVER store secrets, wallet balances, or any authoritative values
 * in localStorage — the backend is always the source of truth.
 */

// ── Token store (in-memory only) ─────────────────────────────────────────────
let _accessToken = null;
let _refreshPromise = null; // prevents simultaneous refresh calls

export function setAccessToken(token) {
  _accessToken = token;
}

export function getAccessToken() {
  return _accessToken;
}

export function clearAccessToken() {
  _accessToken = null;
}

export function isLoggedIn() {
  return _accessToken !== null;
}

// ── Core fetch wrapper ────────────────────────────────────────────────────────

/**
 * Make an authenticated API request.
 *
 * @param {string} path       - API path e.g. '/api/tournaments'
 * @param {object} [options]  - fetch options override
 * @param {boolean} [retry]   - internal: true when retrying after token refresh
 * @returns {Promise<any>}    - parsed JSON response body
 * @throws {ApiError}         - structured error with { code, message, status }
 */
export async function apiFetch(path, options = {}, retry = false) {
  const url = CONFIG.API_BASE_URL + path;

  const headers = {
    'Content-Type': 'application/json',
    ...(options.headers || {}),
  };

  if (_accessToken) {
    headers['Authorization'] = `Bearer ${_accessToken}`;
  }

  // For multipart/form-data uploads, let the browser set Content-Type
  if (options._isMultipart) {
    delete headers['Content-Type'];
    delete options._isMultipart;
  }

  let resp;
  try {
    resp = await fetch(url, {
      credentials: 'include', // include httpOnly cookies for refresh
      ...options,
      headers,
    });
  } catch (networkErr) {
    throw new ApiError(0, 'network_error', 'Network error — check your connection.');
  }

  // ── Handle 401: try silent token refresh once ─────────────────────────────
  if (resp.status === 401 && !retry) {
    try {
      await silentRefresh();
      return apiFetch(path, options, true); // retry once with new token
    } catch {
      clearAccessToken();
      redirectToLogin();
      throw new ApiError(401, 'session_expired', 'Session expired. Please log in again.');
    }
  }

  // ── Parse response ────────────────────────────────────────────────────────
  let body;
  const contentType = resp.headers.get('content-type') || '';
  if (contentType.includes('application/json')) {
    body = await resp.json();
  } else {
    body = await resp.text();
  }

  if (!resp.ok) {
    // AuraPlayFF backend error format: { error: { code, message } }
    // FastAPI validation errors come as { detail: [...] } or { detail: string }
    const code    = body?.error?.code || body?.code || 'api_error';
    const message = _extractMessage(body, resp.status);
    throw new ApiError(resp.status, code, message, body);
  }

  return body;
}

/** GET helper */
export function apiGet(path, params = {}) {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== '') qs.set(k, v);
  }
  const query = qs.toString();
  return apiFetch(query ? `${path}?${query}` : path, { method: 'GET' });
}

/** POST helper */
export function apiPost(path, body = {}) {
  return apiFetch(path, { method: 'POST', body: JSON.stringify(body) });
}

/** POST multipart/form-data helper (for screenshot upload) */
export function apiPostForm(path, formData) {
  return apiFetch(path, { method: 'POST', body: formData, _isMultipart: true });
}

/** PATCH helper */
export function apiPatch(path, body = {}) {
  return apiFetch(path, { method: 'PATCH', body: JSON.stringify(body) });
}

/** DELETE helper */
export function apiDelete(path) {
  return apiFetch(path, { method: 'DELETE' });
}

// ── Silent refresh ────────────────────────────────────────────────────────────

/**
 * Call POST /api/auth/refresh using the httpOnly refresh cookie.
 * Deduplicates concurrent refresh attempts.
 */
export async function silentRefresh() {
  if (_refreshPromise) return _refreshPromise;

  _refreshPromise = (async () => {
    const resp = await fetch(CONFIG.API_BASE_URL + '/api/auth/refresh', {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
    });
    if (!resp.ok) throw new Error('refresh_failed');
    const data = await resp.json();
    setAccessToken(data.access_token);
  })().finally(() => { _refreshPromise = null; });

  return _refreshPromise;
}

// ── Error type ────────────────────────────────────────────────────────────────

export class ApiError extends Error {
  constructor(status, code, message, raw = null) {
    super(message);
    this.status  = status;
    this.code    = code;
    this.message = message;
    this.raw     = raw;
    this.name    = 'ApiError';
  }

  /** True for the specific HTTP status */
  is(status) { return this.status === status; }

  /** Human-readable label for display */
  get displayMessage() { return this.message; }
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function _extractMessage(body, status) {
  if (!body) return _defaultMessage(status);

  if (typeof body === 'string') return body;

  // AuraPlayFF backend error format: { error: { code, message } }
  if (body.error && body.error.message) return body.error.message;

  // FastAPI default: { detail: string | Array<{loc,msg,type}> }
  if (body.detail) {
    if (typeof body.detail === 'string') return body.detail;
    if (Array.isArray(body.detail)) {
      return body.detail.map(e => e.msg || JSON.stringify(e)).join('; ');
    }
  }

  if (body.message) return body.message;

  return _defaultMessage(status);
}

function _defaultMessage(status) {
  const messages = {
    400: 'Bad request.',
    401: 'Authentication required.',
    403: 'You don\'t have permission for this action.',
    404: 'Not found.',
    409: 'Conflict — this action cannot be performed.',
    422: 'Validation error.',
    429: 'Too many requests. Please slow down.',
    500: 'Server error. Please try again.',
  };
  return messages[status] || `Unexpected error (${status}).`;
}

function redirectToLogin() {
  const current = encodeURIComponent(window.location.pathname + window.location.search);
  // Don't redirect if already on auth pages
  if (window.location.pathname.includes('login') ||
      window.location.pathname.includes('register') ||
      window.location.pathname === '/' ||
      window.location.pathname === '/index.html') return;
  window.location.href = `/login.html?next=${current}`;
}

// ── Session persistence helper ────────────────────────────────────────────────
// On page load, attempt a silent refresh so that if the user has a valid
// refresh cookie the session is restored automatically.

export async function tryRestoreSession() {
  try {
    await silentRefresh();
    return true;
  } catch {
    return false;
  }
}
