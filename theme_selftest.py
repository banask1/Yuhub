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
        from ui.main_window import PILL_BAR_WIDTH, MainWindow

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
        chk("左缘色条加宽到 5px（原来是 3px 的细线）",
            PILL_BAR_WIDTH >= 5, PILL_BAR_WIDTH)
        # 色条真的画出来了：贴左缘那一列应有 accent 蓝（对照右侧同高的列）
        mid = pimg.height() // 2
        left_col = pimg.pixelColor(3, mid)
        chk("左缘色条确实是 accent 蓝（不是被裁掉了）",
            left_col.blue() - left_col.red() > 60, left_col.getRgb())

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
