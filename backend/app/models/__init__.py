"""
Import all models here so Alembic can detect them via Base.metadata.
The order matters for FK resolution during metadata inspection.
"""
from app.models.user import User
from app.models.staff_user import StaffUser
from app.models.permission import Permission, StaffUserPermission
from app.models.wallet import Wallet, WalletTransaction
from app.models.referral import ReferralRelationship
from app.models.player_stats import PlayerStats
from app.models.password_reset_token import PasswordResetToken
from app.models.refresh_token import RefreshToken
from app.models.audit_log import AuditLog
from app.models.tournament import Tournament, TournamentEntry
from app.models.prize_config import PrizeConfig
from app.models.result import TournamentResult, ResultStatus
from app.models.screenshot import ScreenshotProof, ProofReviewStatus
# Phase 5 — Deposits, Withdrawals, Webhooks, Platform Settings
from app.models.deposit import Deposit, DepositMethod, DepositStatus
from app.models.withdrawal import Withdrawal, WithdrawalStatus
from app.models.provider_event import ProviderEvent, ProviderEventStatus
from app.models.platform_setting import PlatformSetting
# Phase 6 — Agent Management, Performance, Salary
from app.models.agent_target_config import AgentTargetConfig
from app.models.agent_performance import (
    AgentDailyPerformance,
    AgentGrace,
    AgentWarning,
    AgentSalaryRecord,
    DailyPerfStatus,
    SalaryRecordStatus,
)
# Phase 7 — Scheduler, Notifications
from app.models.notification import Notification, RecipientType, NotificationType
from app.models.job_execution_log import JobExecutionLog, JobStatus

__all__ = [
    "User",
    "StaffUser",
    "Permission",
    "StaffUserPermission",
    "Wallet",
    "WalletTransaction",
    "ReferralRelationship",
    "PlayerStats",
    "PasswordResetToken",
    "RefreshToken",
    "AuditLog",
    "Tournament",
    "TournamentEntry",
    "PrizeConfig",
    "TournamentResult",
    "ResultStatus",
    "ScreenshotProof",
    "ProofReviewStatus",
    # Phase 5
    "Deposit",
    "DepositMethod",
    "DepositStatus",
    "Withdrawal",
    "WithdrawalStatus",
    "ProviderEvent",
    "ProviderEventStatus",
    "PlatformSetting",
    # Phase 6
    "AgentTargetConfig",
    "AgentDailyPerformance",
    "AgentGrace",
    "AgentWarning",
    "AgentSalaryRecord",
    "DailyPerfStatus",
    "SalaryRecordStatus",
    # Phase 7
    "Notification",
    "RecipientType",
    "NotificationType",
    "JobExecutionLog",
    "JobStatus",
]
