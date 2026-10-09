/**
 * AuraPlayFF — Frontend Configuration
 *
 * Change API_BASE_URL to point to your deployed backend.
 * NEVER put secrets (JWT signing key, Supabase service-role key,
 * Razorpay secret) here — those live only on the server.
 */
const CONFIG = {
  /** Base URL of the FastAPI backend.  No trailing slash.
   *  Dynamically mirrors the browser hostname so both
   *  http://localhost:5500 and http://127.0.0.1:5500 work correctly. */
  API_BASE_URL: `http://${window.location.hostname}:8000`,

  /** Razorpay checkout script CDN (safe public URL, no secret) */
  RAZORPAY_CHECKOUT_URL: 'https://checkout.razorpay.com/v1/checkout.js',

  /** Polling interval (ms) for unread notification badge */
  NOTIFICATION_POLL_INTERVAL: 60_000,

  /** App name shown in titles and headings */
  APP_NAME: 'AuraPlayFF',
};

// Freeze so no page can accidentally overwrite config values
Object.freeze(CONFIG);
