/**
 * AuraPlayFF Admin — Configuration
 * Mirrors player config. No secrets here.
 */
const ADMIN_CONFIG = {
  /** Base URL of the FastAPI backend.  No trailing slash.
   *  Dynamically mirrors the browser hostname so both
   *  http://localhost:5500 and http://127.0.0.1:5500 work correctly. */
  API_BASE_URL: `http://${window.location.hostname}:8000`,
  APP_NAME: 'AuraPlayFF Admin',
  NOTIFICATION_POLL_INTERVAL: 30_000, // 30s for admin
};
Object.freeze(ADMIN_CONFIG);
