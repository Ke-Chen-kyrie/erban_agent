from __future__ import annotations

import sys
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else ROOT
CONFIG_PATH = Path.cwd() / "config.yaml"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Some Linux desktop sessions export "dxcb;xcb". Qt treats the first value as
# the platform name, then fails before it can try xcb.
if os.environ.get("QT_QPA_PLATFORM", "").startswith("dxcb"):
    os.environ["QT_QPA_PLATFORM"] = "xcb"
if "cv2/qt/plugins" in os.environ.get("QT_QPA_PLATFORM_PLUGIN_PATH", ""):
    os.environ.pop("QT_QPA_PLATFORM_PLUGIN_PATH", None)

if sys.platform.startswith("linux"):
    os.environ["XMODIFIERS"] = "@im=fcitx"
    os.environ["QT_IM_MODULE"] = "fcitx"
    os.environ["GTK_IM_MODULE"] = "fcitx"

    # PySide6 wheels may not bundle the fcitx5 input context plugin. Deepin /
    # Linux desktops usually provide it in the system Qt6 plugin directory.
    # Keep PySide6 pinned to the system Qt minor version in pyproject.toml.
    pyside_plugins = (
        Path(sys.prefix)
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
        / "PySide6"
        / "Qt"
        / "plugins"
    )
    system_qt6_plugins = Path("/usr/lib/x86_64-linux-gnu/qt6/plugins")
    plugin_paths = [str(p) for p in (pyside_plugins, system_qt6_plugins) if p.exists()]
    existing_plugin_path = os.environ.get("QT_PLUGIN_PATH", "")
    if existing_plugin_path:
        plugin_paths.append(existing_plugin_path)
    if plugin_paths:
        os.environ["QT_PLUGIN_PATH"] = os.pathsep.join(dict.fromkeys(plugin_paths))

def import_check() -> int:
    from rosbags.typesys import Stores, get_typestore
    import rosbags.typesys.stores.ros2_jazzy  # noqa: F401
    import foxglove_client

    get_typestore(Stores.ROS2_JAZZY)
    foxglove_client.FoxgloveClient()
    print("IMPORT_CHECK_OK")
    return 0


def main() -> int:
    if os.environ.get("USER_FRIENDLY_IMPORT_CHECK") == "1":
        return import_check()

    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtWidgets import QApplication

    from app.config import load_config
    from ui.main_window import MainWindow

    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
    app.setApplicationName("智能体调试平台")
    app.setOrganizationName("Erban")

    window = MainWindow(load_config(CONFIG_PATH), APP_DIR)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
