/**
 * AuraPlayFF — Shared Layout
 * Injects sidebar + bottom nav into every page after DOM load.
 * Call initLayout() from each page's script.
 */
import { logout } from './auth.js';
import { toastSuccess } from './ui.js';
import { startNotifPolling } from './notifications.js';
import { isLoggedIn } from './api.js';

const NAV_ITEMS = [
  { href: '/index.html',           icon: '🏠', label: 'Home',        id: 'home' },
  { href: '/tournaments.html',     icon: '🏆', label: 'Tournaments', id: 'tournaments' },
  { href: '/my-matches.html',      icon: '🎮', label: 'My Matches',  id: 'my-matches' },
  { href: '/wallet.html',          icon: '💰', label: 'Wallet',      id: 'wallet' },
  { href: '/profile.html',         icon: '👤', label: 'Profile',     id: 'profile' },
];

// Extra sidebar-only items
const SIDEBAR_EXTRAS = [
  { href: '/notifications.html',   icon: '🔔', label: 'Notifications', id: 'notifications', badge: true },
  { href: '/leaderboard.html',     icon: '📊', label: 'Leaderboard',   id: 'leaderboard' },
  { href: '/referral.html',        icon: '🤝', label: 'Referral',      id: 'referral' },
  { href: '/deposit.html',         icon: '💳', label: 'Add Money',     id: 'deposit' },
  { href: '/withdrawal.html',      icon: '💸', label: 'Withdraw',      id: 'withdrawal' },
  { href: '/support.html',         icon: '🆘', label: 'Help & Support', id: 'support' },
];

function currentPage() {
  return window.location.pathname.replace(/^\//, '') || 'index.html';
}

function isActive(href) {
  const page = currentPage();
  return href.replace(/^\//, '') === page || href === '/' + page;
}

function buildSidebarNavItem(item) {
  const active = isActive(item.href) ? 'active' : '';
  const badge = item.badge
    ? `<span class="nav-badge hidden" data-notif-badge>0</span>`
    : '';
  return `
    <a href="${item.href}" class="${active}" id="nav-${item.id}">
      <span class="nav-icon">${item.icon}</span>
      <span>${item.label}</span>
      ${badge}
    </a>`;
}

function buildBottomNavItem(item) {
  const active = isActive(item.href) ? 'active' : '';
  const badge = item.id === 'notifications'
    ? `<span class="nav-badge hidden" data-notif-badge>0</span>`
    : '';
  return `
    <a href="${item.href}" class="${active}">
      ${badge}
      <span class="nav-icon">${item.icon}</span>
      <span>${item.label}</span>
    </a>`;
}

export function buildSidebar(user) {
  const allNavItems = [...NAV_ITEMS, ...SIDEBAR_EXTRAS];
  const notifItem = { href: '/notifications.html', icon: '🔔', label: 'Notifications', id: 'notifications', badge: true };

  return `
    <div class="sidebar" id="sidebar">
      <div class="sidebar-logo">
        <a href="/index.html" class="logo-text" style="text-decoration:none">${CONFIG.APP_NAME}</a>
      </div>
      <nav class="sidebar-nav">
        ${NAV_ITEMS.map(buildSidebarNavItem).join('')}
        <hr style="border-color:var(--color-border);margin:var(--space-3) var(--space-5);">
        ${SIDEBAR_EXTRAS.map(buildSidebarNavItem).join('')}
      </nav>
      <div class="sidebar-bottom">
        <div class="sidebar-user">
          <div class="sidebar-avatar" id="sidebar-avatar">?</div>
          <div class="sidebar-user-info">
            <div class="sidebar-username" id="sidebar-username">Loading…</div>
            <div class="sidebar-role">Player</div>
          </div>
        </div>
        <button class="btn btn-ghost btn-sm btn-full" id="sidebar-logout" style="margin-top:var(--space-3);justify-content:flex-start;gap:var(--space-2)">
          🚪 Logout
        </button>
      </div>
    </div>`;
}

export function buildBottomNav() {
  // Bottom nav: Home, Tournaments, My Matches, Wallet, Notifications
  const bottomItems = [
    NAV_ITEMS[0], NAV_ITEMS[1], NAV_ITEMS[2], NAV_ITEMS[3],
    { href: '/notifications.html', icon: '🔔', label: 'Alerts', id: 'notifications' },
  ];
  return `
    <nav class="bottom-nav" id="bottom-nav">
      ${bottomItems.map(buildBottomNavItem).join('')}
    </nav>`;
}

export function buildTopbar() {
  return `
    <header class="topbar" id="topbar">
      <a href="/index.html" class="topbar-logo">${CONFIG.APP_NAME}</a>
      <div class="topbar-actions">
        <a href="/notifications.html" style="position:relative;color:var(--color-text-muted);font-size:22px;text-decoration:none">
          🔔<span class="nav-badge hidden" data-notif-badge style="position:absolute;top:-4px;right:-6px;"></span>
        </a>
        <a href="/wallet.html" style="color:var(--color-text-muted);font-size:22px;text-decoration:none">💰</a>
      </div>
    </header>`;
}

/**
 * Initialize layout: inject sidebar + bottom nav, wire logout, start polling.
 * Also auto-populates the sidebar username from sessionStorage.
 * @param {object} [options]
 * @param {boolean} [options.requiresAuth=true]  - not used directly here;
 *   call requireAuth() from the page script before initLayout() when true.
 */
export async function initLayout({ requiresAuth = true } = {}) {
  // Inject topbar
  const topbarPlaceholder = document.getElementById('topbar-placeholder');
  if (topbarPlaceholder) topbarPlaceholder.innerHTML = buildTopbar();

  // Inject sidebar
  const sidebarPlaceholder = document.getElementById('sidebar-placeholder');
  if (sidebarPlaceholder) sidebarPlaceholder.innerHTML = buildSidebar();

  // Inject bottom nav
  const bottomNavPlaceholder = document.getElementById('bottom-nav-placeholder');
  if (bottomNavPlaceholder) bottomNavPlaceholder.innerHTML = buildBottomNav();

  // Auto-populate sidebar username from sessionStorage
  const storedUsername = sessionStorage.getItem('aura_username');
  if (storedUsername) {
    setSidebarUser({ username: storedUsername });
  }

  // Wire logout button
  document.getElementById('sidebar-logout')?.addEventListener('click', async () => {
    await logout();
    sessionStorage.removeItem('aura_username');
    toastSuccess('Logged out.');
    setTimeout(() => { window.location.href = '/login.html'; }, 600);
  });

  // Start notification badge polling if logged in
  if (isLoggedIn()) {
    startNotifPolling();
  }
}

/**
 * Update sidebar user display from a player object.
 */
export function setSidebarUser(user) {
  const el = document.getElementById('sidebar-username');
  const av = document.getElementById('sidebar-avatar');
  if (el && user?.username) el.textContent = user.username;
  if (av && user?.username) av.textContent = user.username[0].toUpperCase();
}
