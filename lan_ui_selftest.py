# -*- coding: utf-8 -*-
"""打包后自检：联机页三个 UI/数据链路回归（v1.3.1）。

用法：`Yuhub.exe --lan-ui-selftest <结果json路径>`

钉住用户 2026-11-06 报的三件事：
  ① 在线成员出现「两个我」——残留旧 wintun 网卡的 IP 被当成另一个成员；
  ② C盘清理后联机组件缺失——清理器删掉进程自己的 `_MEIPASS` 内置资源；
  ③ 临时云盘分享长文件名把右侧按钮挤出可视区。

全部是**真调用** + 打桩隔离副作用，不做自洽比较。无 destructive 动作：
只建临时目录、构造离线页面与控件，不改 hosts、不进房间、不起子进程
（`run_framework` / `_resource_dir` 都被打桩）。
"""
import json
import os
import sys
import traceback


def run(out_file):
    try:
        return _run(out_file)
    except Exception as exc:
        _dump({"ok": False,
               "checks": [{"name": "★ 自检自身未崩溃", "pass": False,
                           "detail": "%r\n%s" % (exc, traceback.format_exc())}],
               "runtime": {}}, out_file)
        return 5


def _run(out_file):
    root = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, root)
    checks = []

    def ck(name, ok, detail=""):
        checks.append({"name": name, "pass": bool(ok), "detail": str(detail)})

    from PySide6.QtWidgets import QApplication, QLabel
    app = QApplication.instance() or QApplication([])

    import etier
    import ui.pages.accel_page as ap
    import ui.pages.lan_page as lp
    from ui.pages.lan_page import _ElidedLabel

    # ============ ① 在线成员不能出现「两个我」 ============
    orig_vips = etier.virtual_adapter_ips
    etier.virtual_adapter_ips = lambda: ["10.126.126.3", "10.126.126.2"]
    try:
        own = etier._own_virtual_ips("10.126.126.3")
        ck("★ _own_virtual_ips 收齐本机全部虚拟地址",
           own == {"10.126.126.3", "10.126.126.2"}, own)

        tracker = etier.MemberTracker.__new__(etier.MemberTracker)
        tracker._my_ip = "10.126.126.3"
        tracker._alive_hints = [
            lambda: ["10.126.126.1", "10.126.126.2", "10.126.126.3"]]
        hints = tracker._hint_ips(own)
        ck("★ 到场线索剔掉旧 .2 与当前 .3、保留队友 .1",
           hints == {"10.126.126.1"}, hints)

        page = lp.LanPage()
        page._my_ip = "10.126.126.3"
        page._host_nick = "BRDXSM"
        members = [
            {"ip": "10.126.126.1", "name": "LAPTOP-SS6S82DH",
             "game": "Minecraft 服务器", "port": 25565, "ss": 0,
             "relayed": True},
            {"ip": "10.126.126.2", "name": "BRDXSM", "game": "",
             "port": 0, "ss": 0, "relayed": True},       # 旧的自己
            {"ip": "10.126.126.9", "name": "BRDXSM", "game": "",
             "port": 0, "ss": 0, "relayed": False},      # 同名真队友
        ]

        class FakeTracker:
            def snapshot(self):
                return members

        page._tracker = FakeTracker()
        page._members_row = []
        page._multi_ip_hinted = False
        page._update_ip_inner()
        ips = [r["ip"] for r in page._members_row]
        ck("★ 旧 .2 不再渲染成第二个我", "10.126.126.2" not in ips, ips)
        ck("★ 恰好同名的真队友没被按名字误删", "10.126.126.9" in ips, ips)
        ck("★ 第一行仍是「我」", page._members_row[0].get("self") is True, ips)
        ck("★ 人数标签与实际行数一致",
           page.members_count.text() == "%d 人" % len(page._members_row),
           page.members_count.text())

        # ============ ③ 分享长文件名不遮挡按钮 ============
        long_name = "超级无敌长的测试文件名" * 18 + "_v20261106_final.zip"
        lab = _ElidedLabel(long_name)
        lab.setToolTip(long_name)
        lab.resize(140, 20)
        lab.show()                     # Qt 只在 show 之后派发 resizeEvent
        app.processEvents()
        ck("★ 长文件名被按可用宽度省略", len(lab.text()) < len(long_name),
           "%d → %d 字符" % (len(long_name), len(lab.text())))
        ck("★ 省略号可见", "…" in lab.text() or "..." in lab.text(),
           lab.text()[:60])
        ck("★ tooltip 保留全名", lab.toolTip() == long_name)
        ck("★ minimumSizeHint 不撑宽（外层 SetMinimumSize 也不会出横向滚动条）",
           lab.minimumSizeHint().width() == 0,
           lab.minimumSizeHint().width())
        plain = QLabel(long_name)
        ck("★ 对照：普通 QLabel 确实会被撑宽（证明根因不是装饰性断言）",
           plain.minimumSizeHint().width() > 600,
           "%d px" % plain.minimumSizeHint().width())

        entry = {"key": "k1", "name": long_name, "mine": True,
                 "owner": "我", "size": 1234567, "problem": False}
        row = page._make_share_row(entry)
        row.resize(320, 36)
        app.processEvents()
        btn = page._share_widgets["k1"]["btn"]
        ck("★ 分享行右侧按钮仍在且可见",
           btn is not None and btn.width() >= 56,
           "btn w=%d" % (btn.width() if btn else -1))
        ck("★ 分享行最小宽没被长文件名撑爆",
           row.minimumSizeHint().width() <= 480,
           "%d px" % row.minimumSizeHint().width())
    finally:
        etier.virtual_adapter_ips = orig_vips

    # ============ ② 清理后组件缺失的兜底 ============
    old_res = etier._resource_dir
    try:
        etier._resource_dir = lambda: ""      # 模拟 _MEIPASS 被清理器删掉
        ok, msg = etier.release_binaries()
        ck("★ 落地目录完好时 release_binaries 不报缺失", ok is True, msg)
        ok2, msg2 = etier.release_binaries(force=True)
        ck("★ force=True 仍不短路（强制覆盖语义保留）", ok2 is False, msg2)
    finally:
        etier._resource_dir = old_res

    old_emb, old_run = ap._embedded_framework_path, ap.run_framework
    try:
        ap._embedded_framework_path = lambda: None
        ap.run_framework = lambda args, timeout=10: (0, "ok", "")
        ok3, msg3 = ap.install_framework()
        ck("★ 内置源缺失但落点已就位时不误报『请重新安装 Yuhub』",
           ok3 is True and "重新安装" not in msg3, msg3)
    finally:
        ap._embedded_framework_path, ap.run_framework = old_emb, old_run

    result = {"ok": all(c["pass"] for c in checks), "checks": checks,
              "runtime": {"frozen": bool(getattr(sys, "frozen", False))}}
    _dump(result, out_file)
    return 0 if result["ok"] else 1


def _dump(result, out_file):
    try:
        text = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    except (TypeError, ValueError) as exc:
        text = json.dumps({"ok": False, "checks": result.get("checks", []),
                           "dump_error": repr(exc)},
                          ensure_ascii=False, indent=2, default=str)
    try:
        with open(out_file, "w", encoding="utf-8") as fh:
            fh.write(text)
    except OSError:
        return 4
    return 0


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "lan_ui_selftest_result.json"
    sys.exit(run(out))
