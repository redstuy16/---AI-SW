"""읽기 전용 대시보드 API의 호환 진입점."""
from .dashboard import (APIResponse, DashboardReadAPI, create_app, dashboard_html,
                        project_dashboard, serve)

__all__ = ["APIResponse", "DashboardReadAPI", "create_app", "dashboard_html",
           "project_dashboard", "serve"]
