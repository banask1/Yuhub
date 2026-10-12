"""首页：水墨鱼池横幅（yuhub 大字 + 版本号）+ 本机配置 + 实时监控。

2026-10-12 改版（用户要求三区块）：
  * 上半部分：与开机动画同款的水墨鱼池，几条写实墨鱼巡游，
    动画中央写 "yuhub" 大字与版本号小字；
  * 左下部分：本机配置（硬件信息面板）；
  * 右下部分：实时监控（CPU / 内存 / 显卡）。

注意：本页**不含功能快捷跳转卡片**（按用户要求移除），
功能入口统一走左侧导航栏。
"""

import json

from PySide6.QtCore import Qt, QEvent, QSettings, QTimer, Signal
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from .. import ink, theme, VERSION_LABEL
from ..widgets import HardwarePanel
from ..monitor_widgets import LiveMonitorPanel
from .base_page import BasePage
import hardware
import live_monitor


class HomePage(BasePage):
    TINT_KEY = "tile_1"
    # 主窗口会 connect 这个信号（页面内暂无跳转卡片，保留接口兼容）
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
        # ---- 上半：水墨鱼池 + 中央大字 ----
        pond_card = QFrame()
        pond_card.setObjectName("Card")
        # 鱼池保底高度：下半（本机配置+实时监控）在硬盘数据加载完后最小
        # 高度有 ~560px，不设保底的话 stretch 会把鱼池挤成一条缝。
        # 保底之上仍参与 stretch（大屏上鱼池能占到半页）。
        pond_card.setMinimumHeight(230)
        pv = QVBoxLayout(pond_card)
        pv.setContentsMargins(10, 10, 10, 10)
        pv.setSpacing(0)

        # 鱼池与中央文字叠在同一格：QGridLayout 同格控件按添加顺序绘制，
        # 后加的文字容器盖在鱼池之上（容器本身透明）。
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(0)
        self._pond = ink.InkPond(fish_count=5)
        grid.addWidget(self._pond, 0, 0)

        mid = QWidget()
        mid.setAttribute(Qt.WA_TranslucentBackground, True)
        ml = QVBoxLayout(mid)
        ml.setContentsMargins(0, 0, 0, 0)
        ml.setSpacing(6)
        ml.setAlignment(Qt.AlignCenter)
        self._pond_title = QLabel("yuhub")
        self._pond_ver = QLabel(VERSION_LABEL)
        ml.addWidget(self._pond_title)
        ml.addWidget(self._pond_ver)
        grid.addWidget(mid, 0, 0, Qt.AlignCenter)

        pv.addLayout(grid)
        self.add(pond_card, 1)

        # ---- 下半：左本机配置 / 右实时监控 ----
        bottom = QHBoxLayout()
        bottom.setSpacing(12)

        # 本机配置面板（on_profile 用于把采集结果写入缓存，供下次秒开）
        self.hw_panel = HardwarePanel(
            scanner_factory=hardware.HardwareScanner,
            on_profile=self._save_cached_profile,
        )
        bottom.addWidget(self.hw_panel, 1)

        # 实时监控面板
        self.monitor_panel = LiveMonitorPanel(
            monitor_factory=lambda: live_monitor.LiveMonitor(interval=1.0)
        )
        bottom.addWidget(self.monitor_panel, 1)

        self.add_layout(bottom, 1)

        theme.bus.changed.connect(self._apply_pond_text)
        self._apply_pond_text()

    def _apply_pond_text(self):
        """鱼池中央的文字配色随主题走（浅色黑墨、深色白墨）。"""
        p = theme.current()
        self._pond_title.setStyleSheet(
            "font-size: 54px; font-weight: 800; background: transparent;"
            " color: %s; letter-spacing: 2px;" % p["text"]
        )
        self._pond_ver.setStyleSheet(
            "font-size: 13px; background: transparent; color: %s;"
            " letter-spacing: 1px;" % p["text_dim"]
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
