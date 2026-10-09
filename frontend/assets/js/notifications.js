/**
 * AuraPlayFF — Notifications Module
 */
import { apiGet, apiPost } from './api.js';
import { updateNotifBadges } from './ui.js';

// ── API calls ─────────────────────────────────────────────────────────────────

export function listNotifications(params = {}) {
  return apiGet('/api/notifications', params);
}

export function getUnreadCount() {
  return apiGet('/api/notifications/unread-count');
}

export function markRead(notificationId) {
  return apiPost(`/api/notifications/${notificationId}/read`, {});
}

export function markAllRead() {
  return apiPost('/api/notifications/read-all', {});
}

// ── Polling ───────────────────────────────────────────────────────────────────

let _pollTimer = null;

/**
 * Start polling unread count and updating nav badges.
 * Safe to call multiple times — deduplicated.
 */
export function startNotifPolling() {
  if (_pollTimer) return;

  const poll = async () => {
    try {
      const data = await getUnreadCount();
      updateNotifBadges(data.unread_count);
    } catch { /* silent — don't break the page */ }
  };

  poll(); // immediate first run
  _pollTimer = setInterval(poll, CONFIG.NOTIFICATION_POLL_INTERVAL);
}

export function stopNotifPolling() {
  if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null; }
}

// ── Icon per notification type ────────────────────────────────────────────────

export function notifIcon(type) {
  if (type.startsWith('tournament')) return '🏆';
  if (type.startsWith('deposit'))    return '💰';
  if (type.startsWith('withdrawal')) return '💸';
  if (type.startsWith('referral'))   return '🤝';
  if (type.startsWith('account'))    return '👤';
  if (type.startsWith('agent'))      return '🎯';
  if (type.startsWith('admin'))      return '⚙️';
  if (type.startsWith('scheduler'))  return '⏰';
  return '🔔';
}
