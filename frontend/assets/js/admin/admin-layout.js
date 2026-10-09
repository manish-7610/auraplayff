/**
 * AuraPlayFF Admin — Shared Layout
 *
 * Injects the sidebar + topbar HTML, sets the active nav item,
 * manages the mobile hamburger, and polls for notification badges.
 *
 * Usage:
 *   await initAdminLayout({ page: 'tournaments', title: 'Tournaments' });
 */

import { adminLogout } from './admin-auth.js';
import { adminGet }    from './admin-api.js';
import { getStaffProfile, isSuper } from './admin-api.js';

// ── Navigation definition ─────────────────────────────────────────────────────
const NAV_ITEMS = [
  // section, id, icon, label, href, adminOnly
  { section: 'Overview',      id: 'dashboard',    icon: '📊', label: 'Dashboard',       href: '/admin/index.html',         adminOnly: false },
  { section: 'Overview',      id: 'leaderboard',  icon: '🏅', label: 'Leaderboard',     href: '/admin/leaderboard.html',   adminOnly: false },
  { section: 'Tournaments',   id: 'tournaments',  icon: '🏆', label: 'Tournaments',      href: '/admin/tournaments.html',        adminOnly: false },
  { section: 'Tournaments',   id: 'results',      icon: '🎯', label: 'Results',          href: '/admin/results.html',            adminOnly: false },
  { section: 'Tournaments',   id: 'results-history', icon: '📋', label: 'Results History', href: '/admin/results.html?tab=history', adminOnly: false },
  { section: 'Players',       id: 'players',      icon: '👥', label: 'Players',         href: '/admin/players.html',       adminOnly: false },
  { section: 'Support',      id: 'support',      icon: '🆘', label: 'Support',          href: '/admin/support.html',       adminOnly: false },
  { section: 'Finance',       id: 'deposits',          icon: '💰', label: 'Deposits',          href: '/admin/deposits.html',          adminOnly: true  },
  { section: 'Finance',       id: 'withdrawals',       icon: '💸', label: 'Withdrawals',       href: '/admin/withdrawals.html',       adminOnly: true  },
  { section: 'Finance',       id: 'payment-settings',  icon: '⚙️', label: 'Payment Settings',  href: '/admin/payment-settings.html',  adminOnly: true  },
  { section: 'Agents',        id: 'agents',       icon: '🤖', label: 'Agents',          href: '/admin/agents.html',        adminOnly: true  },
  { section: 'System',        id: 'notifications',icon: '🔔', label: 'Notifications',   href: '/admin/notifications.html', adminOnly: false },
  { section: 'System',        id: 'jobs',         icon: '⚙️', label: 'Scheduler',       href: '/admin/jobs.html',          adminOnly: true  },
  { section: 'System',        id: 'audit-logs',   icon: '📋', label: 'Audit Logs',      href: '/admin/audit-logs.html',    adminOnly: true  },
  { section: 'System',        id: 'settings',     icon: '🛠️', label: 'Settings',        href: '/admin/settings.html',      adminOnly: true  },
];

export async function initAdminLayout({ page = '', title = '' } = {}) {
  const profile = getStaffProfile();
  const superAdmin = isSuper();

  // Inject sidebar
  _injectSidebar(page, profile, superAdmin);

  // Inject topbar
  _injectTopbar(title);

  // Inject toast container
  if (!document.getElementById('toast-container')) {
    const tc = document.createElement('div');
    tc.id = 'toast-container';
    document.body.appendChild(tc);
  }

  // Role class on body for CSS .admin-only hiding
  document.body.classList.add(superAdmin ? 'role-super_admin' : 'role-agent');

  // Mobile hamburger
  document.getElementById('hamburger-btn')?.addEventListener('click', () => {
    document.querySelector('.admin-sidebar').classList.toggle('open');
    document.getElementById('sidebar-overlay').classList.toggle('open');
  });
  document.getElementById('sidebar-overlay')?.addEventListener('click', () => {
    document.querySelector('.admin-sidebar').classList.remove('open');
    document.getElementById('sidebar-overlay').classList.remove('open');
  });

  // Logout
  document.getElementById('btn-logout')?.addEventListener('click', () => {
    if (confirm('Log out?')) adminLogout();
  });

  // Notification badge poll
  _pollNotificationBadge();
  setInterval(_pollNotificationBadge, ADMIN_CONFIG.NOTIFICATION_POLL_INTERVAL);
}

// ── Build sidebar HTML ─────────────────────────────────────────────────────────
function _injectSidebar(activePage, profile, superAdmin) {
  // Group nav items by section
  const sections = {};
  for (const item of NAV_ITEMS) {
    if (!sections[item.section]) sections[item.section] = [];
    sections[item.section].push(item);
  }

  let navHtml = '';
  for (const [sectionName, items] of Object.entries(sections)) {
    navHtml += `<div class="nav-section-label">${sectionName}</div>`;
    for (const item of items) {
      const active    = item.id === activePage ? 'active' : '';
      const adminOnly = item.adminOnly ? 'admin-only' : '';
      navHtml += `
        <a href="${item.href}" class="nav-item ${active} ${adminOnly}" data-page="${item.id}">
          <span class="nav-icon">${item.icon}</span>
          <span>${item.label}</span>
          ${item.id === 'notifications' ? '<span class="nav-badge" id="notif-badge" style="display:none"></span>' : ''}
        </a>`;
    }
  }

  const roleClass = superAdmin ? 'role-super_admin' : 'role-agent';
  const roleLabel = superAdmin ? '● Super Admin' : '● Agent';

  const sidebarHtml = `
    <aside class="admin-sidebar" id="admin-sidebar">
      <div class="sidebar-logo">
        <div class="logo-icon">🎮</div>
        <div>
          <div class="logo-text">AuraPlayFF</div>
        </div>
        <span class="logo-badge">Admin</span>
      </div>
      <div class="sidebar-role">
        Logged in as <span class="${roleClass}">${roleLabel}</span>
      </div>
      <nav class="sidebar-nav">${navHtml}</nav>
      <div class="sidebar-footer">
        <div class="sidebar-user">
          <div class="user-avatar">👤</div>
          <div class="user-info">
            <div class="user-name" id="sidebar-display-name">${profile?.display_name || 'Staff'}</div>
            <div class="user-id" id="sidebar-agent-id">${profile?.agent_id || ''}</div>
          </div>
        </div>
        <button class="btn-logout" id="btn-logout">
          <span>🚪</span> Log Out
        </button>
      </div>
    </aside>
    <div class="sidebar-overlay" id="sidebar-overlay"></div>`;

  // Insert before the first .admin-main
  const main = document.querySelector('.admin-main');
  if (main) main.insertAdjacentHTML('beforebegin', sidebarHtml);
}

// ── Build topbar HTML ──────────────────────────────────────────────────────────
function _injectTopbar(title) {
  const topbar = document.querySelector('.admin-topbar');
  if (!topbar) return;

  topbar.innerHTML = `
    <button class="topbar-hamburger" id="hamburger-btn" aria-label="Toggle sidebar">☰</button>
    <span class="topbar-title">${title}</span>
    <div class="topbar-actions">
      <button class="topbar-notif-btn" id="topbar-notif-btn"
        onclick="window.location.href='/admin/notifications.html'" title="Notifications">
        🔔
        <span class="badge" id="topbar-badge"></span>
      </button>
    </div>`;
}

// ── Notification badge ─────────────────────────────────────────────────────────
async function _pollNotificationBadge() {
  try {
    const data = await adminGet('/api/staff/notifications/unread-count');
    const count = data?.unread_count ?? 0;
    _updateBadge(count);
  } catch { /* silent fail */ }
}

function _updateBadge(count) {
  const topbarBadge = document.getElementById('topbar-badge');
  const navBadge    = document.getElementById('notif-badge');
  const btn         = document.getElementById('topbar-notif-btn');
  const label       = count > 0 ? String(count > 99 ? '99+' : count) : '';

  if (topbarBadge) {
    topbarBadge.textContent = label;
    topbarBadge.classList.toggle('visible', count > 0);
  }
  if (navBadge) {
    navBadge.textContent = label;
    navBadge.style.display = count > 0 ? 'inline-block' : 'none';
  }
}
