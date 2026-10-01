# -*- coding: utf-8 -*-
"""打包后自检：底部提示（Toast）的堆叠行为
（`Yuhub.exe --toast-selftest <结果json路径>`）。

为什么需要它
------------
提示是**唯一**告诉用户"这个开关刚才干了什么"的反馈。它坏掉的方式很安静：
所有提示都叠在同一个坐标上，最后一条盖住前面几条 —— 界面上"有提示"，
但用户看到的永远是最后一句，快速连点两个开关时等于什么都没说。
这种故障不抛异常、不影响任何数据，只能靠断言守住。

所以这里做四件事：

  * **位置契约**：单条在底部居中；多条自下而上堆叠、间距固定、两两不相交
  * **上顶必须是动画**：新提示出现后旧的还在途中（说明是滑动而非瞬移），
    否则"顶上去"的感觉就没了
  * **同屏上限**：最多 3 条，第 4 条进来时最旧的一条直接消失且被隐藏
  * **回收正确**：淡出后从栈里移除并让剩下的补位；父窗口销毁后栈被清理
    （否则 `_TOAST_STACKS` 会一直涨，且残留的 key 会指向已死对象）

返回码：0 全部通过 / 1 有断言失败 / 2 参数错误 / 4 结果写盘失败
"""

import json
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)


def _dump(result, out_file):
    """写结果文件。

    **序列化兜底必须在这里**：detail 里一旦混进 bytes 之类的非 JSON 类型，
    `json.dumps` 会抛 TypeError；只捕 OSError 的话整个结果文件会被写成
    半截（有历史教训 —— 某个自检的结果文件曾长期只写一半，谁都看不出
    是哪一项干的）。
    """
    try:
        text = json.dumps(result, ensure_ascii=False, indent=2)
    except (TypeError, ValueError) as exc:                 # noqa: BLE001
        for c in result.get("checks", []):
            c["detail"] = str(c.get("detail", ""))[:300]
        result["checks"].append({
            "name": "结果可序列化", "pass": False,
            "detail": "详情里有无法 JSON 化的对象：%r" % (exc,)})
        result["ok"] = False
        text = json.dumps(result, ensure_ascii=False, indent=2)
    try:
        with open(out_file, "w", encoding="utf-8") as fh:
            fh.write(text)
    except OSError:
        return 4
    return 0 if result.get("ok") else 1


def run(out_file):
    checks = []

    def mark(name, ok, detail=""):
        checks.append({"name": name, "pass": bool(ok),
                       "detail": "" if detail is None else str(detail)})

    try:
        _run_all(mark)
    except Exception as exc:                              # noqa: BLE001
        mark("自检过程未抛异常", False,
             "%s: %s" % (type(exc).__name__, exc))

    result = {
        "ok": all(c["pass"] for c in checks),
        "which": "toast",
        "checks": checks,
    }
    return _dump(result, out_file)


def _run_all(mark):
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWidgets import QApplication, QFrame, QLabel

    from ui import widgets as W

    app = QApplication.instance() or QApplication([])

    def pump(sec=0.3):
        end = time.monotonic() + sec
        while time.monotonic() < end:
            app.processEvents()
            time.sleep(0.01)

    def flush():
        """让 deleteLater 真正生效 —— processEvents 默认不处理 DeferredDelete。"""
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        app.processEvents()

    HOST_W, HOST_H = 1000, 700
    FOREVER = 600000        # 除专门测淡出的用例，一律用超长时长

    def new_host():
        h = QFrame()
        h.resize(HOST_W, HOST_H)
        h.show()
        pump(0.1)
        return h

    def stack_of(host):
        return W._toast_stack(host)

    def bottom_y(h):
        return HOST_H - h.height() - W.TOAST_BOTTOM

    def overlaps(items):
        return [(i, j) for i in range(len(items)) for j in range(i + 1, len(items))
                if not items[i].geometry().intersected(items[j].geometry()).isEmpty()]

    def texts_of(items):
        out = []
        for t in sorted(items, key=lambda x: x.y()):
            lbls = t.findChildren(QLabel)
            out.append(lbls[1].text() if len(lbls) >= 2 else "?")
        return out

    # ================= A. 常量契约 =================
    mark("同屏上限就是 3 条", W.TOAST_MAX == 3, W.TOAST_MAX)
    mark("堆叠有自己的间距与底边距",
         W.TOAST_GAP > 0 and W.TOAST_BOTTOM > 0,
         "gap=%s bottom=%s" % (W.TOAST_GAP, W.TOAST_BOTTOM))

    # ================= B. 单条位置 =================
    host = new_host()
    W.show_toast(host, "第一条提示", duration=FOREVER)
    pump(0.3)
    st = stack_of(host)
    mark("弹一条后栈里恰好 1 条", len(st) == 1, len(st))
    t1 = st[0]
    mark("水平居中",
         abs(t1.x() - (HOST_W - t1.width()) // 2) <= 1,
         "x=%d w=%d" % (t1.x(), t1.width()))
    mark("贴底（距底边 %d px）" % W.TOAST_BOTTOM,
         abs(t1.y() - bottom_y(t1)) <= 1,
         "y=%d h=%d 期望=%d" % (t1.y(), t1.height(), bottom_y(t1)))

    # ================= C. 上顶：位置 + 动画 =================
    y1_before = t1.y()
    W.show_toast(host, "第二条提示", duration=FOREVER)
    st = stack_of(host)
    mark("第二条进来后栈里 2 条", len(st) == 2, len(st))
    t2 = st[0]
    mark("最新的排在栈底（下标 0）", t2 is not t1, "")

    pump(0.05)
    y1_mid = t1.y()
    mark("★ 上顶是动画而非瞬移（旧的还在途中）",
         y1_before - 45 < y1_mid <= y1_before,
         "起 %d → 途中 %d" % (y1_before, y1_mid))

    pump(0.5)
    mark("★ 旧的最终被顶上去", t1.y() < y1_before,
         "y %d → %d（上移 %d px）" % (y1_before, t1.y(), y1_before - t1.y()))
    mark("两条间距 = %d px" % W.TOAST_GAP,
         abs((t1.y() + t1.height() + W.TOAST_GAP) - t2.y()) <= 1,
         "t1 底=%d t2 顶=%d" % (t1.y() + t1.height(), t2.y()))
    mark("★ 两条矩形不相交（文字不会被盖住）",
         not overlaps([t1, t2]), "%s / %s" % (t1.geometry(), t2.geometry()))
    mark("后出现的在下面（y 更大）", t2.y() > t1.y(),
         "t2.y=%d t1.y=%d" % (t2.y(), t1.y()))

    # ================= D. 同屏上限 =================
    W.show_toast(host, "第三条提示", duration=FOREVER)
    pump(0.4)
    st = stack_of(host)
    mark("3 条时全部保留", len(st) == 3, len(st))
    keep = list(st)

    W.show_toast(host, "第四条提示", duration=FOREVER)
    pump(0.4)
    st = stack_of(host)
    mark("★ 第 4 条进来后仍然只有 3 条", len(st) == 3, len(st))
    oldest = keep[-1]
    mark("★ 最旧的那条被直接挤掉（不在栈里）", oldest not in st, "")
    mark("被挤掉的那条已经隐藏", not oldest.isVisible(), oldest.isVisible())
    mark("4 条互不重叠", not overlaps(st), overlaps(st) or "无重叠")

    W.show_toast(host, "第五条提示", duration=FOREVER)
    pump(0.4)
    st = stack_of(host)
    mark("加到第 5 条仍然只有 3 条", len(st) == 3, len(st))
    ordered = sorted(st, key=lambda t: t.y())
    gaps = [ordered[i + 1].y() - (ordered[i].y() + ordered[i].height())
            for i in range(len(ordered) - 1)]
    mark("三条自上而下等距排开（间距都是 %d px）" % W.TOAST_GAP,
         all(g == W.TOAST_GAP for g in gaps), gaps)
    mark("最下面那条仍在原位（没被新提示带偏）",
         abs(ordered[-1].y() - bottom_y(ordered[-1])) <= 1,
         "y=%d 期望=%d" % (ordered[-1].y(), bottom_y(ordered[-1])))
    mark("三条矩形互不相交", not overlaps(st), overlaps(st) or "无重叠")

    # ================= E. 回收与补位 =================
    host2 = new_host()
    W.show_toast(host2, "长命的底条", duration=FOREVER)
    pump(0.3)
    W.show_toast(host2, "短命的上条", duration=120)
    pump(0.3)
    st2 = stack_of(host2)
    mark("另一窗口的栈里有 2 条", len(st2) == 2, len(st2))
    first_created = st2[1]
    pump(1.2)
    flush()
    st2 = stack_of(host2)
    mark("★ 到期的提示已从栈中移除", len(st2) == 1, len(st2))
    mark("剩下的是先创建的那条", bool(st2) and st2[0] is first_created, "")
    mark("★ 剩下的这条补位回到底部",
         bool(st2) and abs(st2[0].y() - bottom_y(st2[0])) <= 1,
         st2[0].y() if st2 else "无")

    mark("两个窗口的栈互相独立",
         len(stack_of(host)) == 3 and len(stack_of(host2)) == 1,
         "host=%d host2=%d" % (len(stack_of(host)), len(stack_of(host2))))

    key_host, key2 = id(host), id(host2)
    host2.deleteLater()
    flush()
    pump(0.2)
    mark("★ 父窗口销毁后它的栈被清理（不会一直涨）",
         key2 not in W._TOAST_STACKS and key_host in W._TOAST_STACKS,
         "剩余 key 数=%d" % len(W._TOAST_STACKS))

    # ================= F. 真实链路：主窗口快速连发 =================
    from ui.main_window import MainWindow

    w = MainWindow()
    w.resize(1160, 800)
    w.show()
    pump(0.6)
    base = len(stack_of(w))
    mark("刚起来的窗口还没有提示", base == 0, base)

    for i in range(5):
        w.notify("第 %d 条测试提示" % (i + 1))
        pump(0.08)
    pump(0.6)
    wst = stack_of(w)
    mark("★ 快速连发后条数不超过上限", len(wst) <= W.TOAST_MAX,
         "当前 %d 条（上限 %d）" % (len(wst), W.TOAST_MAX))
    mark("★ 确实堆叠了多条（说明没被盖住）", len(wst) >= 2,
         "当前 %d 条" % len(wst))
    mark("窗口里多条提示互不重叠", not overlaps(wst), overlaps(wst) or "无重叠")
    mark("窗口宽度下仍然水平居中",
         all(abs(t.x() - (w.width() - t.width()) // 2) <= 1 for t in wst),
         [(t.x(), t.width()) for t in wst])
    mark("提示不会被顶出窗口外", all(t.y() >= 0 for t in wst),
         [t.y() for t in wst])
    order = texts_of(wst)
    mark("★ 自上而下就是创建顺序（最上面最旧、最下面最新）",
         order == ["第 3 条测试提示", "第 4 条测试提示", "第 5 条测试提示"],
         order)
    print("     当前提示文案（自上而下）：%s" % (order,))

    # 页面里的 toast() 也要走同一条链路（否则子页面的提示永远不堆叠）。
    # 注意打桩的是 page.notify —— 它是构造时传进来的 bound callback，
    # 改主窗口的 notify 是影响不到它的。
    page = w._pages["home"]
    seen = []
    real_notify = page.notify
    try:
        page.notify = lambda t: seen.append(t)
        page.toast("来自页面的提示")
    finally:
        page.notify = real_notify
    mark("页面的 toast() 会转到传入的 notify()", seen == ["来自页面的提示"],
         seen)

    # ================= G. 真实触发源：内存页的两个开关被快速连点 =================
    # 这一段会真的拨内存优化页的开关，而开关状态是存在 QSettings（真实
    # 注册表）里的 —— 用完必须还原，否则「两个自动开关默认都是关的」那条
    # 自检会被永久带坏（这个坑已经在开发期踩过一次）。
    from PySide6.QtCore import QSettings

    keys = ("mem_auto_enabled", "mem_auto_minutes", "mem_thr_enabled",
            "mem_thr_percent", "mem_admin_deep", "mem_history",
            "mem_only_when_busy")
    st = QSettings("Yuhub", "Yuhub")
    saved = {k: st.value(k, None) for k in keys}
    try:
        mp = w._pages["memory"]
        w.switch_page("memory")
        pump(0.5)
        for sw in (mp.sw_auto, mp.sw_thr):
            sw.setChecked(False)        # 归零，免得"本来就开着"不发信号
        pump(0.8)

        for i in range(5):
            mp.sw_auto.setChecked(i % 2 == 0)
            mp.sw_thr.setChecked(i % 2 == 1)
            pump(0.08)
        pump(0.6)
        cur = stack_of(w)
        mark("★ 连点两个开关时提示不超过上限", len(cur) <= W.TOAST_MAX,
             "当前 %d 条（上限 %d）" % (len(cur), W.TOAST_MAX))
        mark("★ 连点确实堆叠出多条提示", len(cur) >= 2,
             "当前 %d 条" % len(cur))
        mark("★ 页面提示与主窗口提示共用同一个栈（不会被主窗口的盖住）",
             all(t.parent() is w for t in cur), [t.parent() is w for t in cur])
        mark("★ 多条提示互不重叠", not overlaps(cur), overlaps(cur) or "无重叠")
        mark("页面提示文案是正常句子而非 JSON",
             all("{" not in t and "}" not in t for t in texts_of(cur)),
             texts_of(cur))
        print("     连点后的提示（自上而下）：%s" % (texts_of(cur),))
    finally:
        for k, v in saved.items():
            if v is None:
                st.remove(k)
            else:
                st.setValue(k, v)
        st.sync()

    w.close()
    flush()
