from fastapi import APIRouter

from app.api.routes import (
    accounts,
    audit,
    auth,
    automation,
    budgets,
    categories,
    dev,
    google_oauth,
    google_sheets,
    health,
    imports,
    month_close,
    notifications,
    recurring_rules,
    reports,
    service_accounts,
    summary,
    system,
    telegram,
    transactions,
    users,
)

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(health.router, prefix="/health", tags=["health"])
api_router.include_router(system.router, prefix="/system", tags=["system"])
api_router.include_router(dev.router, prefix="/dev", tags=["development"])
api_router.include_router(auth.router, prefix="/auth", tags=["authentication"])
api_router.include_router(
    google_oauth.router,
    prefix="/integrations/google",
    tags=["google-oauth"],
)
api_router.include_router(
    google_sheets.router,
    prefix="/google-sheets",
    tags=["google-sheets"],
)
api_router.include_router(imports.router, prefix="/imports", tags=["imports"])
api_router.include_router(month_close.router, prefix="/month-close", tags=["month-close"])
api_router.include_router(users.router, tags=["users", "workspaces"])
api_router.include_router(accounts.router, prefix="/accounts", tags=["accounts"])
api_router.include_router(categories.router, prefix="/categories", tags=["categories"])
api_router.include_router(budgets.router, prefix="/budgets", tags=["budgets"])
api_router.include_router(
    recurring_rules.router, prefix="/recurring-rules", tags=["recurring-rules"]
)
api_router.include_router(automation.router, prefix="/automation", tags=["automation"])
api_router.include_router(
    service_accounts.router,
    prefix="/settings/service-accounts",
    tags=["service-accounts"],
)
api_router.include_router(
    telegram.router,
    prefix="/integrations/telegram",
    tags=["telegram-integration"],
)
api_router.include_router(
    telegram.settings_router,
    prefix="/settings/telegram",
    tags=["telegram-settings"],
)
api_router.include_router(
    notifications.router,
    prefix="/settings/notifications",
    tags=["notification-settings"],
)
api_router.include_router(transactions.router, prefix="/transactions", tags=["transactions"])
api_router.include_router(audit.router, prefix="/audit", tags=["audit"])
api_router.include_router(summary.router, tags=["summary"])
api_router.include_router(reports.router, prefix="/reports", tags=["reports"])
