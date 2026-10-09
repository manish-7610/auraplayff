"""
RBAC permission constants and checker.

Super Admins automatically have ALL permissions — no DB lookup needed.
Agents must have explicit grants in staff_user_permissions.

Phase 5 additions
-----------------
Deposit permissions:
  deposit.view     — view deposit records (read-only)
  deposit.approve  — approve manual UPI deposits (credits wallet)
  deposit.reject   — reject manual UPI deposits

Withdrawal permissions:
  withdrawal.view       — view withdrawal records (read-only)
  withdrawal.process    — mark withdrawal as processing (started)
  withdrawal.approve    — mark withdrawal as paid (finalises debit)
  withdrawal.reject     — reject withdrawal (releases hold)
  withdrawal.mark_paid  — alias for approve; kept for explicit mark-paid action

Phase 6 additions
-----------------
Agent management permissions:
  agent.create             — create new agent accounts
  agent.view               — view agent profiles and details
  agent.manage             — activate/deactivate/update agents
  agent.reset_password     — reset an agent's password
  agent.view_performance   — view agent performance, salary, grace, warnings
  agent.manage_performance — grant grace, excused, issue warnings
  agent.manage_salary      — manual salary adjustment, confirm salary
  agent.manage_targets     — change daily target configuration

Agents do NOT receive any financial or agent-management permissions by default.
"""
from __future__ import annotations

from enum import StrEnum


class Permission(StrEnum):
    """All permission codes used in the system.
    Values must match what is seeded into the permissions table.
    """
    # ── Tournament management ─────────────────────────────────────────────────
    TOURNAMENT_CREATE = "tournament.create"
    TOURNAMENT_CANCEL = "tournament.cancel"
    TOURNAMENT_MANAGE_PLAYERS = "tournament.manage_players"
    TOURNAMENT_RELEASE_ROOM = "tournament.release_room"
    TOURNAMENT_ENTER_RESULTS = "tournament.enter_results"
    TOURNAMENT_VERIFY_RESULTS = "tournament.verify_results"

    # ── Deposits (Phase 5) ────────────────────────────────────────────────────
    DEPOSIT_VIEW = "deposit.view"
    DEPOSIT_APPROVE = "deposit.approve"
    DEPOSIT_REJECT = "deposit.reject"

    # ── Withdrawals (Phase 5) ─────────────────────────────────────────────────
    WITHDRAWAL_VIEW = "withdrawal.view"
    WITHDRAWAL_PROCESS = "withdrawal.process"
    WITHDRAWAL_APPROVE = "withdrawal.approve"
    WITHDRAWAL_REJECT = "withdrawal.reject"
    WITHDRAWAL_MARK_PAID = "withdrawal.mark_paid"

    # ── Users ─────────────────────────────────────────────────────────────────
    USER_VIEW = "user.view"
    USER_BAN = "user.ban"

    # ── Agent management (Phase 6) ────────────────────────────────────────────
    AGENT_CREATE = "agent.create"
    AGENT_VIEW = "agent.view"
    AGENT_MANAGE = "agent.manage"
    AGENT_RESET_PASSWORD = "agent.reset_password"
    AGENT_VIEW_PERFORMANCE = "agent.view_performance"
    AGENT_MANAGE_PERFORMANCE = "agent.manage_performance"
    AGENT_MANAGE_SALARY = "agent.manage_salary"
    AGENT_MANAGE_TARGETS = "agent.manage_targets"

    # ── Platform ──────────────────────────────────────────────────────────────
    SETTINGS_MANAGE = "settings.manage"
    WALLET_MANUAL_ADJUST = "wallet.manual_adjust"
    REPORT_VIEW = "report.view"
    AUDIT_VIEW = "audit.view"
    SCREENSHOT_VIEW = "screenshot.view"

    # ── Results history (Phase 11) ───────────────────────────────────────────
    RESULTS_HISTORY_VIEW = "results.history_view"  # read-only view of completed tournament results

    # ── Support (Phase 10) ───────────────────────────────────────────────────
    SUPPORT_VIEW = "support.view"            # view and manage player support requests

    # ── Scheduler / automation (Phase 7) ─────────────────────────────────────
    SCHEDULER_VIEW = "scheduler.view"        # view job execution history
    SCHEDULER_TRIGGER = "scheduler.trigger"  # manually trigger a job


# Human-readable descriptions for seeding
PERMISSION_DESCRIPTIONS: dict[str, str] = {
    # Tournament
    Permission.TOURNAMENT_CREATE: "Create new tournaments",
    Permission.TOURNAMENT_CANCEL: "Cancel existing tournaments",
    Permission.TOURNAMENT_MANAGE_PLAYERS: "Manage players in tournaments",
    Permission.TOURNAMENT_RELEASE_ROOM: "Release Room ID and Password",
    Permission.TOURNAMENT_ENTER_RESULTS: "Enter tournament results",
    Permission.TOURNAMENT_VERIFY_RESULTS: "Verify and finalize tournament results",
    # Deposits
    Permission.DEPOSIT_VIEW: "View deposit records (read-only)",
    Permission.DEPOSIT_APPROVE: "Approve manual UPI deposit requests — credits player wallet",
    Permission.DEPOSIT_REJECT: "Reject manual UPI deposit requests",
    # Withdrawals
    Permission.WITHDRAWAL_VIEW: "View withdrawal records (read-only)",
    Permission.WITHDRAWAL_PROCESS: "Mark withdrawal as processing (started)",
    Permission.WITHDRAWAL_APPROVE: "Mark withdrawal as paid — finalises balance debit",
    Permission.WITHDRAWAL_REJECT: "Reject withdrawal — releases balance hold back to player",
    Permission.WITHDRAWAL_MARK_PAID: "Enter payout UTR and mark withdrawal as paid",
    # Users
    Permission.USER_VIEW: "View player profiles and transaction history",
    Permission.USER_BAN: "Ban or unban player accounts",
    # Agent management (Phase 6)
    Permission.AGENT_CREATE: "Create new agent accounts",
    Permission.AGENT_VIEW: "View agent profiles and details",
    Permission.AGENT_MANAGE: "Activate, deactivate, and update agent accounts",
    Permission.AGENT_RESET_PASSWORD: "Reset an agent's login password",
    Permission.AGENT_VIEW_PERFORMANCE: "View agent daily/monthly performance and salary data",
    Permission.AGENT_MANAGE_PERFORMANCE: "Grant grace, mark excused, issue performance warnings",
    Permission.AGENT_MANAGE_SALARY: "Apply manual salary adjustments and confirm monthly salary",
    Permission.AGENT_MANAGE_TARGETS: "Configure daily tournament targets and bonus rates per tier",
    # Platform
    Permission.SETTINGS_MANAGE: "Manage platform settings",
    Permission.WALLET_MANUAL_ADJUST: "Manually adjust player wallet balances",
    Permission.REPORT_VIEW: "View financial and platform reports",
    Permission.AUDIT_VIEW: "View audit logs",
    Permission.SCREENSHOT_VIEW: "View match proof screenshots",
    # Scheduler (Phase 7)
    Permission.SCHEDULER_VIEW: "View scheduler job execution history and status",
    Permission.SCHEDULER_TRIGGER: "Manually trigger scheduled automation jobs",
    # Support (Phase 10)
    Permission.SUPPORT_VIEW: "View and manage player support requests",
    # Results history (Phase 11)
    Permission.RESULTS_HISTORY_VIEW: "View read-only completed tournament results history",
}

# All permission codes as a plain set for fast membership checks
ALL_PERMISSION_CODES: frozenset[str] = frozenset(p.value for p in Permission)

# ── Financial permission sets (for documentation/policy use) ─────────────────
FINANCIAL_PERMISSIONS: frozenset[str] = frozenset({
    Permission.DEPOSIT_APPROVE,
    Permission.DEPOSIT_REJECT,
    Permission.WITHDRAWAL_APPROVE,
    Permission.WITHDRAWAL_REJECT,
    Permission.WITHDRAWAL_PROCESS,
    Permission.WITHDRAWAL_MARK_PAID,
    Permission.WALLET_MANUAL_ADJUST,
})

# ── Admin-only permissions (agents must never receive these) ──────────────────
ADMIN_ONLY_PERMISSIONS: frozenset[str] = frozenset({
    *FINANCIAL_PERMISSIONS,
    Permission.AGENT_CREATE,
    Permission.AGENT_MANAGE,
    Permission.AGENT_RESET_PASSWORD,
    Permission.AGENT_MANAGE_PERFORMANCE,
    Permission.AGENT_MANAGE_SALARY,
    Permission.AGENT_MANAGE_TARGETS,
    Permission.SETTINGS_MANAGE,
    Permission.AUDIT_VIEW,
    Permission.SCHEDULER_VIEW,
    Permission.SCHEDULER_TRIGGER,
})
