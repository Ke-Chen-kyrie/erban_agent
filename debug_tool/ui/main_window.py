from __future__ import annotations

import base64
import json
import logging
import re
import shlex
import time
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PySide6.QtCore import QRegularExpression, Qt
from PySide6.QtGui import QPixmap, QRegularExpressionValidator
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QSplitter,
    QStatusBar,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.api import ApiClient, RobotCommandClient
from app.audio import AudioRecorder
from app.config import AppConfig
from app.media import CameraController
from app.results import annotate_face_detect, format_result, pretty_json
from app.workers import TaskRunner
from .widgets import ImageView


TASKS = ["register", "voiceSearch", "faceVerify", "faceDetect", "robotControl", "system"]
CAMERA_TASKS = {"register", "faceVerify", "faceDetect"}
REGISTER_RECORD_SECONDS = 15
VOICE_SEARCH_RECORD_SECONDS = 6
REGISTER_MIN_AUDIO_SECONDS = 12
REGISTER_VOICE_PROMPT = "请慢慢朗读：我是本人，正在进行声纹采集。今天天气不错，我感觉平安舒心。家人和工作人员会帮助我使用这个系统。请确认这段声音来自我本人，谢谢。"


class ClickOnlyComboBox(QComboBox):
    """下拉框只通过点击选择，避免滚动页面时误改当前值。"""

    def wheelEvent(self, event) -> None:
        event.ignore()


class MainWindow(QMainWindow):
    def __init__(self, config: AppConfig, app_root: Path) -> None:
        super().__init__()
        self.config_data = config
        self.app_root = app_root
        self.client = ApiClient(config.api_base, config.request_timeout)
        self.robot_client = RobotCommandClient(config.shell_proxy_url, config.robot_command_timeout)
        self.runner = TaskRunner()
        self.camera = CameraController(config)
        self.recorder = AudioRecorder(config.audio_samplerate)
        self.runtime_dir = self.app_root / ".runtime"
        self.robot_stats_path = self.runtime_dir / "robot_command_stats.json"
        self.rehab_history_path = self.runtime_dir / "rehab_history.json"
        self.voice_search_stats_path = self.runtime_dir / "voice_search_stats.json"
        self.face_detect_stats_path = self.runtime_dir / "face_detect_stats.json"

        # 机器人指令执行日志
        self._robot_logger = logging.getLogger("robot.command")
        self._robot_logger.setLevel(logging.INFO)
        self._robot_log_path = self.runtime_dir / "robot_commands.log"
        self._robot_log_path.parent.mkdir(parents=True, exist_ok=True)
        _handler = logging.FileHandler(str(self._robot_log_path), encoding="utf-8")
        _handler.setFormatter(logging.Formatter(
            "%(asctime)s | %(levelname)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
        ))
        self._robot_logger.addHandler(_handler)
        self._robot_logger.propagate = False  # 不输出到控制台

        self.users: list[dict[str, Any]] = []
        self.selected_user: dict[str, Any] | None = None
        self.active_task = "register"
        self.current_frame: np.ndarray | None = None
        self.camera_active = False
        self.camera_task = ""
        self.photo_bytes: bytes | None = None
        self.photo_task = ""
        self.audio_bytes: bytes | None = None
        self.audio_task = ""
        self.audio_duration = 0.0
        self.audio_target_seconds = 0
        self.audio_recording_task = ""
        self.last_result_data: Any = None
        self.robot_command_stats: dict[str, dict[str, Any]] = self.load_robot_command_stats()
        self.rehab_history: list[dict[str, Any]] = self.load_rehab_history()
        self.voice_search_stats: dict[str, Any] = self._load_api_stats(self.voice_search_stats_path)
        self.face_detect_stats: dict[str, Any] = self._load_api_stats(self.face_detect_stats_path)
        self.page_widgets: dict[str, dict[str, Any]] = {}
        self.photo_review_mode = False
        self.face_detect_realtime = False
        self.face_detect_inflight = False
        self.face_detect_pending_frame: np.ndarray | None = None
        self.face_detect_realtime_has_result = False
        self.face_detect_realtime_generation = 0
        self.face_detect_realtime_start = 0.0

        self.setWindowTitle("智能体调试平台")
        self.resize(1500, 880)
        self.setMinimumSize(1180, 720)
        self._build_ui()
        self.render_robot_stats()
        self.render_rehab_history()
        self._wire_events()
        self._activate_task_widgets("register")
        self.check_health()
        self.refresh_users()

    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("appRoot")
        outer = QVBoxLayout(root)
        outer.setContentsMargins(18, 16, 18, 14)
        outer.setSpacing(10)
        outer.addWidget(self._build_titlebar())

        self.tabs = QTabWidget()
        self.tabs.setObjectName("mainTabs")
        self._build_task_pages()
        outer.addWidget(self.tabs, 1)

        self.setCentralWidget(root)
        self.status = QStatusBar()
        self.status.setObjectName("statusBar")
        self.setStatusBar(self.status)
        self.service_label = QLabel("服务未检测")
        self.camera_label = QLabel("摄像头未开启")
        self.mic_label = QLabel("麦克风未开启")
        for label in (self.service_label, self.camera_label, self.mic_label):
            label.setObjectName("statusPill")
        self.status.addPermanentWidget(self.service_label)
        self.status.addPermanentWidget(self.camera_label)
        self.status.addPermanentWidget(self.mic_label)
        self._set_styles()

    def _build_titlebar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("topbar")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(10)
        title = QLabel("智能体调试平台")
        title.setObjectName("title")
        layout.addWidget(title)
        layout.addStretch(1)
        return bar

    def _build_task_pages(self) -> None:
        self.register_id = self._line_edit()
        self.register_id.setPlaceholderText("仅支持英文和数字")
        self.register_id.setValidator(QRegularExpressionValidator(QRegularExpression("[A-Za-z0-9]*"), self.register_id))
        self.register_name = self._line_edit()
        self.register_role = ClickOnlyComboBox()
        self.register_role.addItems(["老人", "护工", "管理员"])
        self.register_description = self._line_edit()
        register_form = QWidget()
        register_layout = QGridLayout(register_form)
        register_layout.setContentsMargins(0, 0, 0, 0)
        register_layout.setHorizontalSpacing(12)
        register_layout.setVerticalSpacing(10)
        register_layout.addWidget(self._stacked_field("用户 ID", self.register_id), 0, 0)
        register_layout.addWidget(self._stacked_field("姓名", self.register_name), 0, 1)
        register_layout.addWidget(self._stacked_field("角色", self.register_role), 1, 0)
        register_layout.addWidget(self._stacked_field("备注", self.register_description), 1, 1)
        register_layout.setColumnStretch(0, 1)
        register_layout.setColumnStretch(1, 1)
        self.tabs.addTab(self._build_register_page(register_form), "用户注册")

        self.voice_top_k = QSpinBox()
        self.voice_top_k.setRange(1, 10)
        self.voice_top_k.setValue(1)
        self.tabs.addTab(
            self._build_operation_page(
                "voiceSearch",
                self._inline_form("Top K", self.voice_top_k, "开始声纹搜索", self.submit_voice_search),
                False,
                True,
            ),
            "声纹搜索",
        )

        self.verify_user_id = self._user_id_combo("从用户库选择或输入用户 ID")
        self.tabs.addTab(
            self._build_operation_page(
                "faceVerify",
                self._inline_form("用户 ID", self.verify_user_id, "开始人脸验证", self.submit_face_verify),
                True,
                False,
            ),
            "人脸验证",
        )

        self.detect_max_faces = QSpinBox()
        self.detect_max_faces.setRange(1, 20)
        self.detect_max_faces.setValue(10)
        self.tabs.addTab(
            self._build_operation_page(
                "faceDetect",
                self._build_face_detect_form(),
                True,
                False,
            ),
            "人脸检测",
        )

        self.tabs.addTab(self._build_robot_control_page(), "机器人控制")
        self.tabs.addTab(self._build_system_page(), "系统管理")

    def _build_operation_page(self, task: str, form: QWidget, needs_camera: bool, needs_audio: bool) -> QWidget:
        page = QWidget()
        layout = QHBoxLayout(page)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(12)

        left = QFrame()
        left.setObjectName("panel")
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(14, 14, 14, 14)
        left_layout.setSpacing(12)
        left_layout.addWidget(self._section_title("任务参数"))
        left_layout.addWidget(form, 0)
        if needs_camera:
            left_layout.addWidget(self._build_camera_panel(task), 1)
        if needs_audio:
            left_layout.addWidget(self._build_audio_panel(task), 0)
        if not needs_camera:
            left_layout.addStretch(1)

        right = QFrame()
        right.setObjectName("panel")
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(14, 14, 14, 14)
        right_layout.setSpacing(12)
        right_layout.addWidget(self._build_result_panel(task), 1)
        if task in ("voiceSearch", "faceDetect"):
            right_layout.addWidget(self._build_stats_card(task), 0)

        layout.addWidget(left, 3)
        layout.addWidget(right, 2)
        self.page_widgets.setdefault(task, {})
        return page

    def _build_register_page(self, form: QWidget) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(12)

        content = QWidget()
        content_layout = QHBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(12)

        left = QWidget()
        left.setObjectName("registerSide")
        left.setMinimumWidth(460)
        left.setMaximumWidth(620)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(12)

        right = QFrame()
        right.setObjectName("panel")
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(8, 8, 8, 8)
        right_layout.setSpacing(8)
        right_layout.addWidget(self._build_camera_panel("register"), 1)

        info = QFrame()
        info.setObjectName("panel")
        info_layout = QVBoxLayout(info)
        info_layout.setContentsMargins(14, 14, 14, 14)
        info_layout.setSpacing(10)
        info_layout.addWidget(self._section_title("注册信息"))
        info_layout.addWidget(form)

        audio = self._build_audio_panel("register")
        submit = self._button("提交注册", "primary", self.submit_register)
        submit.setObjectName("submitPrimary")
        submit.setMinimumHeight(42)
        submit.setFixedWidth(380)

        audio.setMinimumHeight(260)
        audio.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        left_layout.addWidget(info, 0)
        left_layout.addWidget(audio, 1)

        content_layout.addWidget(left, 2)
        content_layout.addWidget(right, 4)

        submit_row = QHBoxLayout()
        submit_row.setContentsMargins(0, 0, 0, 0)
        submit_row.addStretch(1)
        submit_row.addWidget(submit)
        submit_row.addStretch(1)

        layout.addWidget(content, 1)
        layout.addLayout(submit_row)
        return page

    def _build_camera_panel(self, task: str) -> QWidget:
        panel = QFrame()
        panel.setObjectName("cameraCard")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)
        top = QHBoxLayout()
        top.setSpacing(8)
        source_combo = ClickOnlyComboBox()
        source_combo.addItems(["本地摄像头", "机器人摄像头"])
        topic_label = self._field_label("机器人话题")
        topic_combo = ClickOnlyComboBox()
        topic_combo.addItems(["头部摄像头", "腰部摄像头"])
        topic_combo.setEnabled(False)
        topic_label.setVisible(False)
        topic_combo.setVisible(False)
        start_btn = self._button("开启摄像头", "secondary", self.start_camera)
        capture_btn = self._button("拍照", "primary", self.capture_photo)
        detect_btn = None
        if task == "faceDetect":
            detect_btn = self._button("人脸检测", "primary", self.submit_face_detect)
            detect_btn.setMinimumHeight(36)
        stop_btn = self._button("关闭摄像头", "secondary", self.stop_camera)
        top.addWidget(self._section_title("实时采集"))
        top.addStretch(1)
        top.addWidget(self._field_label("相机源"))
        top.addWidget(source_combo)
        top.addWidget(topic_label)
        top.addWidget(topic_combo)
        camera_view = ImageView("摄像头未开启")
        camera_view.setMinimumSize(470, 264)
        photo_state = QLabel("尚未拍照")
        photo_state.setObjectName("hint")
        actions = QGridLayout()
        actions.setHorizontalSpacing(10)
        actions.addWidget(start_btn, 0, 0)
        actions.addWidget(stop_btn, 0, 1)
        actions.addWidget(capture_btn, 0, 2)
        stop_column = 3 if detect_btn is not None else 2
        if detect_btn is not None:
            actions.addWidget(detect_btn, 0, 3)
        for column in range(stop_column + 1):
            actions.setColumnStretch(column, 1)
        layout.addLayout(top)
        layout.addWidget(camera_view, 1)
        layout.addLayout(actions)
        layout.addWidget(photo_state)
        source_combo.currentIndexChanged.connect(self.on_camera_source_changed)
        topic_combo.currentIndexChanged.connect(self.restart_robot_if_needed)
        self.page_widgets.setdefault(task, {}).update({
            "source_combo": source_combo,
            "topic_label": topic_label,
            "topic_combo": topic_combo,
            "start_camera_btn": start_btn,
            "capture_btn": capture_btn,
            "face_detect_once_btn": detect_btn,
            "stop_camera_btn": stop_btn,
            "camera_view": camera_view,
            "photo_state": photo_state,
        })
        return panel

    def _build_audio_panel(self, task: str) -> QWidget:
        panel = QFrame()
        panel.setObjectName("sideCard")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)
        top = QHBoxLayout()
        audio_time = QLabel("00:00")
        audio_time.setObjectName("timer")
        meter = QProgressBar()
        meter.setRange(0, 100)
        meter.setFormat("")
        meter.setTextVisible(False)
        meter.setObjectName("micMeter")
        top.addWidget(audio_time)
        top.addWidget(self._field_label("录音进度"))
        top.addWidget(meter, 1)
        actions = QGridLayout()
        record_seconds = REGISTER_RECORD_SECONDS if task == "register" else VOICE_SEARCH_RECORD_SECONDS
        record_btn = self._button(
            "录制",
            "primary",
            lambda checked=False, seconds=record_seconds: self.start_recording(seconds),
        )
        record_btn.setMinimumHeight(38)
        pause_btn = None
        if task == "register":
            actions.addWidget(record_btn, 0, 0)
            pause_btn = self._button("暂停", "secondary", self.toggle_recording_pause)
            pause_btn.setEnabled(False)
            actions.addWidget(pause_btn, 0, 1)
        else:
            actions.addWidget(record_btn, 0, 0, 1, 2)
        stop_btn = self._button("停止", "secondary", self.stop_recording)
        play_btn = self._button("回放", "secondary", self.play_audio)
        actions.addWidget(stop_btn, 1, 0)
        actions.addWidget(play_btn, 1, 1)
        review = QLabel("录音：未采集")
        review.setObjectName("statusText")
        layout.addWidget(self._section_title("声纹采集"))
        layout.addLayout(top)
        layout.addLayout(actions)
        layout.addWidget(review)
        if task == "register":
            prompt = QLabel(REGISTER_VOICE_PROMPT)
            prompt.setObjectName("voicePrompt")
            prompt.setWordWrap(True)
            prompt.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
            layout.addWidget(prompt)
            layout.addStretch(1)
        self.page_widgets.setdefault(task, {}).update({
            "audio_time": audio_time,
            "audio_meter": meter,
            "audio_review": review,
            "audio_record_btn": record_btn,
            "audio_pause_btn": pause_btn,
            "audio_stop_btn": stop_btn,
            "audio_play_btn": play_btn,
        })
        return panel

    def _build_result_panel(self, task: str) -> QWidget:
        panel = QFrame()
        panel.setObjectName("sideCard")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        header = QHBoxLayout()
        header.addWidget(self._section_title("识别结果"))
        header.addStretch(1)
        header.addWidget(self._button("JSON", "secondary", self.show_json_result))
        text = QTextEdit()
        text.setObjectName("resultText")
        text.setReadOnly(True)
        text.setText("请选择功能并采集所需的人脸照片或声纹音频。")
        layout.addLayout(header)
        layout.addWidget(text, 1)
        self.page_widgets.setdefault(task, {})["result_text"] = text
        return panel

    def _build_stats_card(self, task: str) -> QWidget:
        card = QFrame()
        card.setObjectName("sideCard")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(6)
        layout.addWidget(self._section_title("耗时统计"))

        rows_def: list[tuple[str, str]] = [
            ("count", "调用次数"),
            ("last", "最新耗时"),
            ("avg", "平均耗时"),
            ("range", "最快 / 最慢"),
            ("success_rate", "成功率"),
            ("last_time", "最后调用"),
        ]
        labels: dict[str, QLabel] = {}
        for key, display in rows_def:
            row = QHBoxLayout()
            row.setSpacing(6)
            field_label = QLabel(display)
            field_label.setObjectName("fieldLabel")
            value = QLabel("—")
            value.setObjectName("statValue")
            row.addWidget(field_label)
            row.addStretch(1)
            row.addWidget(value)
            layout.addLayout(row)
            labels[key] = value

        self.page_widgets.setdefault(task, {})["stats_labels"] = labels
        return card

    def _build_robot_control_page(self) -> QWidget:
        self.robot_contexts: dict[str, dict[str, Any]] = {}
        self.robot_user_combos: list[QComboBox] = []
        self.robot_page_stack = QStackedWidget()
        self.robot_pages = {
            "home": self._build_robot_home_page(),
            "feeding": self._build_robot_feeding_page(),
            "water": self._build_robot_water_page(),
            "rehab": self._build_robot_rehab_page(),
        }
        for page in self.robot_pages.values():
            self.robot_page_stack.addWidget(page)
        self.active_robot_page = "home"
        self.robot_page_stack.setCurrentWidget(self.robot_pages["home"])
        self.page_widgets["robotControl"] = {
            "result_text": self.robot_contexts["home"]["output"],
        }
        return self.robot_page_stack

    def _build_robot_home_page(self) -> QWidget:
        page = QWidget()
        layout = QHBoxLayout(page)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(12)

        left = QFrame()
        left.setObjectName("panel")
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(14, 14, 14, 14)
        left_layout.setSpacing(12)

        header = QHBoxLayout()
        header.addWidget(self._section_title("机器人控制"))
        header.addStretch(1)
        proxy = QLabel(f"服务端url：{self.config_data.shell_proxy_url}")
        proxy.setObjectName("hint")
        header.addWidget(proxy)
        left_layout.addLayout(header)

        controls = QWidget()
        controls_layout = QVBoxLayout(controls)
        controls_layout.setContentsMargins(0, 0, 0, 0)
        controls_layout.setSpacing(12)
        controls_layout.addWidget(self._robot_basic_action_group())
        controls_layout.addWidget(self._robot_info_query_group())
        controls_layout.addWidget(self._robot_scene_entry_group())
        controls_layout.addStretch(1)

        controls_scroll = QScrollArea()
        controls_scroll.setObjectName("controlScroll")
        controls_scroll.setFrameShape(QFrame.Shape.NoFrame)
        controls_scroll.setWidgetResizable(True)
        controls_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        controls_scroll.setWidget(controls)
        left_layout.addWidget(controls_scroll, 1)

        right = QFrame()
        right.setObjectName("panel")
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(14, 14, 14, 14)
        right_layout.setSpacing(12)

        output_card = QFrame()
        output_card.setObjectName("sideCard")
        output_card.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        output_layout = QVBoxLayout(output_card)
        output_layout.setContentsMargins(12, 12, 12, 12)
        output_layout.setSpacing(8)
        status_row = QHBoxLayout()
        status_row.addWidget(self._section_title("执行结果"))
        status_row.addStretch(1)
        self.robot_status_label = QLabel("等待命令")
        self.robot_status_label.setObjectName("hint")
        status_row.addWidget(self.robot_status_label)

        self.robot_output = QTextEdit()
        self.robot_output.setObjectName("resultText")
        self.robot_output.setReadOnly(True)
        self.robot_output.setText("点击左侧命令后，会在这里显示 shell proxy 返回内容。")

        stats_card = QFrame()
        stats_card.setObjectName("sideCard")
        stats_card.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        stats_layout = QVBoxLayout(stats_card)
        stats_layout.setContentsMargins(12, 12, 12, 12)
        stats_layout.setSpacing(8)
        self.robot_stats_table = QTableWidget(0, 6)
        self.robot_stats_table.setObjectName("userTable")
        self.robot_stats_table.setHorizontalHeaderLabels(["命令", "次数", "最新耗时", "平均耗时", "状态", "执行成功率"])
        self.robot_stats_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, 6):
            self.robot_stats_table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.robot_stats_table.verticalHeader().setDefaultSectionSize(30)
        self.robot_stats_table.setAlternatingRowColors(True)
        self.robot_stats_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.robot_stats_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.robot_stats_table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.robot_stats_table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.robot_stats_table.setWordWrap(False)

        output_layout.addLayout(status_row)
        output_layout.addWidget(self.robot_output, 1)
        stats_header = QHBoxLayout()
        stats_header.addWidget(self._section_title("耗时统计"))
        stats_header.addStretch(1)
        stats_header.addWidget(self._button("清空", "danger", self.clear_robot_stats))
        stats_layout.addLayout(stats_header)
        stats_layout.addWidget(self.robot_stats_table, 1)

        right_layout.addWidget(output_card, 1)
        right_layout.addWidget(stats_card, 1)

        layout.addWidget(left, 7)
        layout.addWidget(right, 5)
        self._register_robot_context(
            "home",
            status=self.robot_status_label,
            output=self.robot_output,
        )
        return page

    def _build_robot_feeding_page(self) -> QWidget:
        page, controls_layout, _right_layout = self._build_robot_scene_page(
            "feeding", "喂饭", needs_user=True
        )
        controls_layout.addWidget(self._robot_turn_group())
        controls_layout.addWidget(self._robot_command_group(
            "碗勺操作",
            [
                ("拾取碗勺", "拾取碗勺", "pick bowl", False),
                ("放置碗勺", "放置碗勺", "place bowl", False),
            ],
        ))
        controls_layout.addWidget(self._robot_command_group(
            "喂饭操作",
            [
                ("舀一勺", "舀一勺", "scoop", False),
                ("递送食物", "递送食物", "deliver-spoon -u {user_id}", True),
                ("撤回勺子", "撤回勺子", "retract-spoon", False),
            ],
        ))
        controls_layout.addStretch(1)
        self._add_robot_back_button(controls_layout)
        return page

    def _build_robot_water_page(self) -> QWidget:
        page, controls_layout, _right_layout = self._build_robot_scene_page(
            "water", "喂水", needs_user=True
        )
        controls_layout.addWidget(self._robot_turn_group())
        controls_layout.addWidget(self._robot_command_group(
            "水杯操作",
            [
                ("拾取水杯", "拾取水杯", "pick cup", False),
                ("放置水杯", "放置水杯", "place cup", False),
            ],
        ))
        controls_layout.addWidget(self._robot_command_group(
            "喂水操作",
            [
                ("放低水杯", "放低水杯", "lower-cup", False),
                ("递送水杯", "递送水杯", "deliver-cup -u {user_id}", True),
                ("撤回水杯", "撤回水杯", "retract-cup", False),
            ],
        ))
        controls_layout.addStretch(1)
        self._add_robot_back_button(controls_layout)
        return page

    def _build_robot_rehab_page(self) -> QWidget:
        page, controls_layout, right_layout = self._build_robot_scene_page(
            "rehab", "康复运动", needs_user=False
        )
        controls_layout.addWidget(self._robot_turn_group())

        hand_card = QFrame()
        hand_card.setObjectName("sideCard")
        hand_layout = QGridLayout(hand_card)
        hand_layout.setContentsMargins(12, 12, 12, 12)
        hand_layout.setHorizontalSpacing(8)
        hand_layout.setVerticalSpacing(8)
        title = self._section_title("手臂动作")
        hand_layout.addWidget(title, 0, 0, 1, 3)

        self.rehab_offset = self._value_combo([
            ("低", "low"), ("中", "mid"), ("高", "high"),
        ], "mid")
        self.rehab_raise_level = self._value_combo([
            ("轻微", "slight"), ("中等", "moderate"), ("显著", "significant"),
        ], "significant")
        self.rehab_lower_level = self._value_combo([
            ("轻微", "slight"), ("中等", "moderate"), ("显著", "significant"),
        ], "slight")
        self._add_robot_parameter_action(
            hand_layout, 1, "伸出手臂", self.rehab_offset,
            lambda: self.run_robot_combo_command(
                "伸出手臂", "extend-hand --offset={value}", self.rehab_offset
            ),
        )
        self._add_robot_parameter_action(
            hand_layout, 2, "抬起手", self.rehab_raise_level,
            lambda: self.run_robot_combo_command(
                "抬起手", "raise-hand --level={value}", self.rehab_raise_level
            ),
        )
        self._add_robot_parameter_action(
            hand_layout, 3, "放下手", self.rehab_lower_level,
            lambda: self.run_robot_combo_command(
                "放下手", "lower-hand --level={value}", self.rehab_lower_level
            ),
        )
        retract = self._button(
            "收回手臂", "secondary",
            lambda: self.run_robot_command("收回手臂", "retract-hand"),
        )
        hand_layout.addWidget(retract, 4, 1, 1, 2)
        self._track_robot_button(retract)
        controls_layout.addWidget(hand_card)

        trace_card = QFrame()
        trace_card.setObjectName("sideCard")
        trace_layout = QGridLayout(trace_card)
        trace_layout.setContentsMargins(12, 12, 12, 12)
        trace_layout.setHorizontalSpacing(8)
        trace_layout.setVerticalSpacing(8)
        trace_layout.addWidget(self._section_title("轨迹运动"), 0, 0, 1, 3)
        self.rehab_trace_type = ClickOnlyComboBox()
        self.rehab_trace_type.addItem("up_and_down", "up_and_down")
        self.rehab_trace_type.setEnabled(False)
        trace_layout.addWidget(self._field_label("轨迹类型"), 1, 0)
        trace_layout.addWidget(self.rehab_trace_type, 1, 1, 1, 2)

        self.rehab_speed = self._value_combo([
            ("慢", "slow"), ("正常", "normal"), ("快", "fast"),
        ], "normal")
        self.rehab_scale = self._value_combo([
            ("短", "short"), ("中", "mid"), ("长", "long"),
        ], "mid")
        self.rehab_repeat = QSpinBox()
        self.rehab_repeat.setRange(1, 20)
        self.rehab_repeat.setValue(1)
        trace_layout.addWidget(self._field_label("速度"), 2, 0)
        trace_layout.addWidget(self.rehab_speed, 2, 1, 1, 2)
        trace_layout.addWidget(self._field_label("幅度"), 3, 0)
        trace_layout.addWidget(self.rehab_scale, 3, 1, 1, 2)
        trace_layout.addWidget(self._field_label("重复次数"), 4, 0)
        trace_layout.addWidget(self.rehab_repeat, 4, 1, 1, 2)
        execute_trace = self._button("执行轨迹", "primary", self.run_rehab_trace)
        trace_layout.addWidget(execute_trace, 5, 1, 1, 2)
        self._track_robot_button(execute_trace)
        controls_layout.addWidget(trace_card)
        controls_layout.addStretch(1)
        self._add_robot_back_button(controls_layout)

        history_card = QFrame()
        history_card.setObjectName("sideCard")
        history_layout = QVBoxLayout(history_card)
        history_layout.setContentsMargins(12, 12, 12, 12)
        history_layout.setSpacing(8)
        history_header = QHBoxLayout()
        history_header.addWidget(self._section_title("康复执行记录"))
        history_header.addStretch(1)
        history_header.addWidget(
            self._button("一键删除", "danger", self.clear_rehab_history)
        )
        history_layout.addLayout(history_header)
        self.rehab_history_table = QTableWidget(0, 5)
        self.rehab_history_table.setObjectName("userTable")
        self.rehab_history_table.setHorizontalHeaderLabels(
            ["动作", "参数", "执行时间", "耗时", "结果"]
        )
        self.rehab_history_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        for column in (0, 2, 3, 4):
            self.rehab_history_table.horizontalHeader().setSectionResizeMode(
                column, QHeaderView.ResizeMode.ResizeToContents
            )
        self.rehab_history_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.rehab_history_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.rehab_history_table.setAlternatingRowColors(True)
        history_layout.addWidget(self.rehab_history_table)
        right_layout.addWidget(history_card, 1)
        self.robot_contexts["rehab"]["history"] = self.rehab_history_table
        return page

    def _build_robot_scene_page(
        self, key: str, title: str, needs_user: bool
    ) -> tuple[QWidget, QVBoxLayout, QVBoxLayout]:
        page = QWidget()
        layout = QHBoxLayout(page)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(12)

        left = QFrame()
        left.setObjectName("panel")
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(14, 14, 14, 14)
        left_layout.setSpacing(12)
        header = QHBoxLayout()
        header.addWidget(self._section_title(title))
        header.addStretch(1)
        proxy = QLabel(f"服务端url：{self.config_data.shell_proxy_url}")
        proxy.setObjectName("hint")
        header.addWidget(proxy)
        left_layout.addLayout(header)

        user_combo = None
        if needs_user:
            user_combo = self._user_id_combo("从用户列表选择或输入用户 ID")
            self.robot_user_combos.append(user_combo)
            user_row = QHBoxLayout()
            user_row.addWidget(self._field_label("用户 ID"))
            user_row.addWidget(user_combo, 1)
            left_layout.addLayout(user_row)

        controls = QWidget()
        controls_layout = QVBoxLayout(controls)
        controls_layout.setContentsMargins(0, 0, 0, 0)
        controls_layout.setSpacing(12)
        controls_scroll = QScrollArea()
        controls_scroll.setObjectName("controlScroll")
        controls_scroll.setFrameShape(QFrame.Shape.NoFrame)
        controls_scroll.setWidgetResizable(True)
        controls_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        controls_scroll.setWidget(controls)
        left_layout.addWidget(controls_scroll, 1)

        right = QFrame()
        right.setObjectName("panel")
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(14, 14, 14, 14)
        right_layout.setSpacing(12)
        result_card = QFrame()
        result_card.setObjectName("sideCard")
        result_layout = QVBoxLayout(result_card)
        result_layout.setContentsMargins(12, 12, 12, 12)
        result_layout.setSpacing(8)
        status_row = QHBoxLayout()
        status_row.addWidget(self._section_title("执行结果"))
        status_row.addStretch(1)
        status = QLabel("等待命令")
        status.setObjectName("hint")
        status_row.addWidget(status)
        output = QTextEdit()
        output.setObjectName("resultText")
        output.setReadOnly(True)
        output.setText("点击左侧命令后，会在这里显示 shell proxy 返回内容。")
        result_layout.addLayout(status_row)
        result_layout.addWidget(output, 1)
        right_layout.addWidget(result_card, 1)

        layout.addWidget(left, 7)
        layout.addWidget(right, 5)
        self._register_robot_context(key, user_combo=user_combo, status=status, output=output)
        return page, controls_layout, right_layout

    def _add_robot_back_button(self, layout: QVBoxLayout) -> None:
        row = QHBoxLayout()
        row.addStretch(1)
        back = self._button(
            "←  返回机器人控制",
            "navigation",
            lambda: self.show_robot_page("home"),
        )
        back.setProperty("robotNavigation", True)
        back.setMinimumWidth(190)
        back.setMinimumHeight(42)
        row.addWidget(back)
        row.addStretch(1)
        layout.addLayout(row)

    def _register_robot_context(
        self,
        key: str,
        *,
        status: QLabel,
        output: QTextEdit,
        user_combo: QComboBox | None = None,
    ) -> None:
        self.robot_contexts[key] = {
            "status": status,
            "output": output,
            "user_combo": user_combo,
            "buttons": [],
            "busy": False,
        }

    def show_robot_page(self, key: str) -> None:
        page = self.robot_pages.get(key)
        if page is None:
            return
        self.active_robot_page = key
        self.robot_page_stack.setCurrentWidget(page)

    def _robot_scene_entry_group(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("sideCard")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        layout.addWidget(self._section_title("专项操作"))
        grid = QGridLayout()
        entries = [
            ("进入喂饭", "feeding"),
            ("进入喂水", "water"),
            ("进入康复运动", "rehab"),
        ]
        for column, (text, key) in enumerate(entries):
            button = self._button(
                text, "primary",
                lambda _checked=False, page_key=key: self.show_robot_page(page_key),
            )
            button.setMinimumHeight(42)
            grid.addWidget(button, 0, column)
        layout.addLayout(grid)
        return panel

    def _robot_turn_group(self) -> QWidget:
        return self._robot_command_group(
            "转向",
            [
                ("转向用户", "转向用户", "turn -t user", False),
                ("转向桌子", "转向桌子", "turn -t table", False),
            ],
        )

    def _value_combo(self, items: list[tuple[str, str]], default: str) -> QComboBox:
        combo = ClickOnlyComboBox()
        for text, value in items:
            combo.addItem(text, value)
        index = combo.findData(default)
        if index >= 0:
            combo.setCurrentIndex(index)
        return combo

    def _add_robot_parameter_action(
        self,
        layout: QGridLayout,
        row: int,
        label: str,
        field: QWidget,
        callback,
    ) -> None:
        layout.addWidget(self._field_label(label), row, 0)
        layout.addWidget(field, row, 1)
        button = self._button("执行", "secondary", callback)
        layout.addWidget(button, row, 2)
        self._track_robot_button(button)

    def _track_robot_button(self, button: QPushButton) -> None:
        context = self.robot_contexts.get(getattr(self, "active_robot_page", ""), {})
        if context:
            context.setdefault("buttons", []).append(button)

    def _robot_basic_action_group(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("sideCard")
        panel.setMinimumHeight(205)
        panel.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        layout.addWidget(self._section_title("基础动作"))

        self.robot_turn_target = ClickOnlyComboBox()
        self.robot_turn_target.setMinimumHeight(34)
        self.robot_turn_target.addItem("用户", ("user", "用户"))
        self.robot_turn_target.addItem("桌子", ("table", "桌子"))
        self.robot_pick_target = ClickOnlyComboBox()
        self.robot_pick_target.setMinimumHeight(34)
        self.robot_pick_target.addItem("水杯", ("cup", "水杯"))
        self.robot_pick_target.addItem("碗勺", ("bowl", "碗勺"))
        self.robot_place_target = ClickOnlyComboBox()
        self.robot_place_target.setMinimumHeight(34)
        self.robot_place_target.addItem("水杯", ("cup", "水杯"))
        self.robot_place_target.addItem("碗勺", ("bowl", "碗勺"))

        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(8)
        self._add_robot_select_command(grid, 0, "转向", self.robot_turn_target, "turn -t {value}", "转向{label}")
        self._add_robot_select_command(grid, 1, "拾取", self.robot_pick_target, "pick {value}", "拾取{label}")
        self._add_robot_select_command(grid, 2, "放置", self.robot_place_target, "place {value}", "放置{label}")
        status_button = self._button("获取状态", "secondary", lambda: self.run_robot_command("获取状态", "get-status"))
        status_button.setMinimumHeight(34)
        grid.addWidget(status_button, 3, 1, 1, 2)
        layout.addLayout(grid)
        return panel

    def _add_robot_select_command(
        self,
        layout: QGridLayout,
        row: int,
        action: str,
        combo: QComboBox,
        template: str,
        label_template: str,
    ) -> None:
        layout.addWidget(self._field_label(action), row, 0)
        layout.addWidget(combo, row, 1)
        button = self._button(
            "执行",
            "secondary",
            lambda _checked=False, c=combo, t=template, lt=label_template: self.run_robot_select_command(c, t, lt),
        )
        button.setMinimumHeight(34)
        layout.addWidget(button, row, 2)

    def _robot_command_group(self, title: str, commands: list[tuple[str, str, str, bool]]) -> QWidget:
        panel = QFrame()
        panel.setObjectName("sideCard")
        panel.setMinimumHeight(125)
        panel.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        layout.addWidget(self._section_title(title))
        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(8)
        for index, (text, label, template, needs_user) in enumerate(commands):
            button = self._button(
                text,
                "secondary",
                lambda _checked=False, l=label, t=template, n=needs_user: self.run_robot_command(l, t, n),
            )
            button.setMinimumHeight(36)
            grid.addWidget(button, index // 3, index % 3)
        layout.addLayout(grid)
        return panel

    def _robot_info_query_group(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("sideCard")
        panel.setMinimumHeight(200)
        panel.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        layout.addWidget(self._section_title("信息查询"))

        self.robot_search_query = self._line_edit()
        self.robot_search_query.setMinimumHeight(34)
        self.robot_search_query.setPlaceholderText("搜索关键词")
        self.robot_weather_city = self._line_edit()
        self.robot_weather_city.setMinimumHeight(34)
        self.robot_weather_city.setPlaceholderText("城市")

        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(8)
        grid.addWidget(self.robot_search_query, 0, 0, 1, 2)
        search_button = self._button(
            "联网搜索",
            "secondary",
            lambda: self.run_robot_input_command(
                "联网搜索",
                "search {value}",
                self.robot_search_query,
                "请输入搜索关键词",
            ),
        )
        search_button.setMinimumHeight(34)
        grid.addWidget(search_button, 0, 2)
        grid.addWidget(self.robot_weather_city, 1, 0, 1, 2)
        weather_button = self._button(
            "查询天气",
            "secondary",
            lambda: self.run_robot_input_command(
                "天气查询",
                "weather {value}",
                self.robot_weather_city,
                "请输入城市",
            ),
        )
        weather_button.setMinimumHeight(34)
        grid.addWidget(weather_button, 1, 2)
        time_button = self._button("当前时间", "secondary", lambda: self.run_robot_command("当前时间", "cur-time"))
        time_button.setMinimumHeight(34)
        grid.addWidget(time_button, 2, 0, 1, 3)
        layout.addLayout(grid)
        return panel

    def _build_system_page(self) -> QWidget:
        page = QWidget()
        layout = QHBoxLayout(page)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(12)

        left = QFrame()
        left.setObjectName("panel")
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(14, 14, 14, 14)
        left_layout.setSpacing(10)
        header = QHBoxLayout()
        header.addWidget(self._section_title("系统管理"))
        header.addStretch(1)
        header.addWidget(self._button("刷新", "secondary", self.refresh_and_cleanup))
        header.addWidget(self._button("一键清库", "danger", self.clear_user_database))
        header.addWidget(self._button("删除", "danger", self.delete_selected_user))
        self.search_input = self._line_edit()
        self.search_input.setPlaceholderText("搜索 ID、姓名、角色或备注")
        self.user_table = QTableWidget(0, 3)
        self.user_table.setObjectName("userTable")
        self.user_table.setHorizontalHeaderLabels(["ID", "姓名", "角色"])
        self.user_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.user_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.user_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.user_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        left_layout.addLayout(header)
        left_layout.addWidget(self.search_input)
        left_layout.addWidget(self.user_table, 1)

        right = QFrame()
        right.setObjectName("panel")
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(14, 14, 14, 14)
        right_layout.setSpacing(10)
        right_layout.addWidget(self._section_title("用户详情"))
        self.user_face_view = ImageView("请选择用户查看照片")
        self.user_face_view.setMinimumSize(260, 190)
        self.user_face_view.setMaximumHeight(280)
        self.detail_text = QTextEdit()
        self.detail_text.setObjectName("detailText")
        self.detail_text.setReadOnly(True)
        right_layout.addWidget(self.user_face_view, 0)
        right_layout.addWidget(self.detail_text, 1)
        self.page_widgets["system"] = {"result_text": self.detail_text}
        layout.addWidget(left, 3)
        layout.addWidget(right, 2)
        return page

    def _inline_form(self, label: str, field: QWidget, button_text: str, callback) -> QWidget:
        form = QWidget()
        layout = QHBoxLayout(form)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        field.setMinimumWidth(160)
        field.setMaximumWidth(240)
        button = self._button(button_text, "primary", callback)
        button.setMinimumWidth(132)
        layout.addWidget(self._field_label(label), 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(field, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(button, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addStretch(1)
        return form

    def _build_face_detect_form(self) -> QWidget:
        form = QWidget()
        layout = QHBoxLayout(form)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        self.detect_max_faces.setMinimumWidth(160)
        self.detect_max_faces.setMaximumWidth(240)
        realtime_btn = self._button("开启实时检测", "primary", self.toggle_realtime_face_detect)
        realtime_btn.setMinimumWidth(132)
        self.page_widgets.setdefault("faceDetect", {})["realtime_face_detect_btn"] = realtime_btn
        layout.addWidget(self._field_label("最大人脸数"), 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self.detect_max_faces, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(realtime_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addStretch(1)
        return form

    def _add_form_field(self, layout: QGridLayout, row: int, column: int, label_text: str, field: QWidget) -> None:
        label = self._field_label(label_text)
        label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        field.setMinimumWidth(140)
        layout.addWidget(label, row, column)
        layout.addWidget(field, row, column + 1)

    def _stacked_field(self, label_text: str, field: QWidget) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        field.setMinimumWidth(190)
        layout.addWidget(self._field_label(label_text))
        layout.addWidget(field)
        return box

    def _line_edit(self) -> QLineEdit:
        field = QLineEdit()
        field.setAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled, True)
        field.setInputMethodHints(Qt.InputMethodHint.ImhNone)
        return field

    def _user_id_combo(self, placeholder: str) -> QComboBox:
        combo = ClickOnlyComboBox()
        combo.setEditable(True)
        combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        combo.lineEdit().setPlaceholderText(placeholder)
        combo.lineEdit().setAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled, True)
        combo.lineEdit().setInputMethodHints(Qt.InputMethodHint.ImhNone)
        return combo

    def _button(self, text: str, role: str, callback) -> QPushButton:
        button = QPushButton(text)
        button.setObjectName(role)
        button.clicked.connect(callback)
        return button

    def _section_title(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("sectionTitle")
        return label

    def _field_label(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("fieldLabel")
        return label

    def _wire_events(self) -> None:
        self.tabs.currentChanged.connect(self.on_tab_changed)
        self.search_input.textChanged.connect(self.render_users)
        self.user_table.itemSelectionChanged.connect(self.on_user_select)
        self.camera.frame.connect(self.on_camera_frame)
        self.camera.status.connect(self.on_camera_status)
        self.camera.error.connect(self.on_camera_error)
        self.recorder.elapsed_changed.connect(self.on_record_elapsed)
        self.recorder.finished.connect(self.on_audio_done)
        self.recorder.failed.connect(self.on_audio_error)
        self.recorder.playback_failed.connect(self.on_playback_error)
        self.recorder.state_changed.connect(self.mic_label.setText)

    def _activate_task_widgets(self, task: str) -> None:
        widgets = self.page_widgets.get(task, {})
        self.source_combo = widgets.get("source_combo")
        self.topic_label = widgets.get("topic_label")
        self.topic_combo = widgets.get("topic_combo")
        self.start_camera_btn = widgets.get("start_camera_btn")
        self.capture_btn = widgets.get("capture_btn")
        self.face_detect_once_btn = widgets.get("face_detect_once_btn")
        self.realtime_face_detect_btn = widgets.get("realtime_face_detect_btn")
        self.stop_camera_btn = widgets.get("stop_camera_btn")
        self.camera_view = widgets.get("camera_view")
        self.photo_state = widgets.get("photo_state")
        self.audio_time = widgets.get("audio_time", QLabel("00:00"))
        self.audio_meter = widgets.get("audio_meter", QProgressBar())
        self.audio_review = widgets.get("audio_review", QLabel(""))
        self.audio_pause_btn = widgets.get("audio_pause_btn")
        self.result_text = widgets.get("result_text", getattr(self, "result_text", QTextEdit()))
        self._sync_audio_controls()
        if task in ("voiceSearch", "faceDetect"):
            self._render_api_stats(task)
        if task == "faceDetect":
            self._sync_face_detect_realtime_controls()

    def _set_styles(self) -> None:
        self.setStyleSheet("""
            QWidget {
                color: #1f2a33;
                font-size: 14px;
                selection-background-color: #1c6c84;
                selection-color: #ffffff;
            }
            QWidget#appRoot { background: #edf2f4; }
            QFrame#topbar, QFrame#panel {
                background: #ffffff;
                border: 1px solid #d5dee3;
                border-radius: 8px;
            }
            QFrame#cameraCard, QFrame#sideCard {
                background: #f8fbfc;
                border: 1px solid #d7e1e6;
                border-radius: 8px;
            }
            QLabel#title {
                color: #14242d;
                font-size: 22px;
                font-weight: 700;
            }
            QLabel#muted, QLabel#fieldLabel {
                color: #647780;
                font-size: 12px;
            }
            QLabel#sectionTitle {
                color: #172832;
                font-size: 15px;
                font-weight: 700;
            }
            QLabel#statValue {
                color: #1a2d38;
                font-size: 14px;
                font-weight: 600;
            }
            QLabel#hint, QLabel#statusText {
                color: #60737d;
                font-size: 13px;
            }
            QLabel#voicePrompt {
                color: #314b57;
                font-size: 13px;
                background: #eef5f7;
                border: 1px solid #d7e1e6;
                border-radius: 6px;
                padding: 10px;
            }
            QLabel#timer {
                color: #10232b;
                font-family: Consolas, monospace;
                font-size: 24px;
                font-weight: 700;
                min-width: 72px;
            }
            QLabel#statusPill {
                background: #eef5f7;
                color: #315462;
                border: 1px solid #d2e1e6;
                border-radius: 10px;
                padding: 3px 10px;
                margin-left: 8px;
            }
            QLabel#imageView {
                background: #edf2f4;
                color: #60737d;
                border: 1px solid #cfdbe1;
                border-radius: 8px;
                padding: 4px;
            }
            QPushButton {
                min-height: 28px;
                padding: 7px 13px;
                border-radius: 5px;
                border: 1px solid #b9c8cf;
                background: #f7fafb;
                color: #1e2e36;
            }
            QPushButton:hover { background: #edf5f7; border-color: #91aab4; }
            QPushButton:pressed { background: #dbe8ec; border-color: #78939e; }
            QPushButton:disabled { color: #9aa9af; background: #f2f5f6; border-color: #d7e0e4; }
            QPushButton#primary {
                background: #1c6c84;
                color: #ffffff;
                border-color: #15566a;
                font-weight: 600;
            }
            QPushButton#primary:hover { background: #175c70; }
            QPushButton#primary:pressed { background: #124b5c; border-color: #0e3e4c; }
            QPushButton#primary:disabled { background: #a9c3cc; color: #eef5f7; }
            QPushButton#submitPrimary {
                background: #1c6c84;
                color: #ffffff;
                border: 1px solid #15566a;
                border-radius: 6px;
                font-size: 15px;
                font-weight: 700;
                padding: 9px 14px;
            }
            QPushButton#submitPrimary:hover { background: #175c70; }
            QPushButton#submitPrimary:pressed { background: #124b5c; }
            QPushButton#secondary {
                background: #eef5f7;
                color: #254653;
                border-color: #c8d9df;
            }
            QPushButton#secondary:hover { background: #e2eef2; border-color: #a9c3cc; }
            QPushButton#secondary:pressed { background: #d2e2e8; border-color: #8dafbb; }
            QPushButton#navigation {
                background: #244957;
                color: #ffffff;
                border-color: #193944;
                font-size: 15px;
                font-weight: 700;
                padding: 9px 20px;
            }
            QPushButton#navigation:hover {
                background: #1c6c84;
                border-color: #15566a;
            }
            QPushButton#navigation:pressed {
                background: #173d49;
                border-color: #102f38;
            }
            QPushButton#danger {
                background: #b33f49;
                color: white;
                border-color: #96313a;
                font-weight: 600;
            }
            QPushButton#danger:hover { background: #9f343e; border-color: #842a32; }
            QPushButton#danger:pressed { background: #842a32; border-color: #6f232a; }
            QLineEdit, QSpinBox, QComboBox {
                min-height: 30px;
                border: 1px solid #c9d6dc;
                border-radius: 5px;
                padding: 4px 8px;
                background: #ffffff;
            }
            QLineEdit:focus, QSpinBox:focus, QComboBox:focus { border-color: #1c6c84; }
            QTextEdit, QTableWidget {
                border: 1px solid #d4e0e5;
                border-radius: 6px;
                padding: 6px;
                background: #ffffff;
            }
            QTextEdit#resultText, QTextEdit#detailText { background: #fbfdfd; }
            QHeaderView::section {
                background: #edf3f5;
                color: #40545d;
                border: none;
                border-bottom: 1px solid #d4e0e5;
                padding: 7px 8px;
                font-weight: 600;
            }
            QTableWidget#userTable {
                gridline-color: #e3ebef;
                alternate-background-color: #f7fafb;
            }
            QTableWidget::item { padding: 6px; }
            QTabWidget::pane {
                border: 1px solid #d8e1e5;
                border-radius: 8px;
                background: #ffffff;
                top: -1px;
            }
            QTabBar::tab {
                background: #edf3f5;
                color: #4a606a;
                padding: 9px 16px;
                border: 1px solid #d8e1e5;
                border-bottom: none;
                border-top-left-radius: 6px;
                border-top-right-radius: 6px;
                margin-right: 2px;
            }
            QTabBar::tab:selected {
                background: #ffffff;
                color: #17313b;
                font-weight: 700;
            }
            QProgressBar {
                height: 10px;
                border: 1px solid #d2e0e5;
                border-radius: 5px;
                background: #edf3f5;
            }
            QProgressBar::chunk { background: #1c6c84; border-radius: 5px; }
            QProgressBar#micMeter {
                height: 12px;
                background: #eaf1f4;
            }
            QProgressBar#micMeter::chunk {
                background: #2d8aa3;
                border-radius: 5px;
            }
            QStatusBar#statusBar {
                background: #edf2f4;
                border-top: 1px solid #d5dee3;
            }
        """)

    def load_robot_command_stats(self) -> dict[str, dict[str, Any]]:
        try:
            raw = json.loads(self.robot_stats_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except Exception:
            return {}
        if not isinstance(raw, dict):
            return {}
        stats: dict[str, dict[str, Any]] = {}
        for cmd, item in raw.items():
            if not isinstance(item, dict):
                continue
            try:
                count = int(item.get("count", 0) or 0)
                success_count = int(item.get("success_count", 0) or 0)
                success_count = max(0, min(success_count, count))
                stats[str(cmd)] = {
                    "label": str(item.get("label", cmd)),
                    "count": count,
                    "total_duration": float(item.get("total_duration", 0.0) or 0.0),
                    "last_duration": float(item.get("last_duration", 0.0) or 0.0),
                    "success_count": success_count,
                    "last_success": bool(item.get("last_success", item.get("success", False))),
                    "last_finished_at": str(item.get("last_finished_at", "")),
                    # 失败耗时单独统计，避免污染成功耗时数据
                    "fail_count": int(item.get("fail_count", 0) or 0),
                    "fail_total_duration": float(item.get("fail_total_duration", 0.0) or 0.0),
                    "fail_last_duration": float(item.get("fail_last_duration", 0.0) or 0.0),
                }
            except (TypeError, ValueError):
                continue
        return stats

    def load_rehab_history(self) -> list[dict[str, Any]]:
        try:
            raw = json.loads(self.rehab_history_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return []
        if not isinstance(raw, list):
            return []
        return [item for item in raw if isinstance(item, dict)]

    @staticmethod
    def _default_api_stats() -> dict[str, Any]:
        return {
            "count": 0,
            "total_duration": 0.0,
            "last_duration": 0.0,
            "min_duration": float("inf"),
            "max_duration": 0.0,
            "success_count": 0,
            "last_success": True,
            "last_finished_at": "",
        }

    def _load_api_stats(self, path: Path) -> dict[str, Any]:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return self._default_api_stats()
        if not isinstance(raw, dict):
            return self._default_api_stats()
        result = self._default_api_stats()
        for key in result:
            if key in raw:
                result[key] = raw[key]
        return result

    def _save_api_stats(self, task: str) -> None:
        stats_map: dict[str, dict[str, Any]] = {
            "voiceSearch": self.voice_search_stats,
            "faceDetect": self.face_detect_stats,
        }
        path_map: dict[str, Path] = {
            "voiceSearch": self.voice_search_stats_path,
            "faceDetect": self.face_detect_stats_path,
        }
        stats = stats_map.get(task)
        path = path_map.get(task)
        if stats is None or path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = path.with_suffix(path.suffix + ".tmp")
            tmp_path.write_text(
                json.dumps(stats, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp_path.replace(path)
        except Exception as exc:
            self.status.showMessage(f"耗时统计保存失败：{exc}", 5000)

    def _record_api_stats(self, result_type: str, duration: float, success: bool) -> None:
        stats_map: dict[str, tuple[str, dict[str, Any]]] = {
            "voice_search": ("voiceSearch", self.voice_search_stats),
            "face_detect": ("faceDetect", self.face_detect_stats),
        }
        entry = stats_map.get(result_type)
        if entry is None:
            return
        task, stats = entry
        stats["count"] += 1
        stats["total_duration"] += duration
        stats["last_duration"] = duration
        if duration < stats["min_duration"]:
            stats["min_duration"] = duration
        if duration > stats["max_duration"]:
            stats["max_duration"] = duration
        if success:
            stats["success_count"] += 1
        stats["last_success"] = success
        stats["last_finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self._save_api_stats(task)
        self._render_api_stats(task)

    def _render_api_stats(self, task: str) -> None:
        stats_map: dict[str, dict[str, Any]] = {
            "voiceSearch": self.voice_search_stats,
            "faceDetect": self.face_detect_stats,
        }
        stats = stats_map.get(task)
        if stats is None:
            return
        widgets = self.page_widgets.get(task, {})
        labels = widgets.get("stats_labels")
        if labels is None:
            return

        count = int(stats.get("count", 0))
        total = float(stats.get("total_duration", 0.0))
        last = float(stats.get("last_duration", 0.0))
        min_d = stats.get("min_duration", float("inf"))
        max_d = float(stats.get("max_duration", 0.0))
        success_count = int(stats.get("success_count", 0))
        avg = total / count if count else 0.0
        success_rate = success_count / count * 100 if count else 0.0
        last_time = str(stats.get("last_finished_at", ""))

        labels.get("count", QLabel()).setText(f"{count} 次")
        labels.get("last", QLabel()).setText(f"{last:.2f}s" if count else "—")
        labels.get("avg", QLabel()).setText(f"{avg:.2f}s" if count else "—")
        if count > 0 and min_d != float("inf"):
            labels.get("range", QLabel()).setText(f"{min_d:.2f}s / {max_d:.2f}s")
        else:
            labels.get("range", QLabel()).setText("—")
        labels.get("success_rate", QLabel()).setText(f"{success_rate:.0f}% ({success_count}/{count})")
        labels.get("last_time", QLabel()).setText(last_time if last_time else "—")

    def clear_robot_stats(self) -> None:
        if QMessageBox.question(
            self,
            "智能体调试平台",
            "确认清空所有机器人指令耗时统计数据？此操作不可恢复。",
        ) != QMessageBox.StandardButton.Yes:
            return
        self.robot_command_stats.clear()
        self.save_robot_command_stats()
        self.robot_stats_table.setRowCount(0)
        self.status.showMessage("机器人耗时统计已清空", 3000)

    def save_robot_command_stats(self) -> None:
        try:
            self.robot_stats_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self.robot_stats_path.with_suffix(self.robot_stats_path.suffix + ".tmp")
            tmp_path.write_text(
                json.dumps(self.robot_command_stats, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp_path.replace(self.robot_stats_path)
        except Exception as exc:
            self.status.showMessage(f"耗时统计保存失败：{exc}", 5000)

    def save_rehab_history(self) -> None:
        try:
            self.rehab_history_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self.rehab_history_path.with_suffix(
                self.rehab_history_path.suffix + ".tmp"
            )
            tmp_path.write_text(
                json.dumps(self.rehab_history, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp_path.replace(self.rehab_history_path)
        except Exception as exc:
            self.status.showMessage(f"康复记录保存失败：{exc}", 5000)

    def clear_rehab_history(self) -> None:
        if QMessageBox.question(
            self,
            "智能体调试平台",
            "确认一键删除全部康复执行记录？此操作不可恢复。",
        ) != QMessageBox.StandardButton.Yes:
            return
        self.rehab_history.clear()
        self.save_rehab_history()
        self.render_rehab_history()
        self.status.showMessage("康复执行记录已删除", 3000)

    def apply_api_base(self) -> None:
        self.config_data = self.config_data.with_api_base(self.client.api_base)
        self.client.set_api_base(self.config_data.api_base)
        self.check_health()

    def check_health(self) -> None:
        self.runner.run(
            self.client.health,
            lambda data: self.service_label.setText("服务正常" if data.get("status") == "ok" else f"服务异常：{data.get('status', '')}"),
            lambda _err: self.service_label.setText("服务不可用"),
        )

    def refresh_users(self) -> None:
        self.set_result("加载用户中，请稍候...")
        self.runner.run(self.client.list_users, self._users_loaded, lambda err: self.set_result(f"加载用户失败：{err}"))

    def refresh_and_cleanup(self) -> None:
        self.set_result("正在清理孤儿数据并刷新用户列表...")
        self.runner.run(
            lambda: {
                "sync": self.client.sync_data(),
                "users": self.client.list_users(),
            },
            self._cleanup_and_users_loaded,
            lambda err: QMessageBox.warning(self, "智能体调试平台", f"刷新失败：{err}"),
        )

    def _cleanup_and_users_loaded(self, data: Any) -> None:
        self._users_loaded(data.get("users", {}))
        sync_data = data.get("sync", {})
        voice_cleanup = sync_data.get("voice_cleanup") or []
        face_cleanup = sync_data.get("face_cleanup") or []
        lines = [
            "刷新完成，孤儿数据清理已执行。",
            f"声纹孤儿数据：{', '.join(map(str, voice_cleanup)) if voice_cleanup else '无'}",
            f"人脸孤儿数据：{', '.join(map(str, face_cleanup)) if face_cleanup else '无'}",
        ]
        QMessageBox.information(self, "智能体调试平台", "\n".join(lines))

    def _users_loaded(self, data: Any) -> None:
        self.users = data.get("users", [])
        self.render_users()
        self.render_verify_user_ids()
        self.render_robot_user_ids()
        self.set_result(f"用户列表已刷新，共 {data.get('total', len(self.users))} 位用户。")

    def render_users(self) -> None:
        keyword = self.search_input.text().strip().lower()
        rows = []
        for user in self.users:
            haystack = f"{user.get('user_id', '')} {user.get('name', '')} {user.get('role', '')} {user.get('description', '')}".lower()
            if not keyword or keyword in haystack:
                rows.append(user)
        self.user_table.setRowCount(len(rows))
        for row, user in enumerate(rows):
            self.user_table.setItem(row, 0, QTableWidgetItem(str(user.get("user_id", ""))))
            self.user_table.setItem(row, 1, QTableWidgetItem(str(user.get("name", ""))))
            self.user_table.setItem(row, 2, QTableWidgetItem(str(user.get("role", ""))))

    def render_robot_user_ids(self) -> None:
        for combo in getattr(self, "robot_user_combos", []):
            current = combo.currentText().strip()
            combo.clear()
            seen = set()
            for user in self.users:
                user_id = str(user.get("user_id", "")).strip()
                if user_id and user_id not in seen:
                    combo.addItem(user_id, user_id)
                    seen.add(user_id)
            if current:
                index = combo.findText(current)
                if index >= 0:
                    combo.setCurrentIndex(index)
                else:
                    combo.setEditText(current)

    def render_verify_user_ids(self) -> None:
        combo = getattr(self, "verify_user_id", None)
        if combo is None:
            return
        current = combo.currentText().strip()
        combo.clear()
        seen = set()
        for user in self.users:
            user_id = str(user.get("user_id", "")).strip()
            if user_id and user_id not in seen:
                combo.addItem(user_id, user_id)
                seen.add(user_id)
        if current:
            index = combo.findData(current)
            if index >= 0:
                combo.setCurrentIndex(index)
            else:
                combo.setEditText(current)

    def on_user_select(self) -> None:
        selected = self.user_table.selectedItems()
        if not selected:
            return
        user_id = self.user_table.item(selected[0].row(), 0).text()
        self.selected_user = next((u for u in self.users if str(u.get("user_id", "")) == user_id), None)
        if self.selected_user:
            self.verify_user_id.setCurrentText(user_id)
            self.user_face_view.clear_image("照片加载中...")
            self.detail_text.setText("\n".join([
                f"ID：{self.selected_user.get('user_id', '')}",
                f"姓名：{self.selected_user.get('name', '')}",
                f"角色：{self.selected_user.get('role', '')}",
                f"备注：{self.selected_user.get('description', '')}",
                f"创建时间：{self.selected_user.get('created_at', '')}",
            ]))
            self.load_user_face(user_id)

    def load_user_face(self, user_id: str) -> None:
        self.runner.run(
            lambda: self.client.get_user_face(user_id),
            lambda data, expected_id=user_id: self.show_user_face(expected_id, data),
            lambda err, expected_id=user_id: self.show_user_face_error(expected_id, err),
        )

    def show_user_face(self, expected_id: str, data: Any) -> None:
        if not self.selected_user or str(self.selected_user.get("user_id", "")) != expected_id:
            return
        face_image = data.get("face_image") if isinstance(data, dict) else None
        if not face_image:
            self.user_face_view.clear_image("暂无照片")
            return
        pixmap = pixmap_from_data_uri(str(face_image))
        if pixmap is None:
            self.user_face_view.clear_image("照片解析失败")
            return
        self.user_face_view.set_pixmap(pixmap)

    def show_user_face_error(self, expected_id: str, message: str) -> None:
        if self.selected_user and str(self.selected_user.get("user_id", "")) == expected_id:
            self.user_face_view.clear_image(f"照片加载失败：{message}")

    def use_selected_for_verify(self) -> None:
        if not self.selected_user:
            QMessageBox.information(self, "智能体调试平台", "请先选择一个用户")
            return
        self.verify_user_id.setCurrentText(str(self.selected_user.get("user_id", "")))
        self.tabs.setCurrentIndex(3)

    def delete_selected_user(self) -> None:
        if not self.selected_user:
            QMessageBox.information(self, "智能体调试平台", "请先选择一个用户")
            return
        user_id = str(self.selected_user.get("user_id", ""))
        if QMessageBox.question(self, "智能体调试平台", f"确认删除用户 {user_id}？") != QMessageBox.StandardButton.Yes:
            return
        self.run_api("删除用户", lambda: self.client.delete_user(user_id), "delete", lambda _data: self.refresh_users())

    def clear_user_database(self) -> None:
        if QMessageBox.question(
            self,
            "智能体调试平台",
            "一键清库会删除所有用户，以及对应的人脸和声纹数据，删除后不可恢复。\n\n确认继续吗？",
        ) != QMessageBox.StandardButton.Yes:
            return
        self.set_result("正在清库，请稍候...")

        def ok(data: Any) -> None:
            deleted = data.get("deleted") or []
            failed = data.get("failed") or []
            sync_result = data.get("sync") or {}
            voice_cleanup = sync_result.get("voice_cleanup") or []
            face_cleanup = sync_result.get("face_cleanup") or []
            self.selected_user = None
            self.user_table.clearSelection()
            self.user_face_view.clear_image("请选择用户查看照片")
            self.detail_text.clear()
            self.refresh_users()
            lines = [
                f"清库完成：已删除 {len(deleted)} / {data.get('total', len(deleted))} 位用户。",
                f"声纹孤儿数据清理：{', '.join(map(str, voice_cleanup)) if voice_cleanup else '无'}",
                f"人脸孤儿数据清理：{', '.join(map(str, face_cleanup)) if face_cleanup else '无'}",
            ]
            if failed:
                lines.append(f"删除失败：{'; '.join(map(str, failed))}")
            if sync_result.get("error"):
                lines.append(f"同步清理失败：{sync_result.get('error')}")
            QMessageBox.information(self, "智能体调试平台", "\n".join(lines))

        self.runner.run(
            self.client.clear_users,
            ok,
            lambda err: QMessageBox.warning(self, "智能体调试平台", f"清库失败：{err}"),
        )

    def on_tab_changed(self, index: int) -> None:
        if self.face_detect_realtime:
            self.stop_realtime_face_detect(update_text=False)
        if self.camera_active:
            self.stop_camera()
        self.active_task = TASKS[index]
        self._activate_task_widgets(self.active_task)
        self.photo_review_mode = self.photo_task == self.active_task and bool(self.photo_bytes)
        if self.active_task in CAMERA_TASKS:
            self._sync_camera_source_controls()
        if self.active_task in {"register", "faceVerify", "faceDetect"}:
            self.photo_bytes = None
            self.photo_task = ""
            self.photo_review_mode = False
            self.photo_state.setText("尚未拍照" if self.active_task == "register" else "请为当前功能重新拍照")
            self.capture_btn.setText("拍照")
            self.camera_view.clear_image("摄像头未开启")
        if self.active_task == "voiceSearch":
            self.audio_bytes = None
            self.audio_task = ""
            self.audio_review.setText("录音：请为声纹搜索重新录音")

    def _sync_camera_source_controls(self) -> bool:
        is_robot = self.source_combo.currentIndex() == 1
        self.topic_combo.setVisible(is_robot)
        self.topic_combo.setEnabled(is_robot)
        self.topic_label.setVisible(is_robot)
        self.start_camera_btn.setEnabled(True)
        self.stop_camera_btn.setEnabled(True)
        return is_robot

    def on_camera_source_changed(self) -> None:
        if self.active_task not in CAMERA_TASKS:
            return
        self._sync_camera_source_controls()
        self.stop_camera()
        self.clear_photo()

    def restart_robot_if_needed(self) -> None:
        if (
            self.active_task in CAMERA_TASKS
            and self.source_combo.currentIndex() == 1
            and self.camera_active
        ):
            self.start_robot_camera()

    def start_camera(self) -> None:
        if self.source_combo.currentIndex() == 1:
            self.start_robot_camera()
            return
        self.camera_active = True
        self.camera_task = self.active_task
        self.camera_label.setText("摄像头开启中")
        self.camera.start_local()

    def start_robot_camera(self) -> None:
        self.camera_active = True
        self.camera_task = self.active_task
        topic_key = "head" if self.topic_combo.currentIndex() == 0 else "up"
        self.current_frame = None
        self.camera_view.clear_image("摄像头开启中")
        self.camera_label.setText("机器人摄像头开启中")
        self.camera.start_robot(topic_key)

    def stop_camera(self) -> None:
        if self.face_detect_realtime:
            self.stop_realtime_face_detect(update_text=False)
        self.camera_active = False
        self.camera_task = ""
        self.camera.stop()
        self.current_frame = None
        self.camera_view.clear_image("摄像头未开启")
        self.camera_label.setText("摄像头未开启")

    def on_camera_frame(self, frame: np.ndarray) -> None:
        if self.camera_task != self.active_task:
            return
        self.current_frame = frame
        if self.active_task == "faceDetect" and self.face_detect_realtime:
            if not self.face_detect_realtime_has_result:
                self.camera_view.set_frame(frame)
            self.queue_realtime_face_detect(frame)
            return
        if self.active_task in CAMERA_TASKS:
            if self.photo_review_mode and self.photo_task == self.active_task:
                return
            self.camera_view.set_frame(frame)

    def on_camera_status(self, message: str) -> None:
        if self.camera_active:
            self.camera_label.setText(message)

    def on_camera_error(self, message: str) -> None:
        if not self.camera_active:
            return
        self.camera_active = False
        self.camera_task = ""
        if self.face_detect_realtime:
            self.stop_realtime_face_detect(update_text=False)
        self.camera_label.setText("摄像头不可用")
        self.set_result(f"摄像头错误：{message}")
        QMessageBox.warning(self, "智能体调试平台", message)

    def capture_photo(self) -> None:
        if self.active_task == "faceDetect" and self.face_detect_realtime:
            QMessageBox.information(self, "智能体调试平台", "请先关闭实时检测后再拍照")
            return
        if self.photo_review_mode and self.photo_task == self.active_task:
            self.clear_photo()
            return
        if self.current_frame is None:
            QMessageBox.information(self, "智能体调试平台", "请先开启摄像头")
            return
        ok, encoded = cv2.imencode(".jpg", self.current_frame, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
        if not ok:
            QMessageBox.warning(self, "智能体调试平台", "照片编码失败")
            return
        self.photo_bytes = encoded.tobytes()
        self.photo_task = self.active_task
        suffix = "（机器人）" if self.source_combo.currentIndex() == 1 else ""
        self.photo_state.setText(f"{task_name(self.active_task)}照片已就绪{suffix}")
        self.photo_review_mode = True
        self.camera_view.set_frame(self.current_frame)
        self.capture_btn.setText("恢复")
        if self.source_combo.currentIndex() == 1:
            self._save_robot_photo(self.current_frame)

    def _save_robot_photo(self, frame: np.ndarray) -> None:
        save_dir = self.runtime_dir / "robot_photos"
        save_dir.mkdir(parents=True, exist_ok=True)
        label = "head" if self.topic_combo.currentIndex() == 0 else "up"
        path = save_dir / f"robot_{label}_{time.strftime('%Y%m%d_%H%M%S')}.jpg"
        cv2.imwrite(str(path), frame)
        self.set_result(f"机器人拍照成功，已保存至 {path}")

    def clear_photo(self) -> None:
        self.photo_bytes = None
        self.photo_task = ""
        self.photo_review_mode = False
        self.photo_state.setText("尚未拍照")
        self.capture_btn.setText("拍照")
        if self.current_frame is not None and self.active_task in {"register", "faceVerify", "faceDetect"}:
            self.camera_view.set_frame(self.current_frame)
        elif self.active_task in {"register", "faceVerify", "faceDetect"}:
            self.camera_view.clear_image("摄像头未开启")

    def start_recording(self, seconds: int) -> None:
        if self.recorder.recording:
            QMessageBox.information(self, "智能体调试平台", "当前正在录音，请先停止或等待录音完成")
            return
        self.audio_bytes = None
        self.audio_task = ""
        self.audio_duration = 0.0
        self.audio_target_seconds = seconds
        self.audio_recording_task = self.active_task
        self.audio_meter.setValue(0)
        self.audio_review.setText("录音：正在录制")
        self.audio_time.setText("00:00")
        self.recorder.start(seconds)
        self._sync_audio_controls()

    def stop_recording(self) -> None:
        if not self.recorder.recording:
            return
        self.audio_review.setText("录音：正在停止")
        self.recorder.stop()

    def toggle_recording_pause(self) -> None:
        if not self.recorder.recording:
            self._sync_audio_controls()
            return
        paused = self.recorder.toggle_pause()
        self.audio_review.setText("录音：已暂停" if paused else "录音：正在录制")
        self._sync_audio_controls()

    def _sync_audio_controls(self) -> None:
        pause_btn = getattr(self, "audio_pause_btn", None)
        if pause_btn is None:
            return
        pause_btn.setEnabled(self.recorder.recording and self.audio_recording_task == "register")
        pause_btn.setText("继续" if self.recorder.paused else "暂停")

    def on_record_elapsed(self, seconds: float) -> None:
        self.audio_time.setText(format_seconds(seconds))
        if self.audio_target_seconds > 0:
            percent = min(100, int(seconds / self.audio_target_seconds * 100))
            self.audio_meter.setValue(percent)

    def on_audio_done(self, wav_bytes: bytes, duration: float) -> None:
        self.audio_bytes = wav_bytes
        self.audio_duration = duration
        self.audio_task = self.audio_recording_task or self.active_task
        self.audio_recording_task = ""
        self.audio_time.setText(format_seconds(duration))
        if self.audio_target_seconds > 0:
            self.audio_meter.setValue(min(100, int(duration / self.audio_target_seconds * 100)))
        self.audio_review.setText(f"录音：{task_name(self.audio_task)}，{duration:.1f}s，可回放检查")
        self.mic_label.setText(f"录音已就绪 {duration:.1f}s")
        self._sync_audio_controls()

    def on_audio_error(self, message: str) -> None:
        self.audio_target_seconds = 0
        self.audio_recording_task = ""
        self.audio_meter.setValue(0)
        self.mic_label.setText("麦克风不可用")
        self._sync_audio_controls()
        QMessageBox.warning(self, "智能体调试平台", message)

    def play_audio(self) -> None:
        if not self.audio_bytes:
            QMessageBox.information(self, "智能体调试平台", "还没有可回放的录音")
            return
        self.recorder.play(self.audio_bytes)

    def on_playback_error(self, message: str) -> None:
        QMessageBox.warning(self, "智能体调试平台", message)

    def submit_register(self) -> None:
        data = {
            "user_id": self.register_id.text().strip(),
            "name": self.register_name.text().strip(),
            "role": self.register_role.currentText().strip(),
            "description": self.register_description.text().strip(),
        }
        if not data["user_id"] or not data["name"]:
            QMessageBox.information(self, "智能体调试平台", "用户 ID 和姓名不能为空")
            return
        if not data["user_id"].isascii() or not data["user_id"].isalnum():
            QMessageBox.information(self, "智能体调试平台", "用户 ID 只能包含英文和数字")
            return
        self.set_result("注册中，请稍候...")

        def ok(result: Any) -> None:
            self.last_result_data = result
            self.refresh_users()
            QMessageBox.information(
                self,
                "智能体调试平台",
                f"注册成功\n用户 ID：{result.get('user_id', data['user_id'])}\n姓名：{result.get('name', data['name'])}",
            )

        self.runner.run(
            lambda: self.client.register_user(
                data,
                self.require_photo(),
                self.require_audio(min_duration=REGISTER_MIN_AUDIO_SECONDS),
            ),
            ok,
            lambda err: QMessageBox.warning(self, "智能体调试平台", f"注册失败：{err}"),
        )

    def submit_voice_search(self) -> None:
        self.run_api("声纹搜索", lambda: self.client.voice_search(self.voice_top_k.value(), self.require_audio("voiceSearch")), "voice_search")

    def submit_face_verify(self) -> None:
        user_id = self.verify_user_id.currentText().strip()
        if not user_id:
            QMessageBox.information(self, "智能体调试平台", "请输入或选择用户 ID")
            return
        self.run_api("人脸验证", lambda: self.client.face_verify(user_id, self.require_photo("faceVerify")), "face_verify")

    def submit_face_detect(self) -> None:
        if self.face_detect_realtime:
            return
        photo = self.require_photo("faceDetect")

        def done(data: Any) -> None:
            annotated = annotate_face_detect(photo, data, self.config_data.face_detect_match_threshold)
            if annotated is not None:
                self.camera_view.set_frame(annotated)

        self.run_api(
            "人脸检测",
            lambda: self.client.face_detect(
                photo,
                self.detect_max_faces.value(),
                self.config_data.face_detect_match_threshold,
            ),
            "face_detect",
            done,
        )

    def toggle_realtime_face_detect(self) -> None:
        if self.face_detect_realtime:
            self.stop_realtime_face_detect()
            return
        self.start_realtime_face_detect()

    def start_realtime_face_detect(self) -> None:
        if self.active_task != "faceDetect":
            return
        if not self.camera_active or self.camera_task != "faceDetect":
            QMessageBox.information(self, "智能体调试平台", "请先开启摄像头")
            return
        self.face_detect_realtime = True
        self.face_detect_realtime_generation += 1
        self.face_detect_inflight = False
        self.face_detect_pending_frame = None
        self.face_detect_realtime_has_result = False
        self.photo_review_mode = False
        self.photo_bytes = None
        self.photo_task = ""
        self.photo_state.setText("实时检测开启")
        self.capture_btn.setText("拍照")
        self.set_result("实时检测开启")
        self._sync_face_detect_realtime_controls()
        if self.current_frame is not None:
            self.queue_realtime_face_detect(self.current_frame)

    def stop_realtime_face_detect(self, update_text: bool = True) -> None:
        self.face_detect_realtime = False
        self.face_detect_realtime_generation += 1
        self.face_detect_inflight = False
        self.face_detect_pending_frame = None
        self.face_detect_realtime_has_result = False
        self._sync_face_detect_realtime_controls()
        if update_text and self.active_task == "faceDetect":
            self.photo_state.setText("实时检测已关闭")
            self.set_result("实时检测已关闭")
            if self.current_frame is not None and not self.photo_review_mode:
                self.camera_view.set_frame(self.current_frame)

    def _sync_face_detect_realtime_controls(self) -> None:
        widgets = self.page_widgets.get("faceDetect", {})
        realtime_btn = widgets.get("realtime_face_detect_btn")
        once_btn = widgets.get("face_detect_once_btn")
        if realtime_btn is not None:
            realtime_btn.setText("关闭实时检测" if self.face_detect_realtime else "开启实时检测")
        if once_btn is not None:
            once_btn.setEnabled(not self.face_detect_realtime)

    def queue_realtime_face_detect(self, frame: np.ndarray) -> None:
        if not self.face_detect_realtime or self.active_task != "faceDetect":
            return
        self.face_detect_pending_frame = frame.copy()
        if not self.face_detect_inflight:
            self.start_next_realtime_face_detect()

    def start_next_realtime_face_detect(self) -> None:
        if not self.face_detect_realtime or self.active_task != "faceDetect":
            return
        frame = self.face_detect_pending_frame
        self.face_detect_pending_frame = None
        if frame is None:
            return
        ok, encoded = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 88])
        if not ok:
            self.on_realtime_face_detect_error("画面编码失败")
            return
        jpg = encoded.tobytes()
        max_faces = self.detect_max_faces.value()
        generation = self.face_detect_realtime_generation
        self.face_detect_realtime_start = time.perf_counter()
        self.face_detect_inflight = True
        self.runner.run(
            lambda: self.client.face_detect(jpg, max_faces, self.config_data.face_detect_match_threshold),
            lambda data, image=jpg, gen=generation: self.on_realtime_face_detect_done(image, data, gen),
            lambda message, gen=generation: self.on_realtime_face_detect_error(message, gen),
        )

    def on_realtime_face_detect_done(self, jpg: bytes, data: Any, generation: int) -> None:
        if generation != self.face_detect_realtime_generation:
            return
        duration = time.perf_counter() - self.face_detect_realtime_start
        self._record_api_stats("face_detect", duration, True)
        self.face_detect_inflight = False
        if not self.face_detect_realtime or self.active_task != "faceDetect":
            return
        self.last_result_data = data
        self.result_text.setText(format_result(data, "face_detect"))
        annotated = annotate_face_detect(jpg, data, self.config_data.face_detect_match_threshold)
        if annotated is not None:
            self.face_detect_realtime_has_result = True
            self.camera_view.set_frame(annotated)
        self.photo_state.setText("实时检测开启")
        self.start_next_realtime_face_detect()

    def on_realtime_face_detect_error(self, message: str, generation: int | None = None) -> None:
        if generation is not None and generation != self.face_detect_realtime_generation:
            return
        if self.face_detect_realtime_start > 0:
            duration = time.perf_counter() - self.face_detect_realtime_start
            self._record_api_stats("face_detect", duration, False)
        self.face_detect_inflight = False
        if not self.face_detect_realtime or self.active_task != "faceDetect":
            return
        self.stop_realtime_face_detect(update_text=False)
        self.photo_state.setText("实时检测已停止")
        self.set_result(f"实时检测失败：{message}")

    def run_api(self, label: str, action, result_type: str, after_success=None) -> None:
        self.set_result(f"{label}中，请稍候...")
        started = time.perf_counter()

        def ok(data: Any) -> None:
            self.last_result_data = data
            self.result_text.setText(format_result(data, result_type))
            self._record_api_stats(result_type, time.perf_counter() - started, True)
            if after_success:
                after_success(data)

        def fail(err: str) -> None:
            self._record_api_stats(result_type, time.perf_counter() - started, False)
            self.set_result(f"{label}失败：{err}")

        self.runner.run(action, ok, fail)

    def run_robot_command(self, label: str, template: str, needs_user: bool = False) -> None:
        cmd = self._robot_command_text(template, needs_user)
        if not cmd:
            return
        self._start_robot_command(label, cmd)

    def run_robot_input_command(self, label: str, template: str, field: QLineEdit, empty_message: str) -> None:
        value = field.text().strip()
        if not value:
            QMessageBox.information(self, "智能体调试平台", empty_message)
            return
        self._start_robot_command(label, template.format(value=shlex.quote(value)))

    def run_robot_select_command(self, combo: QComboBox, template: str, label_template: str) -> None:
        value, label = combo.currentData()
        self._start_robot_command(label_template.format(label=label), template.format(value=value))

    def run_robot_combo_command(self, label: str, template: str, combo: QComboBox) -> None:
        value = str(combo.currentData() or "").strip()
        if not value:
            QMessageBox.information(self, "智能体调试平台", "请选择命令参数")
            return
        self._start_robot_command(label, template.format(value=value))

    def run_rehab_trace(self) -> None:
        trace_type = "up_and_down"
        cmd = " ".join([
            f"exec-trace --type={shlex.quote(trace_type)}",
            f"--speed={self.rehab_speed.currentData()}",
            f"--scale={self.rehab_scale.currentData()}",
            f"--repeat={self.rehab_repeat.value()}",
        ])
        self._start_robot_command("执行轨迹", cmd)

    def refresh_rehab_traces(self) -> None:
        if self.robot_contexts.get("rehab", {}).get("busy"):
            return
        self._start_robot_command(
            "查询可用轨迹",
            "list-trace",
            context_key="rehab",
            after_done=self._update_rehab_trace_options,
            timeout=10,
        )

    def _update_rehab_trace_options(self, result: dict[str, Any]) -> None:
        if not result.get("success"):
            return
        traces = self._extract_trace_names(str(result.get("output", "")))
        current = str(self.rehab_trace_type.currentData() or "")
        self.rehab_trace_type.clear()
        for trace in traces:
            self.rehab_trace_type.addItem(trace, trace)
        selected = False
        if current:
            index = self.rehab_trace_type.findData(current)
            if index >= 0:
                self.rehab_trace_type.setCurrentIndex(index)
                selected = True
        if traces and not selected:
            self.rehab_trace_type.setCurrentIndex(0)
        if not traces:
            self.rehab_trace_type.setPlaceholderText("服务端未返回可用轨迹")
            self.robot_contexts["rehab"]["status"].setText("查询成功，但未解析到可用轨迹")

    def _extract_trace_names(self, output: str) -> list[str]:
        parsed = self._parse_robot_output_json(output)
        candidates: list[Any] = []
        if isinstance(parsed, list):
            candidates.extend(parsed)
        elif isinstance(parsed, dict):
            for key in ("traces", "trace_types", "types", "data", "result", "output"):
                value = parsed.get(key)
                if isinstance(value, list):
                    candidates.extend(value)
                elif isinstance(value, str):
                    candidates.extend(re.split(r"[,\\s]+", value))
        if not candidates:
            for line in output.splitlines():
                clean = line.strip().lstrip("-*•").strip()
                if ":" in clean:
                    prefix, remainder = clean.split(":", 1)
                    if prefix.strip().lower() in {
                        "traces", "trace", "types", "available traces", "可用轨迹",
                    }:
                        clean = remainder.strip()
                candidates.extend(re.split(r"[,\\s]+", clean))

        traces: list[str] = []
        for item in candidates:
            if isinstance(item, dict):
                item = item.get("name") or item.get("type") or item.get("id")
            value = str(item or "").strip().strip("'\"[]()")
            if not value or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
                continue
            if value.lower() in {
                "trace", "traces", "type", "types", "available", "success", "true",
            }:
                continue
            if value not in traces:
                traces.append(value)
        return traces

    def _start_robot_command(
        self,
        label: str,
        cmd: str,
        *,
        context_key: str | None = None,
        after_done=None,
        timeout: int | float | None = None,
    ) -> None:
        key = context_key or getattr(self, "active_robot_page", "home")
        context = self.robot_contexts.get(key, self.robot_contexts["home"])
        if context.get("busy"):
            return
        user_combo = context.get("user_combo")
        user_id = user_combo.currentText().strip() if user_combo is not None else ""
        if key in {"feeding", "water"} and not user_id:
            QMessageBox.information(self, "智能体调试平台", "请先输入或选择用户 ID")
            return
        context["status"].setText(f"执行中：{label}")
        context["output"].setText(f"$ {cmd}\n\n执行中，请稍候...")
        self._set_robot_context_busy(key, True)

        def done(result: Any) -> None:
            if isinstance(result, dict):
                result["context_key"] = key
                result["user_id"] = user_id
            self.on_robot_command_done(result)
            if after_done and isinstance(result, dict):
                after_done(result)

        def failed(err: str) -> None:
            done({
                "label": label,
                "cmd": cmd,
                "duration": 0.0,
                "success": False,
                "detail": "请求失败",
                "output": err,
                "finished_at": time.strftime("%H:%M:%S"),
            })

        self.runner.run(
            lambda: self._execute_robot_command(label, cmd, timeout),
            done,
            failed,
        )

    def _set_robot_context_busy(self, key: str, busy: bool) -> None:
        context = self.robot_contexts.get(key)
        if context is not None:
            context["busy"] = busy
        page = getattr(self, "robot_pages", {}).get(key)
        if page is None:
            return
        for button in page.findChildren(QPushButton):
            if button.property("robotNavigation"):
                continue
            button.setEnabled(not busy)

    def _robot_command_text(self, template: str, needs_user: bool) -> str | None:
        if not needs_user:
            return template
        context = self.robot_contexts.get(
            getattr(self, "active_robot_page", "home"),
            self.robot_contexts["home"],
        )
        combo = context.get("user_combo")
        user_id = combo.currentText().strip() if combo is not None else ""
        if not user_id:
            QMessageBox.information(self, "智能体调试平台", "请先输入用户 ID")
            return None
        return template.format(user_id=shlex.quote(user_id))

    def _execute_robot_command(
        self,
        label: str,
        cmd: str,
        timeout: int | float | None = None,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            data = self.robot_client.run(cmd, timeout=timeout)
            output = str(data.get("output", "")).strip() or "命令执行完成"
            success, detail = self._robot_command_success(data, output)
        except Exception as exc:
            success = False
            output = str(exc)
            detail = "请求失败"
        return {
            "label": label,
            "cmd": cmd,
            "duration": time.perf_counter() - started,
            "success": success,
            "detail": detail,
            "output": output,
            "finished_at": time.strftime("%H:%M:%S"),
        }

    def _robot_command_success(self, data: dict[str, Any], output: str) -> tuple[bool, str]:
        outer_success = self._coerce_success_bool(data.get("success", False))
        if not outer_success:
            return False, "代理返回失败"

        parsed = self._parse_robot_output_json(output)
        json_success = self._json_success_value(parsed)
        if json_success is not None:
            return json_success, "机器人返回成功" if json_success else "机器人返回失败"

        text = output.lower()
        failure_words = ("失败", "错误", "异常", "超时", "failed", "failure", "error", "timeout")
        if any(word in text for word in failure_words):
            return False, "返回内容包含失败信息"
        return True, "代理返回成功"

    def _parse_robot_output_json(self, output: str) -> Any:
        text = output.strip()
        if not text:
            return None
        candidates = [text]
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        candidates.extend(line for line in lines if line.startswith(("{", "[")))
        object_start = text.find("{")
        object_end = text.rfind("}")
        if 0 <= object_start < object_end:
            candidates.append(text[object_start:object_end + 1])
        array_start = text.find("[")
        array_end = text.rfind("]")
        if 0 <= array_start < array_end:
            candidates.append(text[array_start:array_end + 1])
        for candidate in candidates:
            try:
                return json.loads(candidate)
            except (TypeError, ValueError):
                continue
        return None

    def _json_success_value(self, value: Any) -> bool | None:
        if isinstance(value, dict):
            if "success" in value:
                return self._coerce_success_bool(value.get("success"))
            status = value.get("status") or value.get("state") or value.get("result")
            if isinstance(status, bool):
                return status
            if isinstance(status, str):
                normalized = status.strip().lower()
                if normalized in {"success", "succeeded", "ok", "done", "completed", "complete", "true", "成功"}:
                    return True
                if normalized in {"fail", "failed", "failure", "error", "timeout", "false", "失败"}:
                    return False
            code = None
            for key in ("code", "returncode", "return_code"):
                if key in value:
                    code = value.get(key)
                    break
            if code is not None:
                try:
                    return int(code) in {0, 200}
                except (TypeError, ValueError):
                    pass
            for key in ("data", "payload", "response"):
                nested = self._json_success_value(value.get(key))
                if nested is not None:
                    return nested
        if isinstance(value, list) and value:
            nested = [self._json_success_value(item) for item in value]
            known = [item for item in nested if item is not None]
            if known:
                return all(known)
        return None

    def _coerce_success_bool(self, value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"true", "success", "succeeded", "ok", "done", "completed", "complete", "1", "成功"}:
                return True
            if normalized in {"false", "fail", "failed", "failure", "error", "timeout", "0", "失败"}:
                return False
        return bool(value)

    def on_robot_command_done(self, result: Any) -> None:
        if not isinstance(result, dict):
            result = {
                "label": "机器人命令",
                "cmd": "",
                "duration": 0.0,
                "success": False,
                "detail": "请求失败",
                "output": str(result),
                "finished_at": time.strftime("%H:%M:%S"),
            }
        context_key = str(result.get("context_key", "home"))
        context = self.robot_contexts.get(context_key, self.robot_contexts["home"])
        self._set_robot_context_busy(context_key, False)
        cmd = str(result.get("cmd", ""))
        duration = float(result.get("duration", 0.0))
        success = bool(result.get("success", False))
        output = str(result.get("output", ""))
        detail = str(result.get("detail", ""))
        label = str(result.get("label", cmd or "机器人命令"))

        stats = self.robot_command_stats.setdefault(cmd, {
            "label": label,
            "count": 0,
            "total_duration": 0.0,
            "last_duration": 0.0,
            "success_count": 0,
            "last_success": True,
            "fail_count": 0,
            "fail_total_duration": 0.0,
            "fail_last_duration": 0.0,
        })
        stats["label"] = label
        stats["count"] += 1
        if success:
            stats["success_count"] += 1
            stats["total_duration"] += duration
            stats["last_duration"] = duration
        else:
            stats["fail_count"] = stats.get("fail_count", 0) + 1
            stats["fail_total_duration"] = stats.get("fail_total_duration", 0.0) + duration
            stats["fail_last_duration"] = duration
        stats["last_success"] = success
        stats["last_finished_at"] = str(result.get("finished_at", ""))
        self.save_robot_command_stats()
        self.render_robot_stats()

        # 记录每次指令执行日志
        self._robot_logger.info(
            "cmd=%s | label=%s | duration=%.3fs | success=%s | detail=%s",
            cmd, label, duration, success, detail,
        )

        state = "成功" if success else "失败"
        context["status"].setText(f"{state}：{label}，耗时 {duration:.2f}s")
        context["output"].setText(
            "\n".join([
                f"$ {cmd}",
                f"状态：{state}",
                f"判断：{detail}",
                f"完成时间：{result.get('finished_at', '')}",
                f"耗时：{duration:.2f}s",
                "",
                output,
            ])
        )
        if context_key == "rehab":
            self._append_rehab_history(result)

    def _append_rehab_history(self, result: dict[str, Any]) -> None:
        if result.get("cmd") == "list-trace":
            return
        cmd = str(result.get("cmd", ""))
        parts = shlex.split(cmd) if cmd else []
        parameters = " ".join(parts[1:]) if len(parts) > 1 else "—"
        self.rehab_history.append({
            "label": str(result.get("label", "")),
            "cmd": cmd,
            "parameters": parameters,
            "executed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "duration": float(result.get("duration", 0.0)),
            "success": bool(result.get("success")),
        })
        self.save_rehab_history()
        self.render_rehab_history()

    def render_rehab_history(self) -> None:
        table = getattr(self, "rehab_history_table", None)
        if table is None:
            return
        table.setRowCount(len(self.rehab_history))
        for row, record in enumerate(self.rehab_history):
            values = [
                str(record.get("label", "")),
                str(record.get("parameters", "—")) or "—",
                str(record.get("executed_at", "")),
                f"{float(record.get('duration', 0.0)):.2f}s",
                "成功" if record.get("success") else "失败",
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                table.setItem(row, column, item)
            table.setRowHeight(row, 30)
        if self.rehab_history:
            table.scrollToBottom()

    def render_robot_stats(self) -> None:
        rows = list(self.robot_command_stats.items())
        self.robot_stats_table.setRowCount(len(rows))
        for row, (cmd, stats) in enumerate(rows):
            count = int(stats.get("count", 0))
            total = float(stats.get("total_duration", 0.0))
            last = float(stats.get("last_duration", 0.0))
            success_count = int(stats.get("success_count", 0))
            fail_count = int(stats.get("fail_count", 0))
            # 只用成功次数的耗时算平均，避免失败耗时污染
            avg = total / success_count if success_count else 0.0
            success_rate = success_count / count * 100 if count else 0.0
            state = "成功" if stats.get("last_success", stats.get("success", False)) else "失败"
            count_text = str(count)
            if fail_count:
                count_text = f"{count} (失败{fail_count})"
            values = [
                f"{stats.get('label', '')} ({cmd})",
                count_text,
                f"{last:.2f}s",
                f"{avg:.2f}s",
                state,
                f"{success_rate:.0f}%",
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                self.robot_stats_table.setItem(row, column, item)
            self.robot_stats_table.setRowHeight(row, 30)
        if rows:
            self.robot_stats_table.scrollToBottom()

    def require_photo(self, task: str | None = None) -> bytes:
        if not self.photo_bytes:
            raise RuntimeError("请先拍照")
        if task and self.photo_task != task:
            raise RuntimeError("请为当前功能重新拍照")
        return self.photo_bytes

    def require_audio(self, task: str | None = None, min_duration: float = 0.0) -> bytes:
        if not self.audio_bytes:
            raise RuntimeError("请先录音")
        if task and self.audio_task != task:
            raise RuntimeError("请为当前功能重新录音")
        if self.audio_duration < min_duration:
            raise RuntimeError(f"录音时间太短，当前 {self.audio_duration:.1f}s，至少需要 {min_duration:.0f}s")
        return self.audio_bytes

    def set_result(self, text: str) -> None:
        self.last_result_data = None
        if self.active_task == "register":
            self.status.showMessage(text, 5000)
        elif isinstance(getattr(self, "result_text", None), QTextEdit):
            self.result_text.setText(text)

    def show_json_result(self) -> None:
        if self.last_result_data is None:
            QMessageBox.information(self, "智能体调试平台", "当前没有可查看的 JSON 结果")
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("原始 JSON")
        dialog.resize(780, 560)
        layout = QVBoxLayout(dialog)
        text = QTextEdit()
        text.setReadOnly(True)
        text.setText(pretty_json(self.last_result_data))
        layout.addWidget(text)
        dialog.exec()

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self.recorder.shutdown()
        self.camera.stop()
        super().closeEvent(event)


def format_seconds(seconds: float) -> str:
    total = int(seconds)
    return f"{total // 60:02d}:{total % 60:02d}"


def task_name(task: str) -> str:
    return {
        "register": "注册",
        "voiceSearch": "声纹搜索",
        "faceVerify": "人脸验证",
        "faceDetect": "人脸检测",
        "system": "系统管理",
    }.get(task, "当前功能")


def pixmap_from_data_uri(value: str) -> QPixmap | None:
    payload = value.split(",", 1)[1] if "," in value else value
    try:
        image_bytes = base64.b64decode(payload, validate=True)
    except Exception:
        return None
    pixmap = QPixmap()
    if not pixmap.loadFromData(image_bytes):
        return None
    return pixmap
