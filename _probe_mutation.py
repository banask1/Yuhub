# -*- coding: utf-8 -*-
"""变异测试：把实现逐个改坏，确认对应的新断言真的会失败。

为什么必须做这一步：本项目在"假通过"上栽过好几次（读不到源码 → 名字集合为
空 → 判定通过；只查一处透明而没留对照组）。新加的断言如果只是"跑起来是绿的"，
完全可能是它压根没测到东西。所以这里主动把代码改坏，看它红不红。

跑法（走 PowerShell，MainWindow 的硬件扫描会被 bash 沙箱掐掉）：
    powershell -File _run_mutation.ps1
"""
import json
import os
import sys
import textwrap
from string import Template

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.pop("QT_QPA_PLATFORM", None)

# 控制台/管道默认是 GBK（本机 CP936），而下面那句结果行里有个 "✓" —— 不强制
# UTF-8 的话 `print` 直接抛 UnicodeEncodeError，**每条用例都在出错的那一瞬间
# 崩掉**，日志里只剩 traceback，看着像"26 条断言一条都没抓住"（其实是被自己的
# 打印干掉的）。同目录的几个出图脚本早就这么防了，这里补上。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

import theme_selftest
from ui import motion, theme, theme_packs
from ui.main_window import NavButton
from ui.widgets import SegmentSlider, ToggleSwitch
from PySide6.QtCore import QRectF
from PySide6.QtWidgets import QPushButton

OUT = []


def say(s=""):
    OUT.append(str(s))
    print(s, flush=True)


def run_once(tag, suite="theme"):
    f = "_mutation_%s.json" % tag
    if suite == "hosts":
        import hostsaccel_selftest
        mod = hostsaccel_selftest
    elif suite == "uninstall":
        import uninstaller_selftest
        mod = uninstaller_selftest
    else:
        mod = theme_selftest
    try:
        mod.run(f)
    except Exception as exc:                     # noqa: BLE001
        return None, repr(exc)
    with open(f, encoding="utf-8") as fh:
        return json.load(fh), None


def failed_names(res):
    if res is None:
        return set()
    return {c["name"] for c in res["checks"] if not c["pass"]}


# 每个用例：(标记, 期望失败的断言名, 施加变异, 撤销变异)
CASES = []

# ---- 1) 曲线控制点被换回内置那档（软曲线）----
_orig_points = dict(motion._CURVES)


def m1():
    motion._CURVES["out"] = (0.25, 0.1, 0.25, 1.0)      # 内置 ease 的形状


def r1():
    motion._CURVES.clear()
    motion._CURVES.update(_orig_points)


CASES.append(("curve", "强 ease-out 的控制点就是技能库给的那一组", m1, r1))

# ---- 2) 退场改成比进场慢（把原来那个倒挂的问题放回去）----
_orig_out = motion.TOAST_OUT


def m2():
    motion.TOAST_OUT = motion.TOAST_IN + 40


def r2():
    motion.TOAST_OUT = _orig_out


CASES.append(("toastout", "提示：退场比进场快", m2, r2))

# ---- 3) 某个动效时长超预算 ----
_orig_pill = motion.PILL


def m3():
    motion.PILL = 900


def r3():
    motion.PILL = _orig_pill


CASES.append(("budget", "所有交互动效都在 300ms 预算内", m3, r3))

# ---- 4) 分段按钮基础规则退回 `border: none`（焦点环会挤动布局）----
_orig_qss = theme._QSS
_OLD = ("QPushButton#SegmentButton {\n"
        "    background: transparent;\n"
        "    /* 同侧栏项：透明边框占位 + 内边距减 1px，给焦点环留位置而不改尺寸 */\n"
        "    border: 1px solid transparent;\n"
        "    border-radius: 4px;\n"
        "    padding: 5px 13px;\n"
        "    color: $text_dim;\n"
        "}")


def m4():
    src = str(_orig_qss.template)
    assert _OLD in src, "找不到分段按钮的旧写法，变异用例失效"
    src = src.replace(_OLD,
                      "QPushButton#SegmentButton {\n"
                      "    background: transparent;\n"
                      "    border: none;\n"
                      "    border-radius: 4px;\n"
                      "    padding: 6px 14px;\n"
                      "    color: $text_dim;\n"
                      "}")
    theme._QSS = Template(src)


def r4():
    theme._QSS = _orig_qss


CASES.append(("border", "焦点环能生效的前提：基础规则已留好 1px 边框（不是 border: none）",
              m4, r4))

# ---- 5) 焦点规则里偷偷改边框宽度 ----
# 注意目标得选**自检真正会去读的那一块**：自检读的是
#   `QPushButton#PrimaryButton:focus,`（多选择器组）和 `QCheckBox:focus::indicator {`
# 两个块（见 theme_selftest 里 head 的写法）。这两块只在
# theme_packs.INTERACTION_QSS 里，而 ui/theme._QSS 的 template
# = 模板正文 + INTERACTION_QSS（见 theme.py 末尾的拼接），所以改
# theme._QSS 就能打到 "builtin" 那一份。
# 以前这里拿 `QSpinBox:focus {...}` 当锚点 —— 那是一条**单行**规则
# （`QSpinBox:focus { border-color: $accent; }`），而且不在自检读的两个块里，
# 一旦写法变动 assert 直接抛异常、整个变异脚本死掉（"用例失效"必须能自证，
# 不能让脚本崩在半路）。改用组内最后一条 + 紧跟的 QCheckBox 行当锚点。
_focus_old = ("QSpinBox:focus {\n"
              "    border-color: $accent;\n"
              "}\n"
              "QCheckBox:focus::indicator")


def m5():
    src = str(_orig_qss.template)
    assert _focus_old in src, "找不到焦点规则组，变异用例失效"
    src = src.replace(_focus_old,
                      "QSpinBox:focus {\n"
                      "    border: 3px solid $accent;\n"
                      "}\n"
                      "QCheckBox:focus::indicator")
    theme._QSS = Template(src)


def r5():
    theme._QSS = _orig_qss


CASES.append(("focusprop",
              "焦点规则只改边框颜色（改宽度/内边距就会挤动布局）", m5, r5))

# ---- 6) 把某一处按下态删掉 ----
def m6():
    src = str(_orig_qss.template)
    assert "QPushButton#MiniButton:pressed" in src
    src = src.replace("QPushButton#MiniButton:pressed", "QPushButton#MiniButton:Xpressed")
    theme._QSS = Template(src)


def r6():
    theme._QSS = _orig_qss


CASES.append(("pressed", "所有内置模板都补齐了按下态", m6, r6))

# ---- 7) 把"遗留的按下规则"放回内置模板 ----
# 这条历史遗留（`background: $accent` = 常态色）与末尾那条同优先级，
# 只靠"后写的赢"才没出事。放回去之后主要按钮的按下就会与常态同色。
#
# ⚠️ 目标必须是 build_qss() 的输出，**不能**往 theme._QSS 里塞。
# 自检跑到这里时活动的主题包是用户上次保存的那个（实测：MainWindow.__init__
# 里 `theme.set_pack(saved_pack)` 会把包恢复成 "sky glass"），而此时
# `build_qss()` 走的是"主题包自带 theme.qss"那条分支 —— 内置模板根本没被读，
# 往 _QSS 里加东西是打不到的（曾经这条变异就是这么静默失效的：
# 只有静态扫描那条红了，真正要守的"按下规则唯一"没红）。
# 包一层 build_qss 才能与"当前是哪个包"解耦。
_orig_build_qss = theme.build_qss


def m7():
    acc = theme.current().get("accent", "#3b82f6")

    def _bad():
        return (_orig_build_qss()
                + "\nQPushButton#PrimaryButton:pressed"
                  " { background: %s; }\n" % acc)

    theme.build_qss = _bad


def r7():
    theme.build_qss = _orig_build_qss


CASES.append(("legacy", "主要按钮的按下规则唯一、且颜色是现算的加深档",
              m7, r7))

# ---- 7b) 新模板常量里又混进旧写法（静态扫描那条）----
# 与上一条互补：上一條守"运行期 build_qss 里只有一条按下规则"，这条守
# "源码常量里不许再躺着旧写法"（否则新装用户与升级上来的用户行为会不一致）。
_orig_sky_qss = theme_packs.SKY_QSS


def m7b():
    theme_packs.SKY_QSS = (_orig_sky_qss
                           + "\nQPushButton#PrimaryButton:pressed"
                             " { background: $accent; }\n")


def r7b():
    theme_packs.SKY_QSS = _orig_sky_qss


CASES.append(("legacyconst", "新模板里不留旧写法（否则新装与升级上来的行为不一致）",
              m7b, r7b))

# ---- 8) 让焦点环色 == 填充色（主要按钮上就看不见了）----
_orig_ring = theme._ring_color


def m8():
    theme._ring_color = lambda fill, strength=0.72: fill


def r8():
    theme._ring_color = _orig_ring


CASES.append(("ringsame", "两份模式都注入了 accent_ring（且与填充色不同）",
              m8, r8))

# ---- 9) 增量型补丁去掉显式幂等标记（退回"默认取新写法"）----
_tp_orig_patches = theme_packs._SKY_QSS_PATCHES


def m9():
    bad = []
    for it in _tp_orig_patches:
        if len(it) == 3 and it[2] == theme_packs._INTERACTION_MARK:
            bad.append((it[0], it[1]))          # 退回二元组 = 用默认标记
        else:
            bad.append(it)
    theme_packs._SKY_QSS_PATCHES = tuple(bad)


def r9():
    theme_packs._SKY_QSS_PATCHES = _tp_orig_patches


CASES.append(("nomark", "增量型补丁都给了跨版本稳定的显式标记", m9, r9))

# ---- 10) 幂等标记写错（模板里根本不存在的字符串）----
# 这条直接打在 upgrade_builtin_qss 的**默认参数**上 —— 它在函数定义时就绑定
# 好了，只改模块属性是打不到实际执行的代码路径的（这点很容易写错变异）。
#
# 预期会连带红两条，都是真红不是假红：标记失效后连**第一次**升级都会出问题 ——
# 补丁 (3) 追加的交互态块**自带**焦点环，随后补丁 (7) 因为认不出"已经打过了"
# 又追加一份，于是焦点环变成两份，`老盘模板能升到当前版本` 也跟着失败。
# 这恰好说明那条显式标记不是装饰。
_orig_defaults = theme_packs.upgrade_builtin_qss.__defaults__


def m10():
    bad = tuple((it[0], it[1], "/* nope */") if len(it) == 3 else it
                for it in _tp_orig_patches)
    theme_packs.upgrade_builtin_qss.__defaults__ = (theme_packs.QSS_STAMP, bad)


def r10():
    theme_packs.upgrade_builtin_qss.__defaults__ = _orig_defaults


CASES.append(("badmark", "补丁表重跑一遍不会把新规则追加第二遍（幂等）",
              m10, r10))

# ===========================================================================
# 以下覆盖 v0.15beta 新增的「果冻弹簧 + 浅蓝液态玻璃」那批断言。
#
# 为什么单开一组：第 ⑰ 组一次加了 47 条断言，全是"跑起来是绿的"。
# 本项目在"假通过"上栽过（读不到源码 → 集合为空 → 判定通过），所以
# 新断言同样得逐个改坏实现、看它红不红，否则等于没写。
# ===========================================================================

# ---- 11) 果冻弹簧被换成"不过冲"的形状（换名字的 ease-out）----
_orig_spring = motion._SPRINGS["spring"]


def m11():
    motion._SPRINGS["spring"] = (
        ((0.32, 0.86), (0.55, 0.94), (0.66, 0.99)),
        ((0.78, 1.00), (0.90, 1.00), (1.00, 1.00)),
    )


def r11():
    motion._SPRINGS["spring"] = _orig_spring


CASES.append(("spring", "弹簧曲线 spring 真的过冲（峰值 > 1.03）", m11, r11))

# ---- 12) 液态玻璃底色被压暗（"浅蓝"变成深蓝）----
# 注意**不能**只把 tint 换成 accent 本色来测"对照组：主体色已明显不同于
# 实心 accent 蓝" —— 实测那样 delta 仍有 ~67（77% 的 accent 压在近黑侧栏上
# 再叠高光，早就偏开了），> 30 的阈值抓不住它。真正能分区分的判据是
# "底色够浅"：把 tint 压到深蓝，亮度断言立刻红。
_orig_liquid = theme.liquid_palette


def _liquid_with(**over):
    def _f(palette=None):
        d = dict(_orig_liquid(palette))
        d.update(over)
        return d
    return _f


def m12():
    theme.liquid_palette = _liquid_with(tint="#1b3a6b")


def r12():
    theme.liquid_palette = _orig_liquid


CASES.append(("liquidtint", "[dark] 液态玻璃底色够浅（亮度 > 0.45）",
              m12, r12))

# ---- 13) 玻璃上的文字改回白色（浅蓝底 + 白字 = 1.6:1，读不出来）----
def m13():
    theme.liquid_palette = _liquid_with(text="#ffffff")


def r13():
    theme.liquid_palette = _orig_liquid


CASES.append(("liquidtext", "[dark] 玻璃上的文字是深色（亮度 < 0.25）",
              m13, r13))

# ---- 14) 滑块不拉伸了（果冻感的主要来源被拿掉）----
_orig_strmax = ToggleSwitch.STRETCH_MAX


def m14():
    ToggleSwitch.STRETCH_MAX = 0.0


def r14():
    ToggleSwitch.STRETCH_MAX = _orig_strmax


CASES.append(("stretch", "移动中滑块被拉长成胶囊（果冻感的主要来源）",
              m14, r14))

# ---- 15) 按下不再放大滑块（丢掉参考实现的 pressedScale）----
_orig_press_scale = ToggleSwitch.PRESS_SCALE


def m15():
    ToggleSwitch.PRESS_SCALE = 0.0


def r15():
    ToggleSwitch.PRESS_SCALE = _orig_press_scale


CASES.append(("pressscale", "按下时滑块放大（对应参考实现的 pressedScale）",
              m15, r15))

# ---- 16) 切页位移不用轻弹了（换成单调的收尾）----
_orig_soft = motion._SPRINGS["spring_soft"]


def m16():
    motion._SPRINGS["spring_soft"] = (
        ((0.28, 0.80), (0.48, 0.92), (0.62, 0.98)),
        ((0.78, 1.00), (0.90, 1.00), (1.00, 1.00)),
    )


def r16():
    motion._SPRINGS["spring_soft"] = _orig_soft


CASES.append(("pagespring", "切页位移用轻弹曲线（有过冲但比果冻温和）",
              m16, r16))

# ---- 17) 动效强度调节被偷偷加回来（reduced() 又开始返回真）----
_orig_reduced = motion.reduced


def m17():
    motion.reduced = lambda: True


def r17():
    motion.reduced = _orig_reduced


CASES.append(("reduced", "动效强度恒定：reduced() 永远为假", m17, r17))

# ---- 18) dur() 又把时长折算掉（等于强度可调回来了）----
_orig_dur = motion.dur


def m18():
    motion.dur = lambda ms, kind="move": 0 if kind == "fade" else int(ms)


def r18():
    motion.dur = _orig_dur


CASES.append(("durfold", "动效强度恒定：dur() 原样返回时长（不再折算）",
              m18, r18))

# ---- 19) 滑块拉伸改回"单侧拖尾"（用户报的"白色圆圈变正方形"）----
# 这是 v0.15.1 修掉的那个 bug 的原始写法：向右走时 `x -= extra`，起步那一帧
# 左缘直接到 −5.4、被控件边界一刀削平。断言"整条行程都不戳出控件"必须抓住它。
_orig_knob_rect = ToggleSwitch.knob_rect


def m19():
    def _leaky(self, offset=None, stretch=None):
        t = self._offset if offset is None else float(offset)
        t = max(0.0, min(1.0, t))
        st = self._stretch if stretch is None else float(stretch)
        d = self.knob_diameter()
        travel = max(0.0, self.width() - 2.0 * self.KNOB_MARGIN - d)
        x = self.KNOB_MARGIN + t * travel
        w = d
        if st > 0:
            extra = d * self.STRETCH_MAX * max(0.0, min(1.0, st))
            if self._stretch_dir >= 0:
                x -= extra
            w += extra
        return QRectF(x, self.KNOB_MARGIN, w, d)
    ToggleSwitch.knob_rect = _leaky


def r19():
    ToggleSwitch.knob_rect = _orig_knob_rect


CASES.append(("knobleak",
              "整条行程 × 各种拉伸量都不戳出控件（「白圆变方块」的根因）",
              m19, r19))

# ---- 20) 滑动片跑到按钮上面（会盖住选中项的文字）----
_orig_sld_init = SegmentSlider.__init__


def m20():
    def _bad(self, host, parent=None, radius=None):
        _orig_sld_init(self, host, parent, radius)
        self.raise_()               # 抬到最顶 = 背景层变成了覆盖层
    SegmentSlider.__init__ = _bad


def r20():
    SegmentSlider.__init__ = _orig_sld_init


CASES.append(("slidertop",
              "选中片是背景层（压在所有按钮之下，否则盖住文字）", m20, r20))

# ---- 21) 滑动片认了"未布局按钮"的 640x480 默认几何 ----
_orig_target_rect = SegmentSlider._target_rect


def m21():
    def _naive(self, btn=None):
        for b in self._buttons:
            if not b.isChecked():
                continue
            r = QRectF(b.geometry())
            if r.width() > 1 and r.height() > 1:
                return r
        return None
    SegmentSlider._target_rect = _naive


def r21():
    SegmentSlider._target_rect = _orig_target_rect


CASES.append(("segdefault",
              "未布局时不会把按钮的 640x480 默认几何当成目标", m21, r21))

# ---- 22) 点一下挡位时，片只认"第一个 checked"当目标（修复前的行为）----
# 点 `QPushButton` 是**先翻自己的 checked、再 emit clicked**：`toggled(True)`
# 的瞬间旧按钮往往还没被取消，只扫"第一个 checked"会算到旧位置上 →
# `r == self._to` → "目标没变" → 动画被吃掉（用户报的"选中自定义要双击"）。
_orig_target_rect2 = SegmentSlider._target_rect


def m22():
    def _blind(self, btn=None):
        for b in self._buttons:          # 无视"是谁变成选中的"
            r = self._rect_of(b)
            if r is not None:
                return r
        return None
    SegmentSlider._target_rect = _blind


def r22():
    SegmentSlider._target_rect = _orig_target_rect2


CASES.append(("segpick",
              "点一次「自定义」就播切换动画（不必双击）", m22, r22))

# ===========================================================================
# 以下覆盖 v0.15.4beta 的「侧栏按钮 hover 自绘」那批断言（theme 自检 ②b 段）。
#
# 这一组的背景：QSS 的 `:hover` 在本项目的侧栏按钮上**不生效**（State_MouseOver
# 恒 False），而且 `QWidget.grab()` 渲染时会临时剥掉 `WA_UnderMouse` —— 也就是
# 说"用截图证明 hover 有效果"这件事**只有自绘才可能做到**。断言一旦是摆设，
# 用户的 bug（鼠标移上去没动画）就会悄悄回来。
# ===========================================================================

# ---- 23) hover 反馈退回"只靠 QSS :hover"（= 用户报的那个 bug 本身）----
# 自绘膜不画了，而 qss 的 :hover 在 grab 出来的图上永远看不到 →
# "外观确实变了"必须立刻红。
_orig_hover_paint = NavButton.paintEvent


def m23():
    NavButton.paintEvent = lambda self, ev: QPushButton.paintEvent(self, ev)


def r23():
    NavButton.paintEvent = _orig_hover_paint


CASES.append(("hoverqss",
              "侧栏 hover 时按钮外观确实变了（自绘膜的像素证据）", m23, r23))

# ---- 24) hover 膜戳出按钮（弹簧铺开时不收窄，直接铺满整个几何）----
_orig_hover_rect = NavButton._hover_rect


def m24():
    def _leak(self, t):
        r = QRectF(self.rect()).adjusted(-8.0, -6.0, 8.0, 6.0)
        return r
    NavButton._hover_rect = _leak


def r24():
    NavButton._hover_rect = _orig_hover_rect


CASES.append(("hoverleak",
              "hover 膜在任何进度下都在按钮内（含 16% 过冲）", m24, r24))

# ---- 25) 选中项也叠膜（会把侧栏那块液态玻璃选中片再加重一层）----
# paintEvent 里的闸门是 `if t <= 0.004 or self.isChecked(): return`。
# 这里把那道 `isChecked()` 闸去掉 —— 只在自绘那一瞬间把 Python 侧的
# `isChecked` 顶成 False（`QPushButton.paintEvent` 是 C++ 实现，不受影响）。
_orig_ischecked = NavButton.isChecked
_orig_hover_paint2 = NavButton.paintEvent


def m25():
    def _paint(self, ev):
        NavButton.isChecked = lambda _s: False
        try:
            _orig_hover_paint2(self, ev)
        finally:
            NavButton.isChecked = _orig_ischecked
    NavButton.paintEvent = _paint


def r25():
    NavButton.paintEvent = _orig_hover_paint2
    NavButton.isChecked = _orig_ischecked


CASES.append(("hoverchk",
              "选中项 hover 不再叠膜（不会盖住选中片）", m25, r25))


# ---- 27) hosts 清洗被改坏：BEGIN 标记不分服务一律删（真实踩过的跨区块误删坑）----
def m_hosts_clean():
    import hostsaccel as _ha
    orig = _ha.clean_hosts_lines

    def broken(lines, service):
        cleaned, removed = [], 0
        for line in lines:
            s = line.strip()
            if "Yuhub Hosts Acceleration [" in s and "End" not in s:
                removed += 1                    # 坏实现：把别人的开始标记也删了
                continue
            cleaned.append(line)
        return cleaned, removed

    _ha._m_orig_clean = orig
    _ha.clean_hosts_lines = broken


def r_hosts_clean():
    import hostsaccel as _ha
    _ha.clean_hosts_lines = _ha._m_orig_clean


CASES.append(("hostsclean", "清洗不移除别人的 github 区块",
              m_hosts_clean, r_hosts_clean, "hosts"))

# ---- 28) IP 校验被放松：只要像 IPv4 就放行（私网/保留段全漏进来）----
def m_hosts_ip():
    import hostsaccel as _ha
    orig = _ha.is_valid_public_ipv4

    def loose(ip_str):
        parts = str(ip_str).strip().split(".")
        return len(parts) == 4 and all(p.isdigit() for p in parts)

    _ha._m_orig_ip = orig
    _ha.is_valid_public_ipv4 = loose


def r_hosts_ip():
    import hostsaccel as _ha
    _ha.is_valid_public_ipv4 = _ha._m_orig_ip


CASES.append(("hostsip", "非公网 IP 127.0.0.1 被拒",
              m_hosts_ip, r_hosts_ip, "hosts"))

# ---- 29) UI 回调纪律被破坏：后台线程回调里直接 setText 改界面 ----
def m_hosts_cb():
    import hostsaccel_selftest as _hst
    orig = _hst._module_source
    src = orig("accel_page.py")
    assert src and "_domain_done.emit" in src, "accel_page 源码读不到"
    mutated = src.replace(
        "self._domain_done.emit(svc, done, total, domain, ip, ms, error)",
        "self._domain_done.emit(svc, done, total, domain, ip, ms, error)\n"
        "            self._cards[svc]._progress.setText('后台线程直接改界面')")
    assert mutated != src, "锚点没命中"

    def fake(filename, _orig=orig, _mut=mutated):
        return _mut if filename == "accel_page.py" else _orig(filename)

    _hst._m_orig_ms = orig
    _hst._module_source = fake


def r_hosts_cb():
    import hostsaccel_selftest as _hst
    _hst._module_source = _hst._m_orig_ms


CASES.append(("hostscb", "后台线程回调只 emit、绝不直接改界面（硬规则 3 的静态防线）",
              m_hosts_cb, r_hosts_cb, "hosts"))

# ---- 30) SNI 解析被改坏：永远返回空串（代理等于瞎了）----
def m_sni():
    import hostssniproxy as _hp
    orig = _hp.parse_client_hello_sni
    _hp._m_orig_sni = orig
    _hp.parse_client_hello_sni = lambda data: ""


def r_sni():
    import hostssniproxy as _hp
    _hp.parse_client_hello_sni = _hp._m_orig_sni


CASES.append(("sniparse", "ClientHello 往返解析 store.steampowered.com",
              m_sni, r_sni, "hosts"))

# ---- 31) 代理模式载荷校验被放松：任意 IP 都接受 ----
def m_proxypayload():
    import base64 as _b64, json as _json
    import hostsaccel as _ha
    orig = _ha.elevated_hosts_main

    def stripped(payload_b64, _orig=orig):
        # 模拟真实缺陷：载荷在传输/序列化中丢失 proxy 标记 —— 校验退回
        # 直连分支，代理模式的 127.0.0.1 条目会被公网校验拒绝、而带真实
        # IP 的载荷反而放行（模式与地址一致性防线被拆掉）。
        try:
            obj = _json.loads(_b64.b64decode(payload_b64.encode("ascii"))
                              .decode("utf-8"))
            obj.pop("proxy", None)
            nb = _b64.b64encode(_json.dumps(
                obj, ensure_ascii=False, separators=(",", ":"))
                .encode("utf-8")).decode("ascii")
        except Exception:
            return _orig(payload_b64)
        return _orig(nb)

    _ha._m_orig_elev = orig
    _ha.elevated_hosts_main = stripped


def r_proxypayload():
    import hostsaccel as _ha
    _ha.elevated_hosts_main = _ha._m_orig_elev


CASES.append(("proxymode", "代理模式带真实公网 IP → 3（模式与地址必须一致）",
              m_proxypayload, r_proxypayload, "hosts"))

# ---- 32) 映射缓存过滤被去掉：什么 IP 都往缓存里写 ----
def m_maploose():
    import hostsaccel as _ha
    # 过滤是双层的（save / load 各一道），必须同时拆掉才暴露缺陷 ——
    # 这本身就是变异测试发现的设计事实：单层被拆时行为仍然安全。
    orig_save, orig_load = _ha.save_map_cache, _ha.load_map_cache

    def loose_save(mapping, _orig=orig_save):
        import json as _json, os as _os
        p = _ha._map_cache_path()
        if not p:
            return False
        try:
            tmp = p + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fp:
                _json.dump({str(k): str(v) for k, v in dict(mapping).items()},
                           fp)
            _os.replace(tmp, p)
            return True
        except OSError:
            return False

    def loose_load(_orig=orig_load):
        import json as _json
        p = _ha._map_cache_path()
        if not p:
            return {}
        try:
            with open(p, "r", encoding="utf-8") as fp:
                data = _json.load(fp)
        except (OSError, ValueError):
            return {}
        return {str(k): str(v) for k, v in data.items()} \
            if isinstance(data, dict) else {}

    _ha._m_orig_map_save, _ha._m_orig_map_load = orig_save, orig_load
    _ha.save_map_cache = loose_save
    _ha.load_map_cache = loose_load


def r_maploose():
    import hostsaccel as _ha
    _ha.save_map_cache = _ha._m_orig_map_save
    _ha.load_map_cache = _ha._m_orig_map_load


CASES.append(("ipmaploose", "缓存拒绝 127.0.0.1（代理映射必须是真实 IP）",
              m_maploose, r_maploose, "hosts"))


# ---- 31) 下拉弹出区：去掉 topLevelAt 守卫（v1.0.2 问题 1 的回归）----
# 变异点落在断言真正读取的那个对象上：theme_selftest 通过 win._edge_at
# 判边，而 _edge_at 是 MainWindow 的**绑定方法**。直接在类上换成"没有
# topLevelAt 守卫"的旧实现，就能把那条新断言打红。
#
# ⚠️ 不能去改 main_window.py 的源码再 import —— 一个用例一个进程，
# restore 无处落脚；改类属性最干净。

def m_edgeguard():
    from PySide6.QtWidgets import QApplication as _QA
    from PySide6.QtCore import Qt as _Qt
    import ui.main_window as _mw

    def _old_edge_at(self, global_pos):
        if self.isMaximized():
            return _Qt.Edges()
        g = self.frameGeometry()
        m = _mw.RESIZE_MARGIN
        edges = _Qt.Edges()
        if global_pos.x() <= g.left() + m:
            edges |= _Qt.Edge.LeftEdge
        if global_pos.x() >= g.right() - m:
            edges |= _Qt.Edge.RightEdge
        if global_pos.y() >= g.bottom() - m:
            edges |= _Qt.Edge.BottomEdge
        return edges

    _mw._m_orig_edge_at = _mw.MainWindow._edge_at
    _mw.MainWindow._edge_at = _old_edge_at


def r_edgeguard():
    import ui.main_window as _mw
    if hasattr(_mw, "_m_orig_edge_at"):
        _mw.MainWindow._edge_at = _mw._m_orig_edge_at
        del _mw._m_orig_edge_at


CASES.append(("edgeguard", "主窗口外（模拟下拉弹出区）不判为缩放边缘",
              m_edgeguard, r_edgeguard))


# ---- 32) 占用被误判成权限：把 _is_permission_error 改回"先判异常类型" ----
# v1.0.2 问题 3 的第二个根因：Python 把 WinError 32（共享冲突）也包成
# PermissionError，旧实现 isinstance 先命中 → 占用被当成权限，MoveFileEx
# 兜底永远走不到。变异成旧写法，断言 ⑥ 必须红。

def m_permorder():
    import uninstaller as _un

    def _old(exc):
        if exc is None:
            return False
        if isinstance(exc, PermissionError):
            return True
        return getattr(exc, "errno", None) in (13, 1)

    _un._m_orig_is_perm = _un._is_permission_error
    _un._is_permission_error = _old


def r_permorder():
    import uninstaller as _un
    if hasattr(_un, "_m_orig_is_perm"):
        _un._is_permission_error = _un._m_orig_is_perm
        del _un._m_orig_is_perm


CASES.append(("permorder", "被占用文件被判定为「非权限问题」",
              m_permorder, r_permorder, "uninstall"))


# ---- 33) 夺取所有权分支被删除：_delete_dir 退回"只有摘只读 + 逐项删" ----
# v1.0.2 问题 3 的第一个根因：ACL 受阻时连管理员也删不掉，解药是
# takeown + icacls /reset。最直接的"变坏"就是把这道分支拿掉。
#
# ⚠️ 不能用 is_admin 当变异点：自检 ⑩ 也有 is_admin 前置，两者会**同步**
#    变成"非管理员"，于是自洽通过、表面全绿（这正是"它绿了"和"它测到了"
#    的区别）。所以这里直接给 _delete_dir 换一个**删掉了 takeown 分支**
#    的替身 —— 从真实源码里切掉那一段再 exec，保证与线上实现只差这一处。

def m_takeown():
    import inspect
    import uninstaller as _un

    src = inspect.getsource(_un._delete_dir)
    # 去掉函数体的缩进（def 行本身无缩进）
    lines = src.splitlines(True)
    body = "".join(lines[1:])
    body = textwrap.dedent(body)
    # 精确切除 takeown 分支（从注释到该 if 块结束）。
    # 注意：后面的代码仍会读 acl_reset（在"权限受阻"分支里用来决定
    # 提示措辞），所以切除处必须补回它的定义，否则变异版会 NameError、
    # 整个检查函数抛异常，反而把真正要验的断言掩盖成"聚合兜底失败"。
    # dedent 会把函数体整体拉到 0 缩进（docstring 行本身就是 0 缩进，
    # 公共前缀因此是空），所以标记串**不能**带前导空格。
    start = body.find("# 第二道关键修复：ACL 受阻时夺取所有权")
    marker = "# 逐项删（从最深层往上）"
    end = body.find(marker)
    assert start != -1 and end != -1 and start < end, \
        "找不到 takeown 分支，变异用例失效（实现改了）"
    stripped = body[:start] + "acl_reset = False\n" + body[end:]
    ns = {"os": _un.os, "shutil": _un.shutil, "time": _un.time,
          "IS_WIN": _un.IS_WIN, "is_admin": _un.is_admin,
          "_take_ownership": _un._take_ownership,
          "_dir_tree_clear_readonly": _un._dir_tree_clear_readonly,
          "_dir_size": _un._dir_size, "_clear_readonly": _un._clear_readonly,
          "_is_permission_error": _un._is_permission_error,
          "_move_file_delayed": _un._move_file_delayed}
    exec(compile("def _delete_dir(path):\n" + textwrap.indent(stripped, "    "),
                 "<mutation>", "exec"), ns)
    _un._m_orig_delete_dir = _un._delete_dir
    _un._delete_dir = ns["_delete_dir"]


def r_takeown():
    import uninstaller as _un
    if hasattr(_un, "_m_orig_delete_dir"):
        _un._delete_dir = _un._m_orig_delete_dir
        del _un._m_orig_delete_dir


CASES.append(("takeown", "ACL 受阻时 _delete_dir 走 takeown 夺权分支",
              m_takeown, r_takeown, "uninstall"))


# ⚠️ 标题**不在这里**打印：`--list` 的输出会被 _run_mutation.ps1 逐行当用例名
# 用，多出一行标题就会去跑一个不存在的用例、退出码非 0，最后被判成"这些断言
# 没抓住"。标题挪进 main() 的非 --list 分支了。


def case_by_tag(tag):
    for item in CASES:
        if item[0] == tag:
            return item
    return None


def do_base():
    base, err = run_once("base")
    if base is None:
        say("基线跑不起来：%s" % err)
        return 1
    base_fail = failed_names(base)
    say("基线：ok=%s 断言数=%d 失败=%s"
        % (base["ok"], len(base["checks"]), sorted(base_fail) or "无"))
    if base_fail:
        say("!! 基线就有失败项，后面的结论不可信")
        return 1
    return 0


# ---- 33) 「秒加速」被改回"先测速再写盘"（Steam++ 之前的老行为）----
def m_fastaccel():
    """把 _start_enable 换回"无论什么模式都先跑一遍测速"。

    这正是用户报的"不像 Steam++ 那样秒开"的根因，所以必须有断言逮住它：
    代理模式拨开开关时应该**零测速**、直接写全量 127.0.0.1。
    """
    import ui.pages.accel_page as _ap
    _ap.AccelPage._m_orig_enable = _ap.AccelPage._start_enable
    _ap.AccelPage._start_enable = lambda self, svc: self._start_optimize(svc)


def r_fastaccel():
    import ui.pages.accel_page as _ap
    _ap.AccelPage._start_enable = _ap.AccelPage._m_orig_enable


CASES.append(("fastaccel", "秒加速：同步启用路径上一次测速都没有（快就快在这里）",
              m_fastaccel, r_fastaccel, "hosts"))

# ---- 34) 「关闭动作被忙碌挡住」被放回来（v1.0.2 的「关不掉」）----
def m_canceloff():
    """把 _start_disable 换回旧实现：忙碌中一律把开关弹回、什么都不做。

    这就是用户报的「关不掉」——点关没反应，得干等测速跑完。断言逮不住它
    的话，这个 bug 会静悄悄地回来。
    """
    import ui.pages.accel_page as _ap

    def old_disable(self, svc):
        if self._busy[svc]:
            self._sync_switch(svc)
            return
        self._start_clean(svc)

    _ap.AccelPage._m_orig_disable = _ap.AccelPage._start_disable
    _ap.AccelPage._start_disable = old_disable


def r_canceloff():
    import ui.pages.accel_page as _ap
    _ap.AccelPage._start_disable = _ap.AccelPage._m_orig_disable


CASES.append(("canceloff", "忙碌中拨到关 → 立刻清理，不被 busy 挡住",
              m_canceloff, r_canceloff, "hosts"))

# ---- 35) 回调纪律扫描名单被写窄（新函数成了扫描盲区）----
def m_astscan():
    """把 accel_page 里的 _start_enable 改名，模拟"名单没跟上重构"。

    这种漏最阴：断言照样全绿，但那段代码其实**根本没被扫到**
   （"它绿了"≠"它测到了"）。所以专门有一条断言盯着"名单覆盖全部后台入口"。
    """
    import hostsaccel_selftest as _hst
    orig = _hst._module_source
    src = orig("accel_page.py")
    assert src and "def _start_enable" in src, "锚点没命中"
    mutated = src.replace("def _start_enable", "def _start_enable_RENAMED", 1)

    def fake(filename, _orig=orig, _mut=mutated):
        return _mut if filename == "accel_page.py" else _orig(filename)

    _hst._m_orig_ms2 = orig
    _hst._module_source = fake


def r_astscan():
    import hostsaccel_selftest as _hst
    _hst._module_source = _hst._m_orig_ms2


CASES.append(("astscan", "回调纪律扫描覆盖全部后台入口（名单不能漏）",
              m_astscan, r_astscan, "hosts"))


def do_one(tag):
    """只跑一个用例。

    **一个用例一个进程**是必须的：每个用例都会起一个 MainWindow，而它带着
    一个后台监视线程（live_monitor）。在同一个进程里连着跑十几个，线程会
    越积越多、指向已删除的 QObject，最终在某个用例上把进程直接带走
    （实测：跑到第 17 个用例时 python 无栈退出，rc=1，日志断在半截，
    剩下的用例一条都没跑 —— 表面看是"最后一组断言没抓"，其实是被前面的
    线程拖死的）。分开跑还顺带解决了用例之间的状态污染（QSettings 里
    活动主题包、硬件缓存等）。
    """
    item = case_by_tag(tag)
    if item is None:
        say("!! 没有这个用例：%s（可用：%s）"
            % (tag, ", ".join(c[0] for c in CASES)))
        return 2
    # 先落一个保守的 MISSED：万一进程在判定前就崩了（MainWindow 相关用例
    # 偶发无栈退出），脚本能读到"崩了"而不是"没有文件"，不会把它当成
    # 两边都不算的灰区。
    try:
        with open("_mut_verdict_%s.txt" % tag, "w", encoding="ascii") as f:
            f.write("MISSED\nreason=process-died-before-judging\n")
    except OSError:
        pass
    suite = item[4] if len(item) > 4 else "theme"
    _, expect, mutate, restore = item[:4]
    mutate()
    try:
        res, err = run_once(tag, suite)
    finally:
        restore()
    if res is None:
        say("  [%s] 跑不起来：%s" % (tag, err))
        return 1
    got = failed_names(res)
    hit = expect in got
    say("  [%s] %s" % (tag, "抓住 ✓" if hit else "!! 没抓住"))
    say("        期望失败：%s" % expect)
    say("        实际失败：%s" % (sorted(got) or "（没有失败项 —— 断言是摆设）"))
    say("        （其余断言仍应全绿，本次共失败 %d 条）" % len(got))
    extra = got - {expect}
    if extra:
        say("        ⚠ 除了期望的那条，还有别的红了：%s" % sorted(extra))
    # 判定结果单独落一个文件，供 _run_mutation.ps1 读取。
    #
    # 为什么不让脚本看退出码：本进程会起 MainWindow，它的后台监视线程
    # 有时往 stderr 抛 "RuntimeError: Signal source has been deleted"，
    # 而 PowerShell 的 `$out = & $py ... 2>&1` 在遇到 native 命令写 stderr
    # 时会抛 NativeCommandError —— **$out 与 $LASTEXITCODE 都会失真**。
    # 症状：用例其实抓住了，脚本却记成 "NOT CAUGHT" 且报告里没有它的输出
    #（v1.0.2 时 hoverqss / hoverleak / hoverchk / edgeguard 四个用例
    # 就这么被冤枉过，单独跑全是 rc=0 抓住 ✓）。用文件判定彻底绕开噪音。
    try:
        # 纯 ASCII + 无 BOM：PowerShell 5.1 的 Get-Content 按 ANSI 读 UTF-8，
        # 带 BOM 时首行会变成 "\ufeffCAUGHT" 而比较失败（本项目的老坑）。
        # expect/got 里有中文、不能原样写进 ascii 文件，所以只留判定 + 计数
        #（详情看 _mutation_report.txt，那是 utf-8 的）。
        txt = "%s\nexpect_len=%d\ngot_count=%d\n" % (
            "CAUGHT" if hit else "MISSED", len(expect), len(got))
        with open("_mut_verdict_%s.txt" % tag, "w", encoding="ascii") as f:
            f.write(txt)
    except OSError:
        pass
    return 0 if hit else 1


def main():
    argv = sys.argv[1:]
    # `--list` 必须第一个判断、且不能有任何别的输出（见上面的注释）
    if argv and argv[0] == "--list":
        print("\n".join(c[0] for c in CASES))
        return 0
    say("=" * 66)
    say("变异测试：逐个改坏实现，确认对应断言真的会红")
    say("=" * 66)
    if not argv:
        rc = do_base()
    elif argv[0] == "base":
        rc = do_base()
    else:
        rc = do_one(argv[0])
    with open("_mutation_report.txt", "a", encoding="utf-8") as f:
        f.write("\n".join(OUT) + "\n")
    return rc


if __name__ == "__main__":
    sys.exit(main())
