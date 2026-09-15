#!/usr/bin/env python3
"""Erban 实时照护大屏服务入口。"""

from erban_dashboard_app.server import (
    DASHBOARD_HTML_KEY,
    ICON_PREVIEW_HTML_KEY,
    PROCESS_MANAGER_KEY,
    SESSION_AUDIT_KEY,
    create_app,
    main,
)

__all__ = [
    "DASHBOARD_HTML_KEY",
    "ICON_PREVIEW_HTML_KEY",
    "PROCESS_MANAGER_KEY",
    "SESSION_AUDIT_KEY",
    "create_app",
    "main",
]

if __name__ == "__main__":
    raise SystemExit(main())