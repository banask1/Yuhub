"""禁用「选择类控件」上的滚轮改值（应用级，一次安装全项目生效）。

问题：Qt 的默认行为是——鼠标悬停在 QComboBox / QSpinBox / QSlider 上
滚一下，值就被改掉了。设置页、联机页都是长页面，用户其实想滚动页面，
结果选中项被悄悄改掉（有的控件还在视野外，改了也看不见）。

做法：应用级事件过滤器。命中滚轮事件时：
  1. 把事件**转交给最近的滚动区域视口**（没有就退到第一个非守卫的
     父控件）——页面照常滚动，只是不再改值；
  2. 返回 True 吞掉原事件，控件自己的 wheelEvent 不再被调用。

注意：不能只 return True 了事——那会让鼠标悬在下拉框上时「页面滚不动」，
体验比原来更糟。所以要转交，让滚动继续。

守卫链自递归防护：向上找目标时跳过同为守卫类型的祖先（理论上不存在
下拉框套下拉框，但多一份保险不会错）。
"""

from PySide6.QtCore import QEvent, QObject
from PySide6.QtWidgets import (
    QAbstractScrollArea,
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QSlider,
)

# 会被滚轮改值的控件类型
GUARDED_TYPES = (QComboBox, QAbstractSpinBox, QSlider)


def _scroll_target(widget):
    """该把滚轮事件转交给谁：优先最近的滚动区域视口，其次第一个非守卫父控件。

    QScrollArea 的滚动逻辑在 **viewport** 上（QAbstractScrollArea::viewportEvent），
    所以必须找到 scroll area 并把事件发给它的 viewport，页面才会滚。
    """
    fallback = None
    w = widget.parentWidget()
    while w is not None:
        if isinstance(w, QAbstractScrollArea):
            return w.viewport()
        if fallback is None and not isinstance(w, GUARDED_TYPES):
            fallback = w
        w = w.parentWidget()
    return fallback


class WheelGuard(QObject):
    """拦截滚轮事件，阻止选择类控件被滚轮改值。"""

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Wheel and isinstance(obj, GUARDED_TYPES):
            target = _scroll_target(obj)
            if target is not None:
                QApplication.sendEvent(target, event)   # 页面继续滚动
            return True                                 # 吞掉：不改值
        return False


def install(app=None):
    """安装守卫。返回守卫对象（调用方需持有引用，否则会被 GC）。"""
    target = app or QApplication.instance()
    if target is None:
        return None
    guard = WheelGuard(target)
    target.installEventFilter(guard)
    return guard
