# -*- coding: utf-8 -*-
"""打包后自检：主题包系统。

为什么不靠外部脚本
------------------
主题包要读写「文档目录」，而**打包后的 exe 里环境变量与源码运行时不同**
（PyInstaller onefile 的 `sys._MEIPASS`、可能的权限差异），
文档目录的真实路径必须由 exe 自己算一遍才准。

同时主题切换是"扫盘 + 合并色板 + 重新生成全局 QSS + 重绘所有页面"的
组合动作，任何一环坏掉的表现都是"界面颜色不对"这种难以远程诊断的问题，
所以固化进自检。

用法：`Yuhub.exe --theme-selftest <结果json路径>`
返回码：0 全部通过 / 1 有断言失败 / 2 参数错误 / 4 结果写盘失败
"""

import json
import os
import shutil
import tempfile
import time


def _qss_templates():
    """所有**可能真正生效**的 QSS 模板：内置那份 + 每个主题包自带的 theme.qss。

    单独抽出来是因为"改了代码却没生效"这类问题几乎都出在这里：主题包自带
    theme.qss 时会**整份替换**内置模板（见 ui/theme.build_qss），只改内置的
    那份等于没改——v0.9beta 的版本号贴片就差点栽在这上面。
    返回 [(来源名, 模板文本)]。
    """
    from ui import theme as _theme
    # _QSS 是 string.Template，取原始字符串（不是 Template 对象）
    builtin = getattr(_theme._QSS, "template", _theme._QSS)
    out = [("builtin", str(builtin))]
    try:
        for nm, pk in _theme.packs().items():
            if getattr(pk, "qss", ""):
                out.append((nm, pk.qss))
    except Exception:          # noqa: BLE001 —— 扫描失败不影响其它检查
        pass
    return out


def run(out_file, timeout_sec=60):
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    app.setApplicationName("Yuhub")

    from ui import theme, theme_packs

    checks = []
    result = {"ok": False, "checks": checks, "info": {}}

    def chk(name, cond, detail=""):
        checks.append({"name": name, "pass": bool(cond), "detail": detail})

    def pump(ms):
        end = time.time() + ms / 1000.0
        while time.time() < end:
            app.processEvents()
            time.sleep(0.01)

    import sys
    result["info"]["frozen"] = bool(getattr(sys, "frozen", False))
    result["info"]["exe"] = sys.executable
    result["info"]["docs"] = theme_packs.documents_dir()
    result["info"]["themes_root"] = theme_packs.themes_root()

    # ---------------- ⓪ 清掉上次跑崩留下的临时主题包 ----------------
    # 自检会临时建 `__theme_selftest_<pid>` / `__bad_theme_<pid>` 两个包，正常
    # 路径在 `finally` 里删掉。但**进程被外部杀掉**时（跑太久被超时掐、构建脚本
    # 提前返回）`finally` 根本不会执行，临时包就永久留在用户的「文档/Yuhub」里
    # —— 而 `theme.packs()` 并不按下划线过滤，它们会**出现在主题选择列表里**，
    # 用户会看到一串叫 `__theme_selftest_12112` 的主题，还会一直堆积。
    # 所以每次开跑先扫一遍：pid 等于本进程的一定是残留（本进程的目录还没建），
    # 别的 pid 必然来自已经死掉的进程（pid 不会复用）。
    # **只碰这两个前缀**，用户自己建的主题一个字节都不动。
    _root = theme_packs.themes_root()
    _stale = []
    try:
        _me = os.getpid()
        for _n in os.listdir(_root):
            if not (_n.startswith("__theme_selftest_")
                    or _n.startswith("__bad_theme_")):
                continue
            try:
                _pid = int(_n.rsplit("_", 1)[1])
            except (IndexError, ValueError):
                continue
            if _pid != _me:
                _stale.append(_n)
        for _n in _stale:
            shutil.rmtree(os.path.join(_root, _n), ignore_errors=True)
    except OSError:
        pass
    chk("启动时清掉上次崩溃留下的临时主题包",
        all(not os.path.isdir(os.path.join(_root, _n)) for _n in _stale),
        "清掉=%s" % (_stale or "无"))

    # ---------------- ① 文档目录与首次创建 ----------------
    root = theme_packs.themes_root()
    chk("主题根目录在【文档】下且名为 Yuhub",
        os.path.basename(root) == "Yuhub",
        root)

    created, ydir = theme_packs.ensure_builtin_theme()
    chk("YuUI 主题目录已就绪", os.path.isdir(ydir), ydir)
    chk("YuUI/theme.json 存在",
        os.path.isfile(os.path.join(ydir, theme_packs.FILE_PALETTE)), ydir)
    chk("自制主题说明存在",
        os.path.isfile(os.path.join(root, "如何自制主题.txt")), root)

    # ---------------- ② 扫描 ----------------
    packs = theme.reload_packs()
    names = theme.pack_names()
    chk("扫描到默认主题 YuUI", "YuUI" in packs, str(names))
    chk("YuUI 排在列表最前", bool(names) and names[0] == "YuUI",
        str(names))

    pk = packs.get("YuUI")
    chk("YuUI 同时有 dark 与 light 色板",
        pk is not None and "dark" in pk.palette and "light" in pk.palette,
        "")

    # ---------------- ③ 色板与默认一致（视觉零变化） ----------------
    if pk is not None:
        d_bad = [k for k, v in theme_packs.DEFAULT_DARK.items()
                 if pk.palette["dark"].get(k) != v]
        l_bad = [k for k, v in theme_packs.DEFAULT_LIGHT.items()
                 if pk.palette["light"].get(k) != v]
        chk("YuUI 深色色板 == 内置默认", not d_bad, "差异键=%s" % d_bad)
        chk("YuUI 浅色色板 == 内置默认", not l_bad, "差异键=%s" % l_bad)
        chk("深色底足够深（近黑）", pk.palette["dark"]["window_top"] == "#0b0b0e",
            pk.palette["dark"]["window_top"])

    # ---------------- ④ QSS 生成 ----------------
    theme.set_pack("YuUI")
    theme.set_theme("dark")
    qss = theme.build_qss()
    chk("QSS 非空", len(qss) > 2000, "%d 字符" % len(qss))
    leftover = [t for t in ("$accent", "$text", "$card_top", "$border",
                            "$window_top", "$surface_hover") if t in qss]
    chk("没有未替换的 $ 占位符", not leftover, "残留=%s" % leftover)
    chk("QSS 含当前 accent",
        theme.current()["accent"].lower() in qss.lower(),
        theme.current()["accent"])

    # ---------------- ⑤ 新建 / 改色 / 切换 ----------------
    tname = "__theme_selftest_%d" % os.getpid()
    ok, msg = theme_packs.create_theme_from_template(tname, pk)
    chk("另存为新主题成功", ok, msg)
    tdir = theme_packs.theme_dir(tname)
    try:
        import json as _json
        tj = os.path.join(tdir, theme_packs.FILE_PALETTE)
        data = _json.load(open(tj, encoding="utf-8"))
        data["dark"] = {"accent": "#ff6600"}
        _json.dump(data, open(tj, "w", encoding="utf-8"), ensure_ascii=False)

        theme.reload_packs()
        p2 = theme.packs().get(tname)
        chk("新主题被扫到", p2 is not None, tname)
        if p2 is not None:
            chk("自定义 accent 生效",
                p2.palette["dark"]["accent"] == "#ff6600",
                p2.palette["dark"]["accent"])
            chk("未写的键继承默认",
                p2.palette["dark"]["window_top"]
                == theme_packs.DEFAULT_DARK["window_top"],
                p2.palette["dark"]["window_top"])

            theme.set_pack(tname)
            theme.set_theme("dark")
            q2 = theme.build_qss()
            chk("切换后 QSS 反映新 accent", "#ff6600" in q2.lower(), "")
            chk("current_pack_name 正确", theme.current_pack_name() == tname,
                theme.current_pack_name())
    finally:
        shutil.rmtree(tdir, ignore_errors=True)

    # ---------------- ⑥ 坏主题不致命 ----------------
    bdir = theme_packs.theme_dir("__bad_theme_%d" % os.getpid())
    os.makedirs(bdir, exist_ok=True)
    try:
        with open(os.path.join(bdir, theme_packs.FILE_PALETTE),
                  "w", encoding="utf-8") as f:
            f.write("{ 这不是合法 json")
        theme.reload_packs()
        chk("损坏的主题文件被跳过（不崩溃）",
            "bad_theme" not in " ".join(theme.pack_names()), "")
    finally:
        shutil.rmtree(bdir, ignore_errors=True)

    # ---------------- ⑦ 删除后降级 ----------------
    theme.reload_packs()
    theme.set_pack(tname)                     # 可能已不存在 → 应无害
    chk("切到不存在的主题不报错", theme.current_pack_name() in theme.packs(),
        theme.current_pack_name())
    chk("落回一个真实存在的主题",
        theme.current_pack_name() in theme.packs(),
        theme.current_pack_name())

    theme.set_pack("YuUI")
    theme.set_theme("dark")
    result["info"]["final_pack"] = theme.current_pack_name()
    result["info"]["all_packs"] = theme.pack_names()
    result["info"]["qss_len"] = len(theme.build_qss())

    # ---------------- ⑨ 窗口圆角：玻璃不许在角上凸出去 ----------------
    # 背景（血泪）：Qt 的 QSS `border-radius` **不会裁剪子控件**。标题栏 /
    # 侧栏都是矩形控件、又贴着窗口边角，于是会把外壳的圆角"补方"——在窗口
    # 四角露出一圈直角。深色主题下这圈是亮灰色玻璃压在近黑底上，特别扎眼，
    # 用户报的「软件边角凸出来的角」就是它。
    # 这组检查分两层：先验裁剪函数本身对，再验两个玻璃面板确实传了 corners。
    try:
        from PySide6.QtCore import QPoint, QRect, QRectF, QSize, Qt
        from PySide6.QtGui import QImage, QPainter, QPixmap, QRegion
        from PySide6.QtWidgets import QLabel, QWidget

        from ui import glass
        from ui.main_window import (
            LIQUID_TINT_ALPHA, PAGE_ENTER_DY, MainWindow, _PageHost)

        # (1) rounded_path：只圆指定的角
        path = glass.rounded_path(QRectF(0, 0, 20, 20), 6, ("tl", "tr"))
        chk("rounded_path 把指定的角圆掉（左上角外点不在路径内）",
            not path.contains(QPoint(1, 1)), str(path.boundingRect()))
        chk("rounded_path 未指定的角保持直角（左下角内点在路径内）",
            path.contains(QPoint(1, 19)))
        chk("rounded_path 半径收窄到不超过半边长",
            glass.rounded_path(QRectF(0, 0, 10, 10), 99,
                               ("tl",)).boundingRect().width() == 10)

        # (2) 半径解析
        chk("window_radius 解析 '8px'",
            glass.window_radius({"window_radius": "8px"}) == 8)
        chk("window_radius 解析 '14px'（sky glass）",
            glass.window_radius({"window_radius": "14px"}) == 14)
        chk("window_radius 遇到非法值回退默认",
            glass.window_radius({"window_radius": "abc"}, 8) == 8)

        # (3) 裁剪真的生效：画一块 60x60 的玻璃，只圆左上/右上
        class _Fake:
            def rect(self):
                return QRect(0, 0, 60, 60)

            def size(self):
                return QSize(60, 60)

        pm = QPixmap(60, 60)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        glass.paint_glass_panel(p, _Fake(), None, glass.BackdropSource(),
                                fallback="#ffffff", corners=("tl", "tr"),
                                corner_radius=10, edge=False)
        p.end()
        out = pm.toImage()

        def _plain_corner_alpha():
            """不传 corners 时的左上角透明度。用作对照：证明上面那个 0
            确实来自裁剪，而不是"压根没画出来"这种假通过。"""
            q = QPixmap(60, 60)
            q.fill(Qt.transparent)
            pp = QPainter(q)
            glass.paint_glass_panel(pp, _Fake(), None, glass.BackdropSource(),
                                    fallback="#ffffff", edge=False)
            pp.end()
            return q.toImage().pixelColor(1, 1).alpha()

        chk("传 corners 后，圆角外的像素是透明的（左上）",
            out.pixelColor(1, 1).alpha() == 0,
            out.pixelColor(1, 1).alpha())
        chk("传 corners 后，圆角外的像素是透明的（右上）",
            out.pixelColor(58, 1).alpha() == 0,
            out.pixelColor(58, 1).alpha())
        chk("没圆的角仍然是实的（左下）", out.pixelColor(1, 58).alpha() > 0)
        chk("不传 corners 时四角都是实的（旧行为，对照用）",
            _plain_corner_alpha() > 0, _plain_corner_alpha())

        # (4) 两个玻璃面板确实把自己的外角交给了裁剪
        win = MainWindow()
        win.resize(860, 620)
        win.show()
        pump(500)
        theme.set_theme("dark")
        pump(400)
        cur = theme.current()
        r = glass.window_radius(cur)
        from ui.main_window import _panel_corner_radius
        chk("面板圆角半径 = 主题半径 - 1（贴在外壳 1px 边框里）",
            _panel_corner_radius(win, cur) == max(0, r - 1),
            _panel_corner_radius(win, cur))

        calls = []
        orig = glass.paint_glass_panel

        def spy(painter, widget, *a, **kw):
            calls.append((widget, tuple(kw.get("corners") or ()),
                          kw.get("corner_radius")))
            return None

        glass.paint_glass_panel = spy
        try:
            win.titlebar.grab()
            win.sidebar.grab()
        finally:
            glass.paint_glass_panel = orig

        tb = [c for c in calls if c[0] is win.titlebar]
        sb = [c for c in calls if c[0] is win.sidebar]
        chk("标题栏玻璃传了 corners=('tl','tr')",
            bool(tb) and tb[0][1] == ("tl", "tr"), tb[:1])
        chk("侧栏玻璃传了 corners=('bl',)",
            bool(sb) and sb[0][1] == ("bl",), sb[:1])
        chk("两个玻璃面板都拿到了正的裁剪半径",
            bool(tb) and bool(sb) and tb[0][2] > 0 and sb[0][2] > 0,
            (tb[:1], sb[:1]))

        # ---------------- ⑩ 取景坐标：别再踩 mapTo 的坑 ----------------
        # QWidget::mapTo(target) 要求 target 是**祖先**；BackdropLayer 是兄弟
        # 子树，Qt 这时直接返回**全局坐标**（实测偏 (7,7) = 外壳留白 6 + 边框 1），
        # 于是每块玻璃取的都是"往右下挪了 7px"的纹理，贴着下边缘的侧栏还会
        # 取到背板之外。改成全局坐标相减之后，这两个值必须正好是 (0,0) 和
        # (0, 标题栏高)。
        chk("标题栏取景坐标 = (0,0)（不是 mapTo 返回的全局坐标）",
            glass.widget_origin(win.titlebar, win.backdrop) == QPoint(0, 0),
            glass.widget_origin(win.titlebar, win.backdrop))
        chk("侧栏取景坐标 y = 标题栏高度",
            glass.widget_origin(win.sidebar, win.backdrop)
            == QPoint(0, win.titlebar.height()),
            glass.widget_origin(win.sidebar, win.backdrop))

        # ---------------- ⑪ 开关形状：椭圆轨道 + 圆形滑块 ----------------
        # 用户要求「所有的功能开关都改成椭圆形和圆形的」。形状断言挂在控件
        # 自己暴露的几何方法上，再用离屏渲染交叉验证像素——只测方法会被
        # "方法对但 paintEvent 另写一套常量"骗过，只测像素又说不清哪儿错。
        from ui.widgets import ToggleSwitch

        sw = ToggleSwitch(False)
        sw.resize(42, 24)
        chk("轨道圆角 = 高度的一半（两端半圆 → 椭圆）",
            abs(sw.track_radius() - sw.height() / 2.0) < 0.01,
            sw.track_radius())
        kr = sw.knob_rect(0.0)
        chk("滑块外接框是正方形（正圆的前提）",
            abs(kr.width() - kr.height()) < 0.01, (kr.width(), kr.height()))
        chk("滑块圆角 = 外接框的一半（正圆）",
            abs(sw.knob_radius() - kr.width() / 2.0) < 0.01)
        chk("关态滑块贴左内缘", abs(kr.left() - sw.KNOB_MARGIN) < 0.01,
            kr.left())
        chk("开态滑块贴右内缘",
            abs(sw.knob_rect(1.0).right() - (sw.width() - sw.KNOB_MARGIN)) < 0.01,
            sw.knob_rect(1.0).right())

        # paintEvent 必须真的用这两个方法（防"方法对、画的是另一套"）
        spot = {"track": 0, "knob": 0}
        _otr, _okr = ToggleSwitch.track_radius, ToggleSwitch.knob_rect

        def _spy_tr(self):
            spot["track"] += 1
            return _otr(self)

        def _spy_kr(self, offset=None):
            spot["knob"] += 1
            return _okr(self, offset)

        def _render(w):
            """把控件单独渲染到透明底图上（不含父控件）。

            三个坑，全是实测踩出来的：
            1) `render(painter)` 必须同时给 targetOffset —— PySide6 的签名里
               它是必填的，漏了就 TypeError；
            2) painter 一定 try/finally 收尾 —— 中途抛异常的话 QImage 会在
               "仍被 painter 占用"的状态下析构，Qt 直接 qFatal 崩掉进程
               （QPaintDevice: Cannot destroy paint device that is being
               painted），整份自检结果全丢；
            3) **不要带 DrawWindowBackground**（那是默认值）——它会把调色板
               的窗口底色先铺满整块，圆角外那几个像素就永远是 255，
               "四角透明"这种断言直接失明。只留 DrawChildren。
            """
            img = QImage(w.width(), w.height(),
                         QImage.Format_ARGB32_Premultiplied)
            img.fill(Qt.transparent)
            p = QPainter(img)
            try:
                w.render(p, QPoint(0, 0), QRegion(),
                         QWidget.RenderFlag.DrawChildren)
            finally:
                p.end()
            return img

        sw._offset = 0.0
        sw.update()
        ToggleSwitch.track_radius, ToggleSwitch.knob_rect = _spy_tr, _spy_kr
        try:
            img = _render(sw)
        finally:
            ToggleSwitch.track_radius, ToggleSwitch.knob_rect = _otr, _okr
        chk("paintEvent 用的是 track_radius() / knob_rect()（不是另写常量）",
            spot["track"] > 0 and spot["knob"] > 0, spot)

        chk("椭圆轨道：左上角外像素透明（小圆角方块这里会是实的）",
            img.pixelColor(2, 2).alpha() == 0, img.pixelColor(2, 2).alpha())
        chk("椭圆轨道：左端尖点是实的（左端是半圆不是直角）",
            img.pixelColor(1, img.height() // 2).alpha() > 0,
            img.pixelColor(1, img.height() // 2).alpha())
        chk("轨道顶部中点是实的（对照：证明不是整块没画）",
            img.pixelColor(img.width() // 2, 1).alpha() > 0,
            img.pixelColor(img.width() // 2, 1).alpha())

        # 滑块是圆还是方，不能看 alpha —— 滑块底下是**实心轨道**，
        # 外接框的角上一定有颜色。要看那儿的颜色是轨道色还是滑块白。
        kc = img.pixelColor(int(kr.x()) + 1, int(kr.y()) + 1)
        cc = img.pixelColor(int(kr.center().x()), int(kr.center().y()))
        chk("圆形滑块：外接框左上角是轨道色（方角这里会是白的）",
            min(kc.red(), kc.green(), kc.blue()) < 235, kc.getRgb())
        chk("圆形滑块：圆心是滑块色（近白）",
            min(cc.red(), cc.green(), cc.blue()) > 200, cc.getRgb())

        # ---------------- ⑫ 选中玻璃片 / 版本号贴片的四角 ----------------
        # 血泪：玻璃片的背景快照是**整块矩形** drawPixmap 上去的，不裁自己的
        # 圆角的话，圆角外那四个小三角露的是"没染色的裸背板"——用户报的
        # 「选中特效蓝色四角周围没有毛玻璃」就是这个。这里直接渲染控件本身
        # （不含父控件），四角必须透明、中心必须实。
        win.switch_page("memory")
        pump(500)
        pill = win._nav_pill
        chk("选中指示条已显示且有尺寸",
            pill.isVisible() and pill.width() > 8 and pill.height() > 8,
            (pill.width(), pill.height()))
        pimg = _render(pill)
        corners = ((0, 0), (pimg.width() - 1, 0),
                   (0, pimg.height() - 1),
                   (pimg.width() - 1, pimg.height() - 1))
        chk("选中玻璃片四角透明（不再露出裸背板的直角）",
            all(pimg.pixelColor(x, y).alpha() == 0 for x, y in corners),
            [pimg.pixelColor(x, y).alpha() for x, y in corners])
        chk("选中玻璃片中心是实的（对照）",
            pimg.pixelColor(pimg.width() // 2,
                            pimg.height() // 2).alpha() > 0)

        # ---- 选中片"加粗"（v0.9.2beta，用户反馈：蓝色选中效果太细了） ----
        btn = win._nav_buttons["memory"]
        chk("选中片高度几乎撑满整行（上下各只内缩 1px，不再是一片薄条）",
            pill.height() >= btn.height() - 2,
            (pill.height(), btn.height()))
        # ---- 选中片改成「浅蓝液态玻璃」（v0.15beta） ----
        # 用户要求「选中的特效不要蓝色，可以要带有浅蓝色的液态玻璃的效果」。
        # 所以左缘那条实心 accent 色条**撤掉**了 —— 改为由"上/左内侧光带 +
        # 左缘青色散"承担方向提示。这里断言玻璃本身：主体是浅蓝、上缘有光带、
        # 左缘比右缘更偏青（色散），而不是"某条竖线是不是蓝的"。
        def _plum(c):
            return 0.2126 * c.redF() + 0.7152 * c.greenF() + 0.0722 * c.blueF()

        mid = pimg.height() // 2
        body = pimg.pixelColor(pimg.width() // 2, mid)
        chk("选中片主体是浅蓝（B 通道最高且够亮）",
            body.blue() > body.red() and body.blue() > body.green()
            and body.blue() > 170, body.getRgb())
        top_px = pimg.pixelColor(pimg.width() // 2, 3)
        chk("选中片内侧有顶部光带（上缘比中段亮）",
            _plum(top_px) > _plum(body), (_plum(top_px), _plum(body)))
        lpx = pimg.pixelColor(2, mid)
        rpx = pimg.pixelColor(pimg.width() - 3, mid)
        chk("左缘比右缘更偏青（液态玻璃的色散：左青右品红）",
            (lpx.blue() - lpx.red()) > (rpx.blue() - rpx.red()),
            (lpx.getRgb(), rpx.getRgb()))
        # 对照组：主体色**不是**原来的实心 accent 蓝（否则说明"改了个寂寞"）。
        # 判据用"通道差之和"而不是"某个通道更大"：玻璃是 accent 往白里提、
        # 再被深色侧栏稀释的结果 —— 蓝通道反而**低于**纯 accent（255 → 208），
        # 拿"B 更大"当判据会写成一条永远失败的死断言。
        acc = glass.qcolor(theme.current().get("accent"), "#3b82f6")
        delta = (abs(body.red() - acc.red()) + abs(body.green() - acc.green())
                 + abs(body.blue() - acc.blue()))
        chk("对照组：主体色已明显不同于实心 accent 蓝（通道差之和 > 30）",
            delta > 30, "Δ=%d (%s vs %s)" % (delta, body.name(), acc.name()))

        # ---- 版本号：**不能再有那个多出来的框**（v0.9.2beta） ----
        # v0.10beta 给它单独画了一块玻璃贴片（自己的底色 + 描边 + 投影），
        # 用户反馈「不想要顶部版本号有一个额外的框」。现在它就是普通 QLabel：
        # 不画任何底色，直接透出标题栏本身的毛玻璃。
        ver = getattr(win.titlebar, "version_label", None)
        chk("标题栏版本号是普通 QLabel（不再单独做玻璃贴片）",
            ver is not None and type(ver) is QLabel, type(ver))
        if ver is not None:
            vimg = _render(ver)
            chk("版本号控件四角透明（没有独立底色）",
                all(vimg.pixelColor(x, y).alpha() == 0
                    for x, y in ((0, 0), (vimg.width() - 1, 0),
                                 (0, vimg.height() - 1),
                                 (vimg.width() - 1, vimg.height() - 1))),
                (vimg.width(), vimg.height()))
            # 对照：四角透明也可能是"整块没画"这种假通过，所以再抽上下两条
            # 贴边的像素带——描边 / 投影都画在这儿，必须一个点都没有。
            edge_a = [vimg.pixelColor(x, 0).alpha()
                      for x in range(vimg.width())]
            edge_a += [vimg.pixelColor(x, vimg.height() - 1).alpha()
                       for x in range(vimg.width())]
            # 有文字（中间几行）作对照，证明控件确实渲染了内容
            mid_a = [vimg.pixelColor(x, vimg.height() // 2).alpha()
                     for x in range(vimg.width())]
            chk("版本号上下边缘一个像素都不画（连描边/投影都没有）",
                max(edge_a) == 0, max(edge_a))
            chk("版本号中间那行有文字像素（对照：不是整块空壳）",
                max(mid_a) > 0, max(mid_a))

        # QSS 里绝不能再给贴片刷背景：QLabel::paintEvent 一进来就 drawFrame()，
        # 会把底色画在自绘玻璃**之上**，玻璃感全没（sky glass 的
        # surface_hover 还是 rgba(255,255,255,225)，几乎全白）。
        blob_bad = []
        for nm, txt in _qss_templates():
            i = txt.find("QLabel#AppSubtitle")
            if i < 0:
                continue
            blk = txt[i:txt.find("}", i) + 1]
            if "background: transparent" not in blk:
                blob_bad.append(nm)
        chk("所有主题模板的版本号贴片都是透明背景", not blob_bad, blob_bad)

        chk_bad = []
        for nm, txt in _qss_templates():
            i = txt.find("QPushButton#SidebarButton:checked")
            if i < 0:
                continue
            blk = txt[i:txt.find("}", i) + 1]
            if "background: transparent" not in blk:
                chk_bad.append(nm)
        chk("所有主题模板的侧栏选中态都不自己刷底色（交给玻璃片）",
            not chk_bad, chk_bad)

        # ---------------- ⑬ 老磁盘模板的补丁升级 ----------------
        # 主题包首次创建时会把内置模板写进 Documents\Yuhub\<包>\theme.qss，
        # 之后**绝不覆盖**（保护用户改动）。老用户手里那份是旧模板，能把新
        # 代码里的修正整份顶掉——这个升级函数就是为此存在的，必须验：
        # 原样未改的会被升级，用户改过的绝不动。
        import tempfile

        tmp = os.path.join(tempfile.gettempdir(), "yuhub_qss_patch_test.qss")
        old_txt = theme_packs._SKY_QSS_PATCHES[0][0]
        new_bits = theme_packs._SKY_QSS_PATCHES[0][1]
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write("/* header */\n" + old_txt + "\n")
            did = theme_packs.upgrade_builtin_qss(tmp)
            with open(tmp, "r", encoding="utf-8") as f:
                after = f.read()
            chk("旧模板（未被用户改过）会被升级",
                did and new_bits in after and old_txt not in after)
            chk("升级后的文件带版本戳", theme_packs.QSS_STAMP in after)
            chk("升级是幂等的（第二次不再写）",
                theme_packs.upgrade_builtin_qss(tmp) is False)

            with open(tmp, "w", encoding="utf-8") as f:
                f.write("/* 我自己改的主题 */\nQLabel#AppSubtitle { color: red; }\n")
            chk("用户改过的模板不会被动",
                theme_packs.upgrade_builtin_qss(tmp) is False
                and "color: red" in open(tmp, encoding="utf-8").read())
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass

        # ---------------- ⑭ 启动画面的毛玻璃背景（v0.9.2beta） ----------------
        # 用户要求「启动软件的动画背景也改出毛玻璃特效」。启动屏是独立的顶层
        # 窗口、**没有 BackdropLayer 可以抓**，所以它自己合成底纹再糊一次。
        # 这里必须验三件事：底纹真的生成了、背景真的不是纯色、圆角真的裁了
        # （不然就是"改回纯色但没人发现"）。
        from ui.splash import SplashScreen

        sp = SplashScreen(min_duration_ms=100000)
        try:
            bg = sp._backdrop_pixmap()
            chk("启动画面生成了模糊底纹（尺寸与窗口一致）",
                bg is not None and not bg.isNull()
                and bg.width() == sp.width() and bg.height() == sp.height(),
                None if bg is None else (bg.width(), bg.height()))
            simg = _render(sp)
            samples = {simg.pixelColor(x, y).rgb()
                       for x in range(20, simg.width() - 20, 24)
                       for y in range(20, simg.height() - 20, 24)}
            chk("启动画面背景不是纯色（底纹糊开后仍有明暗/色相变化）",
                len(samples) > 3, len(samples))
            chk("启动画面四角透明（圆角玻璃片，不是一块方板）",
                all(simg.pixelColor(x, y).alpha() == 0
                    for x, y in ((0, 0), (simg.width() - 1, 0),
                                 (0, simg.height() - 1),
                                 (simg.width() - 1, simg.height() - 1))),
                [simg.pixelColor(x, y).alpha()
                 for x, y in ((0, 0), (simg.width() - 1, 0),
                              (0, simg.height() - 1),
                              (simg.width() - 1, simg.height() - 1))])
            chk("启动画面玻璃面板本体是实的（对照）",
                simg.pixelColor(simg.width() // 2, 30).alpha() > 0,
                simg.pixelColor(simg.width() // 2, 30).alpha())
        finally:
            try:
                sp._timer.stop()
                sp.close()
                sp.deleteLater()
            except RuntimeError:
                pass
            pump(200)

        # ---------------- ⑮ 动效令牌与交互态（v0.14beta） ----------------
        # 依据：Emil Kowalski 的设计工程技能库 <github.com/emilkowalski/skills>。
        # 这一组守的全是"看不见的地方"：曲线是不是自定义的、时长有没有超预算、
        # 退场有没有比进场快、按下与键盘焦点有没有反馈。任何一条悄悄退回默认值
        # 都不会报错，只会让界面"说不出的不跟手"。
        from PySide6.QtCore import QEasingCurve
        from ui import motion

        co = motion.curve(motion.CURVE_OUT)
        chk("强 ease-out 是自定义贝塞尔（不是内置枚举）",
            co.type() == QEasingCurve.BezierSpline, co.type().name)
        chk("强 ease-out 的控制点就是技能库给的那一组",
            motion.curve_points(motion.CURVE_OUT) == (0.23, 1.0, 0.32, 1.0),
            motion.curve_points(motion.CURVE_OUT))
        # 对照组：内置 OutCubic 在同一点明显更慢；否则"换曲线"这件事没有任何效果
        v_mine = co.valueForProgress(0.2)
        v_builtin = QEasingCurve(QEasingCurve.OutCubic).valueForProgress(0.2)
        v_ease_in = QEasingCurve(QEasingCurve.InCubic).valueForProgress(0.2)
        chk("起步比内置 OutCubic 更快（t=0.2 处至少快 0.1）",
            v_mine > v_builtin + 0.1,
            "自定义=%.3f 内置OutCubic=%.3f" % (v_mine, v_builtin))
        chk("起步远快于内置 ease-in（UI 里明令禁用的那条）",
            v_mine > v_ease_in + 0.4,
            "自定义=%.3f 内置InCubic=%.3f" % (v_mine, v_ease_in))
        # 每条曲线的收尾控制点都要足够高 —— 低了就退化成"内置那批软曲线"
        soft = [n for n in motion.curve_names()
                if not motion.is_spring(n)
                and max(motion.curve_points(n)[1], motion.curve_points(n)[3]) < 0.7]
        chk("非弹簧曲线都有决断的收尾（控制点不低于 0.7）", not soft, soft)
        chk("曲线互不相同（单段 4 条 + 弹簧 2 条）",
            len({motion.curve_points(n) for n in motion.curve_names()})
            == len(motion.curve_names()),
            motion.curve_names())

        over = [k for k in ("PRESS", "HOVER", "TOOLTIP", "POPOVER", "DROPDOWN",
                            "DIALOG_IN", "DIALOG_OUT", "TOAST_IN", "TOAST_OUT",
                            "TOAST_STACK", "NAV", "PILL", "SWITCH", "SIDEBAR")
                if not 0 < motion.budget(k) <= motion.UI_BUDGET]
        chk("所有交互动效都在 300ms 预算内", not over, over)
        chk("提示：退场比进场快", motion.TOAST_OUT < motion.TOAST_IN,
            "%d < %d" % (motion.TOAST_OUT, motion.TOAST_IN))
        chk("弹窗：退场比进场快", motion.DIALOG_OUT < motion.DIALOG_IN,
            "%d < %d" % (motion.DIALOG_OUT, motion.DIALOG_IN))

        # 动效强度固定为「完整」（v0.15beta）。
        # 用户明确要求"不需要可以调节动画程度，一直完整就行"，所以设置页那
        # 三档已经撤掉、reduced() 恒定返回 False、dur() 不再折算。这里守两件
        # 事：强度查询口子还在（不是被顺手删干净导致别处 AttributeError），
        # 以及设置页确实没有入口了。
        chk("动效强度恒定：reduced() 永远为假", motion.reduced() is False)
        chk("动效强度恒定：dur() 原样返回时长（不再折算）",
            motion.dur(260, "move") == 260 and motion.dur(260, "fade") == 260,
            (motion.dur(260, "move"), motion.dur(260, "fade")))
        chk("强度调节的 API 已整组移除",
            not any(hasattr(motion, k) for k in
                    ("set_setting", "setting", "MOTION_REDUCED",
                     "MOTION_SYSTEM", "sys_animations_on", "reset_cache")),
            [k for k in ("set_setting", "setting", "MOTION_REDUCED",
                         "MOTION_SYSTEM", "sys_animations_on", "reset_cache")
             if hasattr(motion, k)])
        _sp = win._pages.get("settings")
        chk("设置页已没有「界面动效」入口",
            _sp is not None and not hasattr(_sp, "motion_segment")
            and not hasattr(_sp, "motion_note"), "")
        chk("设置页也删掉了对应的处理函数",
            _sp is not None and not hasattr(_sp, "_on_motion_changed")
            and not hasattr(_sp, "_update_motion_note"), "")

        # ---- QSS：按下态 / 键盘焦点环，两份内置模板都必须有 ----
        # 为什么要"两份都查"：内置模板与 sky glass 主题包自带一份 theme.qss，
        # 后者会**整份替换**前者。历史上侧栏选中态、版本号贴片都出现过
        # "内置那份补了、主题包那份忘了"的漏改。
        # 只查**确认是 Yuhub 写的那份**（带版本戳）：用户自己改过的 theme.qss
        # 本来就允许跟内置模板不一样，不该判失败。
        need_pressed = ("QPushButton#GhostButton:pressed",
                        "QPushButton#DangerButton:pressed",
                        "QPushButton#MiniButton:pressed",
                        "QPushButton#SegmentButton:pressed:!checked",
                        "QPushButton#SidebarButton:pressed:!checked",
                        'QFrame#Card[clickable="true"]:pressed',
                        "QCheckBox::indicator:pressed")
        need_focus = ("QPushButton#SidebarButton:focus",
                      "QPushButton#SegmentButton:focus",
                      "QCheckBox:focus::indicator")
        miss_p, miss_f, checked_tpl = {}, {}, []
        for nm, txt in _qss_templates():
            if nm != "builtin" and theme_packs.QSS_STAMP not in txt:
                continue                    # 用户改过的，跳过
            checked_tpl.append(nm)
            lack = [s for s in need_pressed if s not in txt]
            if lack:
                miss_p[nm] = lack
            lack = [s for s in need_focus if s not in txt]
            if lack:
                miss_f[nm] = lack
        chk("查到了至少两份内置模板（内置 + 主题包）",
            len(checked_tpl) >= 2, checked_tpl)
        chk("所有内置模板都补齐了按下态", not miss_p, miss_p)
        chk("所有内置模板都有键盘焦点环", not miss_f, miss_f)
        _expect_pressed = theme._pressed_shade(theme.current()["accent"],
                                               theme.get_theme())
        chk("按下色由 build_qss 现算注入（模板里不留占位、也不写死颜色）",
            "$accent_pressed" not in theme.build_qss()
            and _expect_pressed.lower() in theme.build_qss().lower(),
            "期望 QSS 里含 %s" % _expect_pressed)

        # 大字负字距（字号越大字面越显得散）
        no_track = [nm for nm, txt in _qss_templates()
                    if "QLabel#PageTitle" in txt
                    and "letter-spacing" not in
                    txt[txt.find("QLabel#PageTitle"):
                        txt.find("}", txt.find("QLabel#PageTitle")) + 1]]
        chk("所有模板的页面标题都带负字距", not no_track, no_track)

        # ---- 焦点环不许挤动布局 ----
        # 侧栏项 / 分段按钮原来写的是 `border: none`：焦点环一描边就会把控件
        # 撑大 2px，连带挤动整列邻居。现在基础规则改成"1px 透明边框占位 +
        # 内边距各减 1px"，总尺寸不变。
        #
        # ⚠️ 这里**刻意不做运行时尺寸断言**。实测过三种量法，全都不可靠：
        #   * `sizeFromContents(CT_PushButton, opt, ...)`：QStyleSheetStyle 匹配
        #     伪类时看的是控件自己的实时状态，根本不读 `opt.state` 里的
        #     State_HasFocus —— 连"把焦点边框加粗到 8px"的对照组都量不出差异，
        #     属于测量方法本身失灵；
        #   * `sizeHint()` 焦点前后：Qt 不会因为焦点变化让 sizeHint 失效，
        #     返回的是**缓存值**，看起来"相等"其实是假通过；
        #   * 放进真实布局量 geometry：Qt 也不会因为焦点变化触发重新布局。
        # 所以判据改成查"保证机制"本身：焦点规则只准改颜色，基础规则必须
        # 已经留好同宽的透明边框。这两条同时成立，尺寸就不可能变。
        def _qss_block(txt, head):
            """取 `head { ... }` 这对花括号里的内容。取不到返回 None。"""
            i = txt.find(head)
            if i < 0:
                return None
            j = txt.find("{", i)
            k = txt.find("}", j)
            return txt[j + 1:k] if 0 <= j < k else None

        def _declared_props(block):
            """块里声明了哪些属性（丢掉值，只看属性名）。"""
            if not block:
                return set()
            out = set()
            for line in block.splitlines():
                line = line.strip()
                if not line.startswith("/*") and ":" in line:
                    out.add(line.split(":")[0].strip())
            return out

        bad_border, bad_focus = [], []
        for nm, txt in _qss_templates():
            if nm != "builtin" and theme_packs.QSS_STAMP not in txt:
                continue
            for head in ("QPushButton#SegmentButton {",
                         "QPushButton#SidebarButton {",
                         "QPushButton#TitleButton {"):
                blk = _qss_block(txt, head)
                if blk is None:
                    continue
                line = [x.strip() for x in blk.splitlines()
                        if x.strip().startswith("border:")]
                if not line:
                    bad_border.append((nm, head, "没有 border 声明"))
                elif not line[0].split(":", 1)[1].strip().startswith("1px"):
                    bad_border.append((nm, head, line[0]))
            for head in ("QPushButton#PrimaryButton:focus,",
                         "QCheckBox:focus::indicator {"):
                blk = _qss_block(txt, head)
                if blk is None:
                    continue
                props = _declared_props(blk)
                if props - {"border-color"}:
                    bad_focus.append((nm, head, sorted(props)))
        chk("焦点环能生效的前提：基础规则已留好 1px 边框（不是 border: none）",
            not bad_border, bad_border)
        chk("焦点规则只改边框颜色（改宽度/内边距就会挤动布局）",
            not bad_focus, bad_focus)

        # ---- 全窗按钮都是"只吃 Tab 焦点"，等价 CSS 的 :focus-visible ----
        from PySide6.QtWidgets import QPushButton as _QPB
        all_btns = win.findChildren(_QPB)
        not_tab = [b.objectName() or b.text() for b in all_btns
                   if b.focusPolicy() != Qt.TabFocus]
        chk("全窗按钮都设成 Qt.TabFocus（鼠标点完不留焦点环）",
            bool(all_btns) and not not_tab,
            "共 %d 个，异常 %d 个 %s" % (len(all_btns), len(not_tab),
                                        not_tab[:5]))

        # ---- 页面淡入：装了就必须摘掉 ----
        # QGraphicsOpacityEffect 会让整棵子树每帧先渲染到离屏缓冲，**装上去就
        # 一直在收费**（实测联机页每帧 +6.6ms，页面临界）。动画结束必须摘。
        win.switch_page("home")
        pump(250)
        seen_fade = []
        _orig_fe = motion.fade_effect

        def _spy_fe(w):
            seen_fade.append(w)
            return _orig_fe(w)

        motion.fade_effect = _spy_fe
        try:
            win.switch_page("share")
            pump(350)
        finally:
            motion.fade_effect = _orig_fe
        chk("切页时确实给新页面装了淡入（不是没生效）",
            win._pages["share"] in seen_fade, len(seen_fade))
        chk("淡入结束后不透明度特效已被摘掉（不常驻扣渲染开销）",
            win._pages["share"].graphicsEffect() is None,
            win._pages["share"].graphicsEffect())
        # 对照组：不摘的话就会一直挂着 —— 证明上面那个 None 不是"本来就没有"
        _eff = motion.fade_effect(win._pages["share"])
        chk("对照组：装上特效后确实拿得到（说明上面测的是同一条路径）",
            win._pages["share"].graphicsEffect() is _eff, "")
        win._pages["share"].setGraphicsEffect(None)

        # ---------------- ⑯ 焦点环真的看得见 + 模板补丁幂等（v0.14beta） -------
        # 上一组已经把"焦点规则只改颜色、基础规则留了 1px 边框"钉死了 ——
        # 但那只保证**不会挤动布局**，不保证**看得见**。实测抓到过一个反例：
        # 主要按钮的常态边框**就是** $accent，于是"只把边框改成 accent"的
        # 焦点环在它身上 Δ=0，键盘用户在这个按钮上完全看不出焦点。
        # 所以这里补两条：环色必须与填充色反着来；解析后的 QSS 里那条规则
        # 必须真的落到一个不同于常态的颜色上。
        chk("焦点环色跟填充色反着来：暗填充配亮环",
            theme._luminance(theme._ring_color("#3b82f6"))
            > theme._luminance("#3b82f6"),
            "%s -> %s" % ("#3b82f6", theme._ring_color("#3b82f6")))
        chk("焦点环色跟填充色反着来：亮填充配暗环",
            theme._luminance(theme._ring_color("#f5e663"))
            < theme._luminance("#f5e663"),
            "%s -> %s" % ("#f5e663", theme._ring_color("#f5e663")))
        # 解析不了的颜色（渐变、关键字）必须返回 None 而不是瞎猜一个值
        chk("解析不了的颜色不硬猜（返回 None，调用方回退）",
            theme._ring_color("qlineargradient(x1:0,y1:0,x2:1,y2:0)") is None
            and theme._ring_color(None) is None)

        ring_missing, ring_same, pressed_dup = {}, {}, {}
        for md in ("dark", "light"):
            theme.set_theme(md)
            q = theme.build_qss()
            acc = theme.current()["accent"]
            ring = theme._ring_color(acc)
            if "$accent_ring" in q:
                ring_missing[md] = "占位符没被替换"
            if ring and ring.lower() not in q.lower():
                ring_missing[md] = "QSS 里找不到推导出的环色 %s" % ring
            if ring and ring.lower() == acc.lower():
                ring_same[md] = "环色与填充色相同 %s" % ring
            # 主要按钮的按下只能有一条规则：历史上模板里还留着一句
            # `:pressed { background: $accent }`（= 常态色），和末尾那条
            # 同优先级、只靠"后写的赢"才没出事 —— 规则一重排就静默失效。
            n = q.count("QPushButton#PrimaryButton:pressed")
            if n != 1:
                pressed_dup[md] = "%d 条" % n
            else:
                blk = _qss_block(q, "QPushButton#PrimaryButton:pressed")
                want = theme._pressed_shade(acc, md)
                if not blk or want.lower() not in blk.lower():
                    pressed_dup[md] = "颜色不是推导出的 %s：%r" % (want, blk)
        theme.set_theme(theme.get_theme())
        chk("两份模式都注入了 accent_ring（且与填充色不同）",
            not ring_missing and not ring_same, (ring_missing, ring_same))
        chk("主要按钮的按下规则唯一、且颜色是现算的加深档",
            not pressed_dup, pressed_dup)

        # ---- 新模板常量自己不能还留着「待补丁修复的旧写法」 ----
        # 补丁是给**老盘文件**用的。如果新模板里还留着旧写法，就会变成
        # "新装用户和升级上来的用户行为不一样"，这类差异极难被发现。
        builtin_tpl = getattr(theme._QSS, "template", theme._QSS)
        stale = []
        for _old in ("QLabel#AppSubtitle {\n    color: $text_dim;\n"
                     "    font-size: 11px;\n    background: $surface_hover;",
                     "QPushButton#SidebarButton:checked {\n"
                     "    background: $accent_soft;\n    color: $accent;\n"
                     "    border: 1px solid $accent;",
                     "QPushButton#PrimaryButton:pressed { background: $accent; }",
                     "QPushButton#SegmentButton {\n    background: transparent;\n"
                     "    border: none;",
                     "QLabel#PageTitle { font-size: 21px; font-weight: 800; "
                     "color: $text; }",
                     "QLabel#DialogTitle { color: $text; font-size: 17px; "
                     "font-weight: 800; background: transparent; }"):
            if _old in str(theme_packs.SKY_QSS) or _old in str(builtin_tpl):
                stale.append(_old.split("\n")[0][:44])
        chk("新模板里不留旧写法（否则新装与升级上来的行为不一致）",
            not stale, stale)
        chk("两份新模板都自带主要按钮焦点环",
            theme_packs._RING_MARK in str(theme_packs.SKY_QSS)
            and theme_packs._RING_MARK in str(builtin_tpl), "")
        chk("幂等标记选的是交互态块里一条历次稳定的规则",
            theme_packs._INTERACTION_MARK in theme_packs.INTERACTION_QSS)

        # ---- 补丁表必须自带幂等标记 ----
        # STAMP 一升版本，整张表会对老盘文件重跑。增量型补丁（"把结尾换成
        # 结尾 + 新规则"）重跑一次就会把新规则**追加第二遍** —— 不报错、
        # 界面照常，只是文件里躺着两份一样的规则。
        #
        # 这里的判据：凡是"新写法里还含着旧写法"或"新旧相同"的，就是增量型，
        # 必须给一个**跨版本稳定**的显式标记 —— 默认取"新写法"本身不行，
        # 因为新写法一变（哪怕只多一行注释）就认不出来了。
        ambiguous = []
        for item in theme_packs._SKY_QSS_PATCHES:
            old = item[0]
            marker = item[2] if len(item) > 2 else item[1]
            incremental = (old in item[1] and item[1] != old)
            if incremental and marker == item[1]:
                ambiguous.append(old.split("\n")[0][:40])
        chk("增量型补丁都给了跨版本稳定的显式标记",
            not ambiguous, ambiguous)
        chk("补丁都是「旧写法 / 新写法」或「旧写法 / 新写法 / 幂等标记」",
            all(len(it) in (2, 3) for it in theme_packs._SKY_QSS_PATCHES),
            [len(it) for it in theme_packs._SKY_QSS_PATCHES])

        # ---- 端到端幂等：把新模板「倒推」成老盘形态，再升两遍 ----
        # 不依赖 git / .git —— 打包后的 exe 里两者都没有，靠 git 取真实旧模板
        # 会直接把冻结态自检搞失败。改用逆向补丁造一份等价的老盘文件：
        # 只用到本模块内的补丁表，源码态和冻结态表现完全一致。
        tmpd = None
        try:
            def _to_old(text):
                """把当前模板逆向还原成"打补丁之前"的样子。

                **必须按补丁表的正序撤**：补丁 (3) 把整份交互态块追加到模板
                末尾，(7) 又在块尾追加焦点环 —— 倒序撤的话会先摘掉焦点环，
                于是 (3) 的锚点（"结尾块 + 完整的交互态块"）就对不上了，
                整块留在原地，看起来像"逆向还原没生效"。
                """
                for item in theme_packs._SKY_QSS_PATCHES:
                    old, new = item[0], item[1]
                    if new == "":
                        # 纯删除型：把被删掉的那段插回原来的位置
                        anchor = "QPushButton#PrimaryButton:disabled {"
                        if anchor in text and old not in text:
                            text = text.replace(anchor, old + anchor, 1)
                    elif new in text:
                        text = text.replace(new, old)
                return text

            old_body = _to_old(str(theme_packs.SKY_QSS))
            fake_old = "/* yuhub builtin theme.qss v2 */\n" + old_body

            tmpd = tempfile.mkdtemp(prefix="yuhub_tpl_")
            p_tpl = os.path.join(tmpd, "theme.qss")
            with open(p_tpl, "w", encoding="utf-8") as f:
                f.write(fake_old)
            first = theme_packs.upgrade_builtin_qss(p_tpl)
            with open(p_tpl, "r", encoding="utf-8") as f:
                t1 = f.read()
            n1_mark, n1_ring = (t1.count(theme_packs._INTERACTION_MARK),
                                t1.count(theme_packs._RING_MARK))
            # 去掉版本戳逼补丁表再跑一遍（模拟"STAMP 又升了一版"）
            body = t1.split("\n", 1)[1] if t1.startswith("/* yuhub") else t1
            with open(p_tpl, "w", encoding="utf-8") as f:
                f.write(body)
            theme_packs.upgrade_builtin_qss(p_tpl)
            with open(p_tpl, "r", encoding="utf-8") as f:
                t2 = f.read()
            n2_mark, n2_ring = (t2.count(theme_packs._INTERACTION_MARK),
                                t2.count(theme_packs._RING_MARK))
        except Exception as exc:      # noqa: BLE001
            first, n1_mark, n1_ring, n2_mark, n2_ring = False, -1, -1, -1, -1
            chk("模板补丁端到端能跑起来", False, repr(exc))
        finally:
            if tmpd:
                shutil.rmtree(tmpd, ignore_errors=True)

        chk("对照组：伪造的老盘文件里确实没有交互态块与焦点环",
            fake_old.count(theme_packs._INTERACTION_MARK) == 0
            and theme_packs._RING_MARK not in fake_old, "")
        chk("老盘模板能升到当前版本（交互态块与焦点环各补上一份）",
            bool(first) and n1_mark == 1 and n1_ring == 1,
            "交互态块 %d 份 / 焦点环 %d 份" % (n1_mark, n1_ring))
        chk("补丁表重跑一遍不会把新规则追加第二遍（幂等）",
            n2_mark == 1 and n2_ring == 1,
            "第二遍后 交互态块 %d 份 / 焦点环 %d 份" % (n2_mark, n2_ring))

        # ---------------- ⑰ 液态玻璃与果冻动效（v0.15beta） ----------------
        # 用户这一轮提了三件事：
        #   ①「不需要可以调节动画程度，一直完整就行」→ 强度固定、入口撤掉；
        #   ②「功能切换要有 Q 弹果冻感」→ 弹簧曲线 + 开关滑块拉伸；
        #   ③「选中的特效不要蓝色，要浅蓝液态玻璃」→ 选中态整体换材质。
        # 三条都是"改完看起来对了、之后某次重构又会悄悄回去"的类型，所以逐条
        # 钉住。第 ① 条在 ⑮ 组已经守了，这里守 ② ③。
        from PySide6.QtGui import QColor as _QC
        from PySide6.QtCore import Qt as _Qt
        from ui.widgets import SegmentedControl

        # ---- ② 弹簧曲线：必须"冲过目标再弹回"，不是换名字的 ease-out ----
        for _nm in (motion.CURVE_SPRING, motion.CURVE_SPRING_SOFT):
            _c = motion.curve(_nm)
            _v = [_c.valueForProgress(i / 200) for i in range(201)]
            _pk = max(_v)
            _ipk = _v.index(_pk)
            _tr = min(_v[_ipk:])
            chk("弹簧曲线 %s 起点/终点是 0/1" % _nm,
                abs(_v[0]) < 1e-6 and abs(_v[-1] - 1.0) < 1e-6, (_v[0], _v[-1]))
            chk("弹簧曲线 %s 真的过冲（峰值 > 1.03）" % _nm, _pk > 1.03, _pk)
            chk("弹簧曲线 %s 过冲后回落（峰后最低 < 1）" % _nm, _tr < 1.0, _tr)
            chk("弹簧曲线 %s 是多段贝塞尔（单段做不出回弹）" % _nm,
                len(motion.curve_segments(_nm)) >= 2,
                len(motion.curve_segments(_nm)))
            chk("弹簧曲线 %s 有一动不动的那种单调段吗（不该有）" % _nm,
                not all(_v[i] <= _v[i + 1] + 1e-9 for i in range(len(_v) - 1)))
        chk("两条弹簧形状不同（果冻 16% / 轻弹 8% 各司其职）",
            motion.curve_segments(motion.CURVE_SPRING)
            != motion.curve_segments(motion.CURVE_SPRING_SOFT))
        chk("弹簧曲线的时长都在 300ms 预算内",
            max(motion.PILL, motion.SWITCH, motion.PAGE_IN) <= motion.UI_BUDGET,
            (motion.PILL, motion.SWITCH, motion.PAGE_IN))
        # 对照组：普通 ease-out 不满足"过冲"，证明上面测的不是废话
        _ov = [motion.curve(motion.CURVE_OUT).valueForProgress(i / 200)
               for i in range(201)]
        chk("对照组：普通 ease-out 峰值就是 1（没有过冲）",
            abs(max(_ov) - 1.0) < 1e-6, max(_ov))

        # ---- ③ 液态玻璃色组：浅蓝 + 文字对比度过 AA 线 ----
        # 最容易翻的就是对比度：浅蓝底很亮，白字压上去只剩 1.9:1。所以这里
        # 用**实际呈色**（tint 以 LIQUID_TINT_ALPHA 压在侧栏底上）来量。
        _saved_pack = theme.get_pack().name
        _saved_theme_name = theme.get_theme()

        def _contrast(c1, c2):
            a, b = theme._luminance(c1), theme._luminance(c2)
            if a is None or b is None:
                return 0.0
            hi, lo = max(a, b), min(a, b)
            return (hi + 0.05) / (lo + 0.05)

        def _blend(fg, alpha, bg):
            k = alpha / 255.0
            f, b = glass.qcolor(fg), glass.qcolor(bg)
            return _QC(int(f.red() * k + b.red() * (1 - k)),
                       int(f.green() * k + b.green() * (1 - k)),
                       int(f.blue() * k + b.blue() * (1 - k)))

        theme.set_pack("YuUI")
        try:
            for _md in ("dark", "light"):
                theme.set_theme(_md)
                _liq = theme.liquid_palette()
                _tint = glass.qcolor(_liq["tint"])
                chk("[%s] 液态玻璃底色是浅蓝（B 通道最高）" % _md,
                    _tint.blue() > _tint.red() and _tint.blue() > _tint.green(),
                    _tint.getRgb())
                chk("[%s] 液态玻璃底色够浅（亮度 > 0.45）" % _md,
                    theme._luminance(_liq["tint"]) > 0.45,
                    theme._luminance(_liq["tint"]))
                chk("[%s] 玻璃上的文字是深色（亮度 < 0.25）" % _md,
                    theme._luminance(_liq["text"]) < 0.25,
                    theme._luminance(_liq["text"]))
                _over = _blend(_liq["tint"], LIQUID_TINT_ALPHA,
                               theme.current().get("sidebar_bg", "#12141a"))
                # 注意传 `.name()`：theme._luminance 走的是 _parse_rgb，它只认
                # 字符串（#rrggbb / rgba(...)）。直接塞 QColor 会静默返回 None，
                # 对比度算出来恒为 0 —— 这条断言就变成永远失败的假警报。
                chk("[%s] 玻璃实际呈色上文字对比度 ≥ 4.5:1（AA 正文线）" % _md,
                    _contrast(_over.name(), _liq["text"]) >= 4.5,
                    "呈色 %s 对比度 %.2f:1"
                    % (_over.name(), _contrast(_over.name(), _liq["text"])))
                # 对照组：白字压在浅蓝上确实不够（证明这条不是走过场）
                chk("[%s] 对照组：白字压在浅蓝玻璃上不到 3:1" % _md,
                    _contrast(_over.name(), "#ffffff") < 3.0,
                    _contrast(_over.name(), "#ffffff"))
        finally:
            theme.set_pack(_saved_pack)
            theme.set_theme(_saved_theme_name)

        # ---- ③ 两份模板的选中态都得是液态玻璃 ----
        _old_seg = ("QPushButton#SegmentButton:checked { background: $accent; "
                    "color: $accent_text; font-weight: 700; }")
        chk("新模板里没有遗留的「实心 accent 选中段」",
            _old_seg not in str(theme_packs.SKY_QSS)
            and _old_seg not in str(builtin_tpl), "")
        # v0.15.1：选中段的底**整体撤掉**了 —— 改由 ui/widgets.SegmentSlider
        # 自绘一块会滑动的液态玻璃。这里留着 qss 的底色就会把片整个盖住。
        _seg_new = ("QPushButton#SegmentButton:checked {\n"
                    "    background: transparent;\n"
                    "    color: $liquid_text;")
        chk("两份新模板的选中段都不画底（玻璃改由 SegmentSlider 自绘）",
            all(_seg_new in t
                for t in (str(theme_packs.SKY_QSS), str(builtin_tpl))), "")
        chk("两份新模板的侧栏选中文字都改成深色（不再是 $accent_text）",
            all("color: $liquid_text;" in t
                for t in (str(theme_packs.SKY_QSS), str(builtin_tpl))), "")

        # ---- ② 侧栏指示条：弹簧 + 液态玻璃 ----
        _p2 = win._nav_pill
        _pc = _p2._anim.easingCurve()
        chk("侧栏指示条滑动用弹簧曲线（过冲 > 1.05）",
            max(_pc.valueForProgress(i / 200) for i in range(201)) > 1.05,
            max(_pc.valueForProgress(i / 200) for i in range(201)))
        chk("侧栏指示条时长取令牌 PILL",
            _p2._anim.duration() == motion.PILL, _p2._anim.duration())

        # ---- ②b 侧栏按钮 hover 反馈：自绘（v0.15.4beta） ----
        # 为什么必须自绘：QSS 的 `:hover` 在这类按钮上**不生效** ——
        # `QStyleOptionButton.state & State_MouseOver` 实测恒为 False（鼠标
        # 确实在按钮上、underMouse() 为 True、WA_Hover 也为 True），真机截屏
        # hover 前后逐像素零差异 —— 也就是用户报的「左侧这几个功能鼠标移上去
        # 没有动画」。所以反馈改由 NavButton 自己画一层浅蓝液态玻璃膜 + 弹簧
        # 铺开。下面几条把它钉死（并顺带守住"别再退回 qss"）。
        from PySide6.QtCore import QVariantAnimation

        from ui.main_window import NavButton

        _nb = NavButton("memory", "🧠", "自检项")
        _nb.resize(216, 39)
        _nb.show()
        pump(80)

        def _px_diff(a, b):
            """两张图的平均通道差（0~255）。"""
            if a.size() != b.size():
                return -1.0
            tot = 0
            n = 0
            for _y in range(0, a.height(), 2):
                for _x in range(0, a.width(), 2):
                    _ca, _cb = a.pixelColor(_x, _y), b.pixelColor(_x, _y)
                    tot += (abs(_ca.red() - _cb.red())
                            + abs(_ca.green() - _cb.green())
                            + abs(_ca.blue() - _cb.blue()))
                    n += 3
            return tot / max(1, n)

        chk("侧栏按钮的 hover 反馈走自绘（挂着 hover 动画对象）",
            isinstance(getattr(_nb, "_hover_anim", None), QVariantAnimation),
            type(getattr(_nb, "_hover_anim", None)))
        chk("侧栏 hover 动画时长取令牌 HOVER",
            _nb._hover_anim.duration() == motion.HOVER,
            _nb._hover_anim.duration())
        _hc = _nb._hover_anim.easingCurve()
        _hpeak = max(_hc.valueForProgress(_i / 200.0) for _i in range(201))
        chk("侧栏 hover 用弹簧曲线（过冲 > 1.05，Q 弹果冻感）",
            _hpeak > 1.05, round(_hpeak, 3))

        # 0 → 1 必须经过中间值：一帧都不停就是"瞬变"，不算动画
        _nb._hover_t = 0.0
        _nb._animate_hover(1.0)
        _mids = []
        for _i in range(24):
            pump(6)
            _v = _nb._hover_t
            if 0.02 < _v < 1.02:
                _mids.append(round(_v, 3))
        chk("侧栏 hover 是滑过去的（有中间帧，不是瞬变）",
            len(_mids) >= 2, _mids[:6])
        pump(400)
        chk("侧栏 hover 动画最终停在 1.0",
            abs(_nb._hover_t - 1.0) < 0.02, _nb._hover_t)

        # 视觉证据：hover 前后按钮图像**必须**变。
        # 这一条能成立恰恰是因为自绘 —— QWidget.grab() 会临时剥掉
        # WA_UnderMouse（qss 的 :hover 永远抓不到），而自绘看的是 _hover_t。
        _nb._hover_t = 0.0
        _nb.update()
        _img_off = _nb.grab().toImage()
        _nb._hover_t = 1.0
        _nb.update()
        _d_on = _px_diff(_img_off, _nb.grab().toImage())
        chk("侧栏 hover 时按钮外观确实变了（自绘膜的像素证据）",
            _d_on > 3.0, round(_d_on, 2))

        # 选中项不叠膜：那一行已经有液态玻璃选中片，再加一层是同色加重。
        # 对照必须**同为选中态**、只差 hover —— 拿"未选中"当基准会把
        # 选中片自身的变化也算进来（第一版就是这么错判成 122 的）。
        _nb.setChecked(True)
        _nb._hover_t = 0.0
        _nb.update()
        _img_chk_off = _nb.grab().toImage()
        _nb._hover_t = 1.0
        _nb.update()
        _img_chk_on = _nb.grab().toImage()
        _d_checked = _px_diff(_img_chk_off, _img_chk_on)
        chk("选中项 hover 不再叠膜（不会盖住选中片）",
            _d_checked < 1.0, round(_d_checked, 2))
        # 说明：这条不会靠"压根不画"假通过 —— 上面「外观确实变了（_d_on > 3）」
        # 就是它的对照；两处测的是同一个按钮的同一个绘制分支。
        _nb.setChecked(False)

        # 膜在任何进度下都不得冒出按钮（弹簧过冲到 1.16 时最宽）
        _out = []
        for _t in (0.0, 0.25, 0.5, 0.75, 1.0, 1.16):
            _r = _nb._hover_rect(_t)
            if _r is not None and not QRectF(_nb.rect()).contains(_r):
                _out.append((_t, round(_r.x(), 2), round(_r.width(), 2)))
        chk("hover 膜在任何进度下都在按钮内（含 16% 过冲）", not _out, _out[:3])

        _nb._animate_hover(0.0)
        pump(400)
        chk("鼠标移开后 hover 归零", _nb._hover_t < 0.02, _nb._hover_t)
        _nb.hide()
        _nb.deleteLater()
        pump(30)

        # ---- ② 开关滑块：正圆 → 拉伸成胶囊 → 按下放大 ----
        _sw = ToggleSwitch(False)
        _sw.resize(42, 24)
        _flat = _sw.knob_rect(offset=0.0, stretch=0.0)
        chk("静止时滑块仍是正圆（形状断言不因拉伸而失效）",
            abs(_flat.width() - _flat.height()) < 0.01,
            (_flat.width(), _flat.height()))
        _base = _sw.knob_rect(offset=0.5, stretch=0.0)
        _sw._stretch_dir = 1.0
        _st = _sw.knob_rect(offset=0.5, stretch=1.0)
        chk("移动中滑块被拉长成胶囊（果冻感的主要来源）",
            _st.width() > _st.height() * 1.3, (_st.width(), _st.height()))
        # 拉伸必须是**以圆心为中心对称**的（等效参考实现的 scaleX）。
        # 第一版写成单侧拖尾（向右走 `x -= extra`），起步那帧左缘直接到
        # −5.4（滑块半径才 9），被控件边界一刀削平 —— 用户报的
        # 「白色圆圈会变成正方形」就是它。下面两条守着这个根因。
        chk("拉伸以圆心为中心对称（不是单侧拖尾）",
            abs((_st.left() + _st.right()) / 2.0
                - (_base.left() + _base.right()) / 2.0) < 0.01,
            (_st.left(), _base.left(), _st.right(), _base.right()))
        _bleed = []
        for _i in range(0, 21):
            for _s in (0.0, 0.5, 1.0):
                _r = _sw.knob_rect(offset=_i / 20.0, stretch=_s)
                if _r.left() < -0.01 or _r.right() > _sw.width() + 0.01:
                    _bleed.append((_i / 20.0, _s, round(_r.left(), 2),
                                   round(_r.right(), 2)))
        chk("整条行程 × 各种拉伸量都不戳出控件（「白圆变方块」的根因）",
            not _bleed, _bleed[:3])
        _edge = _sw.knob_rect(offset=0.0, stretch=1.0)
        chk("顶到端点时拉伸自动收窄（不会撞出边界）",
            _edge.width() < _st.width() - 1
            and _edge.left() > -0.01
            and _edge.right() < _sw.width() + 0.01,
            (_edge.width(), _st.width()))
        # 按下放大要围绕中心、且不能高过控件（否则上下也会被裁）
        _sw._press = 1.0
        _pbig = _sw.knob_rect(offset=0.5, stretch=0.0)
        chk("按下放大后仍整个落在控件内",
            _pbig.top() > -0.01 and _pbig.bottom() < _sw.height() + 0.01,
            (_pbig.top(), _pbig.bottom(), _sw.height()))
        _sw._press = 0.0
        _sw._press = 1.0
        _pres = _sw.knob_rect(offset=1.0, stretch=0.0)
        chk("按下时滑块放大（对应参考实现的 pressedScale）",
            _pres.height() > _flat.height() * 1.1,
            (_flat.height(), _pres.height()))
        _sw._press = 0.0
        # 弹簧会让 offset 越过 1 —— 绘制必须夹住，否则滑块跑出轨道
        chk("弹簧过冲被夹在轨道内（offset=1.3 也不越界）",
            _sw.knob_rect(offset=1.3, stretch=0.0).right()
            <= _sw.width() - _sw.KNOB_MARGIN + 0.01,
            _sw.knob_rect(offset=1.3, stretch=0.0).right())
        _sc = _sw._anim.easingCurve()
        chk("开关滑动用弹簧曲线（过冲 > 1.05）",
            max(_sc.valueForProgress(i / 200) for i in range(201)) > 1.05,
            max(_sc.valueForProgress(i / 200) for i in range(201)))
        chk("开关滑动时长取令牌 SWITCH",
            _sw._anim.duration() == motion.SWITCH, _sw._anim.duration())
        chk("按下反馈接了动画而不是瞬变",
            _sw._press_anim.duration() > 0 and _sw._press_anim.duration()
            <= motion.UI_BUDGET, _sw._press_anim.duration())

        # ---- ④ 分段控件的滑动选中片（v0.15.1） ----
        # 用户反馈「所有切换功能的地方都没有加入动画，例如内存优化的定时
        # 自动优化那里」。这些地方原来是 qss 的 `:checked` 瞬时换色，
        # 现在统一换成一块会滑的浅蓝液态玻璃（SegmentSlider）。
        _segc = SegmentedControl([("a", "甲"), ("b", "乙"), ("c", "丙")],
                                 current="a")
        _segc.resize(300, 34)
        _sld = getattr(_segc, "slider", None)
        chk("SegmentedControl 装上了滑动选中片", _sld is not None, "")
        if _sld is not None:
            _kids = _segc.children()
            chk("选中片是背景层（压在所有按钮之下，否则盖住文字）",
                bool(_kids) and _kids[0] is _sld,
                [type(c).__name__ for c in _kids][:3])
            chk("选中片不抢鼠标（否则整块分段控件都点不动）",
                _sld.testAttribute(_Qt.WA_TransparentForMouseEvents), "")
            _sc2 = _sld._anim.easingCurve()
            chk("选中片滑动用弹簧曲线（过冲 > 1.03）",
                max(_sc2.valueForProgress(i / 200)
                    for i in range(201)) > 1.03, "")
            chk("选中片时长取令牌 PAGE_IN",
                _sld._anim.duration() == motion.PAGE_IN, _sld._anim.duration())
            # 未跑过 layout 时容器与按钮**都是** 640x480 的默认几何 ——
            # 第一版认了它，片一上来就落成一个巨框。这条守着那个判据。
            _pre = _sld._target_rect()
            chk("未布局时不会把按钮的 640x480 默认几何当成目标",
                _pre is None, _pre)
            _segc.resize(300, 34)
            _segc.show()
            pump(60)
            _tr_ = _sld._target_rect()
            _ba = _segc._buttons["a"].geometry()
            chk("布局完成后选中片精确盖住选中按钮",
                _tr_ is not None
                and abs(_tr_.x() - _ba.x()) < 1.5
                and abs(_tr_.width() - _ba.width()) < 1.5,
                (_tr_, _ba))
            _segc.hide()
        _segc.deleteLater()

        # 内存页那两个挡位选择器（用户点名的「定时自动优化」）
        try:
            from ui.pages.memory_page import (
                IntervalPicker as _IP, ThresholdPicker as _TP)
            _ip = _IP(30)
            _tp = _TP(80)
            chk("内存页的定时挡位（IntervalPicker）也有滑动片",
                getattr(_ip, "slider", None) is not None, "")
            chk("内存页的过载阈值（ThresholdPicker）也有滑动片",
                getattr(_tp, "slider", None) is not None, "")
            chk("挡位选择器的片跟踪到了本组全部按钮",
                getattr(_ip, "slider", None) is not None
                and len(_ip.slider._buttons) == len(_ip._buttons) + 1,
                (len(getattr(getattr(_ip, "slider", None),
                             "_buttons", [])), len(_ip._buttons) + 1))

            # 用户反馈（v0.15.2）：「定时自动优化里选中自定义时必须双击才
            # 出现切换过去的动画」。根因：`QPushButton` 被点一下是**先
            # `nextCheckState()` 翻自己的 checked、再 emit `clicked`**，
            # 所以 `toggled(True)` 的瞬间旧按钮往往还没被取消 —— 只扫
            # "第一个 checked"会算到**旧**位置上，正好撞上"目标没变就不
            # 重播"的判据，动画被整个吃掉；第二次点才滑过去。
            # 下面三条守着修复：点一次就得动，而且必须动到**被点的那个**
            # 按钮上。
            _ip2 = _IP(30)
            _ip2.resize(744, 136)
            _ip2.show()
            pump(120)
            _sl2 = getattr(_ip2, "slider", None)
            if _sl2 is not None:
                _ip2._buttons[3][1].click()          # 先切到一个预设挡位
                pump(120)
                _b3 = _ip2._buttons[3][1].geometry()
                chk("点一次预设挡位就播切换动画（目标是刚被点的按钮）",
                    _sl2._to is not None
                    and abs(_sl2._to.x() - _b3.x()) < 1.5,
                    (_sl2._to, _b3))
                _ip2.btn_custom.click()              # ← 只点这一次
                _bc = _ip2.btn_custom.geometry()
                chk("点一次「自定义」就播切换动画（不必双击）",
                    _sl2._to is not None
                    and abs(_sl2._to.x() - _bc.x()) < 1.5
                    and _sl2._animating(),
                    (_sl2._to, _bc, _sl2._animating()))
                pump(400)
                chk("动画跑完后片精确落在「自定义」按钮上",
                    _sl2._from is not None and _sl2._to is not None
                    and _sl2._from == _sl2._to
                    and abs(_sl2._to.x() - _bc.x()) < 1.5,
                    (_sl2._from, _sl2._to, _bc))
            _ip2.hide()
            _ip2.deleteLater()

            _ip.deleteLater()
            _tp.deleteLater()
        except Exception as _ex:                                   # noqa: BLE001
            chk("内存页两个挡位选择器都能装上滑动片", False, repr(_ex)[:140])

        # ---- ② 切页：host 弹簧位移 + 归位 ----
        _n = win.stack.count()
        chk("stack 的直接子控件全是 _PageHost（页面的位移才动得了）",
            _n > 0 and all(isinstance(win.stack.widget(i), _PageHost)
                           for i in range(_n)), _n)
        chk("_pages 里仍然直接存页面本身（不是 host）",
            not isinstance(win._pages["home"], _PageHost))
        chk("host.page 就是 _pages 里那个对象（没有多包一层）",
            win._hosts["home"].page is win._pages["home"])

        win.switch_page("lan")
        pump(90)
        _sl = getattr(win, "_page_slide", None)
        chk("切页时确实起了 host 位移动画", _sl is not None)
        if _sl is not None:
            _sv = [_sl.easingCurve().valueForProgress(i / 200)
                   for i in range(201)]
            chk("切页位移用轻弹曲线（有过冲但比果冻温和）",
                max(_sv) > 1.02, max(_sv))
            chk("切页位移起点是 PAGE_ENTER_DY",
                _sl.startValue().y() == PAGE_ENTER_DY, _sl.startValue())
            chk("切页位移终点是 (0,0)（必须归位）",
                _sl.endValue().y() == 0, _sl.endValue())
        pump(500)
        _off = {k: (h.x(), h.y()) for k, h in win._hosts.items()
                if (h.x(), h.y()) != (0, 0)}
        chk("切页动画结束后所有 host 都归位到 (0,0)（否则页面会莫名偏下）",
            not _off, _off)
        chk("切页动画结束后页面特效已摘掉",
            win._pages["lan"].graphicsEffect() is None,
            win._pages["lan"].graphicsEffect())

        win.close()

    except Exception as exc:      # noqa: BLE001 —— 跑不起来本身就算失败
        chk("窗口圆角回归检查能跑起来", False, repr(exc))

    result["ok"] = all(c["pass"] for c in checks)
    # 先 dumps 再落盘 + default=str：detail 里混进 bytes / 自定义对象时，
    # json.dump 会抛 TypeError，而它是**边序列化边写**的——文件已被写了
    # 半截，最后拿到的是一份读不出来的残档，前面的检查结果全丢
    #（v0.8.14beta 的 lan 自检就这么坏过一次）。
    try:
        with open(out_file, "w", encoding="utf-8") as f:
            f.write(json.dumps(result, ensure_ascii=False, indent=2,
                               default=str))
    except (OSError, TypeError, ValueError):
        return 4
    return 0 if result["ok"] else 1
