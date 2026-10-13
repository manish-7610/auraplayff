/**
 * AuraPlayFF Admin — Configuration
 * Mirrors player config. No secrets here.
 */
const ADMIN_CONFIG = {
  /** Base URL of the FastAPI backend.  No trailing slash. */
  API_BASE_URL: 'https://auraplayff.onrender.com',
  APP_NAME: 'AuraPlayFF Admin',
  NOTIFICATION_POLL_INTERVAL: 30_000, // 30s for admin
};
Object.freeze(ADMIN_CONFIG);
