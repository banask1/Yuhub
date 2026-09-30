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

    result["ok"] = all(c["pass"] for c in checks)
    try:
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
    except OSError:
        return 4
    return 0 if result["ok"] else 1
