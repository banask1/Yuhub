"""首页：极简色块横幅 + 本机配置面板 + 实时监控面板。

注意：本页**不含功能快捷跳转卡片**（按用户要求移除），
功能入口统一走左侧导航栏。
"""

import json

from PySide6.QtCore import Qt, Signal, QEvent, QSettings, QTimer
from PySide6.QtWidgets import QVBoxLayout, QHBoxLayout, QLabel, QFrame

from .. import theme, VERSION_LABEL
from ..widgets import HardwarePanel
from ..monitor_widgets import LiveMonitorPanel
from .base_page import BasePage
import hardware
import live_monitor


class HomePage(BasePage):
    TINT_KEY = "tile_1"
    navigate = Signal(str)

    def __init__(self, notify=None, parent=None):
        super().__init__(
            "欢迎使用 Yuhub",
            "一个集游戏与系统工具于一体的全能工具箱（当前为交互原型，功能逐步上线）。",
            icon="🚀",
            notify=notify,
            parent=parent,
        )
        self._settings = QSettings("Yuhub", "Yuhub")
        self._build_content()
        self._hw_started = False

    # ------------------------------------------------------------ 内容构建
    def _build_content(self):
        # 顶部横幅：实心主色块 + 左侧深色竖条
        banner = QFrame()
        banner.setObjectName("Banner")
        banner.setFixedHeight(104)
        self._banner = banner
        bh = QHBoxLayout(banner)
        bh.setContentsMargins(0, 0, 24, 0)
        bh.setSpacing(0)

        self._banner_bar = QFrame()
        self._banner_bar.setFixedWidth(6)
        bh.addWidget(self._banner_bar)

        left = QVBoxLayout()
        left.setContentsMargins(22, 0, 0, 0)
        left.setSpacing(4)
        t = QLabel("Yuhub")
        t.setStyleSheet(
            "font-size: 26px; font-weight: 800; background: transparent; color: #ffffff;"
        )
        s = QLabel(f"全能游戏与系统工具箱 · {VERSION_LABEL} 原型")
        s.setStyleSheet(
            "font-size: 13px; background: transparent; color: rgba(255,255,255,0.88);"
        )
        left.addWidget(t)
        left.addWidget(s)
        bh.addLayout(left, 1)

        rocket = QLabel("🛰️")
        rocket.setStyleSheet("font-size: 40px; background: transparent;")
        bh.addWidget(rocket, 0, Qt.AlignVCenter)
        self.add(banner)

        # 本机配置面板（on_profile 用于把采集结果写入缓存，供下次秒开）
        self.hw_panel = HardwarePanel(
            scanner_factory=hardware.HardwareScanner,
            on_profile=self._save_cached_profile,
        )
        self.add(self.hw_panel)

        # 实时监控面板
        self.monitor_panel = LiveMonitorPanel(
            monitor_factory=lambda: live_monitor.LiveMonitor(interval=1.0)
        )
        self.add(self.monitor_panel)

        self.add_stretch()

        self._apply_banner()

    def _apply_banner(self):
        p = theme.current()
        self._banner.setStyleSheet(
            "QFrame#Banner { background: %s; border: 1px solid %s; border-radius: 5px; }"
            % (p["banner_start"], p["banner_start"])
        )
        self._banner_bar.setStyleSheet(
            "background: %s; border-top-left-radius: 5px; border-bottom-left-radius: 5px;"
            % p["banner_end"]
        )

    # -------------------------------------------------------- 硬件信息加载
    def start_hardware_scan(self):
        """由主窗口在界面显示后调用：先显示缓存，再后台刷新。"""
        if self._hw_started:
            return
        self._hw_started = True

        cached = self._load_cached_profile()
        if cached:
            self.hw_panel.load_profile(cached)
        self.hw_panel.refresh()

        # 等配置面板采集完成后（约 3 秒）再启动实时监控，
        # 避免两个后台任务同时跑 PowerShell 抢资源。
        QTimer.singleShot(3200, self.monitor_panel.start)

    def _load_cached_profile(self):
        """读取上次采集结果，保证二次启动立即有内容可看。"""
        raw = self._settings.value("hw_profile_json", "")
        if not raw:
            return None
        try:
            profile = json.loads(raw)
        except (ValueError, TypeError):
            return None
        # 盘符容量是即时数据（ctypes 毫秒级），缓存里的会过期，
        # 也可能来自还没有该字段的旧版本 → 每次都取一遍实时值。
        try:
            drives, total, free = hardware.formatted_drives()
            if drives:
                profile["drives"] = drives
                profile["drives_total"] = total
                profile["drives_free"] = free
        except Exception:
            pass
        return profile

    def _save_cached_profile(self, profile):
        try:
            self._settings.setValue(
                "hw_profile_json", json.dumps(profile, ensure_ascii=False)
            )
        except (TypeError, ValueError):
            pass

    # ------------------------------------------------------------ 生命周期
    def shutdown(self):
        """窗口关闭时停止采样线程，避免进程残留。"""
        try:
            self.monitor_panel.shutdown()
        except Exception:
            pass

    def eventFilter(self, obj, event):
        # 本页不再有响应式卡片网格，保留事件过滤器以便后续扩展
        return super().eventFilter(obj, event)
