# -*- coding: utf-8 -*-
"""打包后自检：屏幕共享（局域网联机 / 异地联机两条链路共用的那条 TCP 流）。

为什么需要它
------------
屏幕共享现在只有一条链路：**自己抓屏 → JPEG → 一条 TCP 流广播出去**。
随包不带浏览器内核、不带 WebRTC、不带公网隧道，代价是这些"本来由库兜着"
的事，现在全得自己保证不出错：

  1. 帧格式必须严格自洽——头 17 字节、心跳零负载。错一位，观看端要么
     卡死要么把整块内存当成画面；
  2. 观看码是**唯一的**准入门槛（虚拟网里任何一台机器都能连到这个端口），
     校验写错就等于谁都能看你的屏幕；
  3. **画面没变不重发**是省流量的命根子（异地经中继时带宽有限），也顺带
     让"连接还活着"这件事有明确信号；
  4. 慢的观看者只能掉帧，不能让发送端内存堆积、不能拖慢别人；
  5. 观看端断开后发送端名单必须归零——否则界面上一直显示"1 人正在观看"；
  6. 抓屏走 GDI，DC / DIB 是手工管理的内核对象，泄漏了看不出来；
  7. 房间内免输观看码靠"房间码+密码派生"两边算出同一个值——这条口径要是
     和临时云盘不一致，房间里点「看屏幕」就会静默失败。

这些都是"打包后才可能坏、坏了也不抛异常"的东西，所以在 127.0.0.1 上
跑真实 TCP（不碰 EasyTier、不要管理员权限、不创建虚拟网卡）把它们固化成
断言。

用法：`Yuhub.exe --screenshare-selftest <结果json路径>`
返回码：0 全部通过 / 1 有断言失败 / 2 参数错误 / 4 结果写盘失败
"""

import json
import os
import socket
import struct
import threading
import time


def _finish(out_file, result):
    checks = result["checks"]
    result["ok"] = bool(checks) and all(c["pass"] for c in checks)
    result["info"]["passed"] = sum(1 for c in checks if c["pass"])
    result["info"]["total"] = len(checks)
    # 先 dumps 再落盘 + default=str：detail 里混进 bytes / 自定义对象时，
    # json.dump 会边序列化边抛，留下一份读不出来的残档，前面的结果全丢。
    try:
        with open(out_file, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(result, ensure_ascii=False, indent=2,
                                default=str))
    except (OSError, TypeError, ValueError):
        return 4
    return 0 if result["ok"] else 1


def _wait(cond, timeout):
    """轮询等一个条件成立（界面层的回调都是异步的，断言前必须先等到）。"""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            if cond():
                return True
        except Exception:
            pass
        time.sleep(0.02)
    try:
        return bool(cond())
    except Exception:
        return False


def _raw_visible_titled():
    """独立数一遍"本来就该被共享列表收录"的顶层窗口，给 list_windows() 当对照组。

    没有对照组，"list_windows() 返回空列表"会让那一串 `all(...)` 断言
    **真空通过**——"一个窗口都没枚举到"和"枚举得完全正确"根本分不出来。

    为什么不能只按"可见 + 有标题"数：最小化、UWP 幽灵（cloaked）、
    工具窗口本来就该被 list_windows() 滤掉——无人值守的桌面上可能
    **所有**窗口都处于这几类（全最小化），那不是枚举坏了。对照组用
    ctypes 独立实现同一套过滤（不走 ss 的代码），list_windows 若因
    枚举逻辑坏掉（如 hwnd 高位截断）而漏窗，两侧就会对不上。
    """
    import ctypes
    import ctypes.wintypes as wt
    u = ctypes.windll.user32
    d = ctypes.windll.dwmapi
    u.IsWindowVisible.argtypes = [wt.HWND]
    u.IsWindowVisible.restype = wt.BOOL
    u.IsIconic.argtypes = [wt.HWND]
    u.IsIconic.restype = wt.BOOL
    u.GetWindowTextLengthW.argtypes = [wt.HWND]
    u.GetWindowTextLengthW.restype = ctypes.c_int
    u.GetWindowLongW.argtypes = [wt.HWND, ctypes.c_int]
    u.GetWindowLongW.restype = ctypes.c_long
    u.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
    u.GetWindowThreadProcessId.restype = wt.DWORD
    u.EnumWindows.argtypes = [ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM),
                              wt.LPARAM]
    u.EnumWindows.restype = wt.BOOL
    GWL_EXSTYLE = -20
    WS_EX_TOOLWINDOW = 0x00000080
    DWMWA_CLOAKED = 14
    mine = os.getpid()
    found = []

    def _cb(hwnd, _lp):
        if not u.IsWindowVisible(hwnd) or u.GetWindowTextLengthW(hwnd) <= 0:
            return True
        pid = wt.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == mine:
            return True
        if u.IsIconic(hwnd):
            return True
        if u.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW:
            return True
        cloaked = wt.DWORD(0)
        d.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(cloaked),
                                ctypes.sizeof(cloaked))
        if cloaked.value:
            return True
        found.append(int(hwnd))
        return True

    try:
        u.EnumWindows(ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)(_cb), 0)
    except Exception:
        pass
    return found


def _iconic(wins):
    """枚举结果里有哪些还是「最小化」的（这个必须为空）。

    最小化的窗口 `IsWindowVisible` 是 True、标题也在，只是被挪到了
    (-32000,-32000) 那个约定位置。我们抓的是**屏幕上的区域**，最小化的
    窗口根本没被绘制 —— 用户从下拉里挑中它，只能看到一片黑。
    """
    import ctypes
    import ctypes.wintypes as wt
    u = ctypes.windll.user32
    u.IsIconic.argtypes = [wt.HWND]
    u.IsIconic.restype = wt.BOOL
    return [w.title[:24] for w in wins if u.IsIconic(w.hwnd)]


class _StubViewer(object):
    """只用来占名额的假观看者（测"人数已满"时不想真开 8 条连接）。"""

    def __init__(self):
        self.alive = False
        self.frames = 0
        self.bytes = 0

    @property
    def label(self):
        return "stub:0"

    def stop(self):
        pass

    def join(self, timeout=1.0):
        pass


def run(out_file, timeout_sec=60):
    checks = []
    result = {"ok": False, "checks": checks, "info": {}}

    def chk(name, cond, detail=""):
        checks.append({"name": name, "pass": bool(cond), "detail": str(detail)})

    def info(key, value):
        result["info"][key] = value

    started = time.monotonic()

    try:
        import screenshare as ss
    except Exception as exc:                       # pragma: no cover - 防御
        chk("导入 screenshare", False, repr(exc))
        return _finish(out_file, result)

    try:
        import lan_share as ls
    except Exception as exc:                       # pragma: no cover - 防御
        ls = None
        info("lan_share", "导入失败：%r" % (exc,))

    # ================================================================== A 协议
    # 帧头是所有东西的地基：观看端完全按这 17 字节去解释后面那一坨字节流，
    # 位置错一位就会把 JPEG 的中间几个字节当成宽高。
    chk("帧头固定 17 字节（4s+B+I+H+H+I）", ss.HEADER.size == 17, ss.HEADER.size)
    chk("魔数与协议串符合约定",
        ss.MAGIC == b"YSH1" and ss.PROTOCOL == "YUHUB-SHARE/1",
        (ss.MAGIC, ss.PROTOCOL))
    packed = ss.HEADER.pack(ss.MAGIC, ss.TYPE_FRAME, 0x01020304, 1600, 1000, 4096)
    chk("帧头编解码往返一致",
        ss.HEADER.unpack(packed) == (ss.MAGIC, ss.TYPE_FRAME, 0x01020304,
                                     1600, 1000, 4096),
        ss.HEADER.unpack(packed))
    idle = ss.HEADER.pack(ss.MAGIC, ss.TYPE_IDLE, 7, 1920, 1080, 0)
    chk("心跳帧零负载（长度字段必须是 0，否则观看端会去读不存在的负载）",
        len(idle) == 17 and ss.HEADER.unpack(idle)[5] == 0,
        (len(idle), ss.HEADER.unpack(idle)))
    presets_ok = (len(ss.QUALITY_PRESETS) == 4
                  and all(len(p) == 4 for p in ss.QUALITY_PRESETS)
                  and all(isinstance(p[0], str) for p in ss.QUALITY_PRESETS)
                  and [p[1] for p in ss.QUALITY_PRESETS] ==
                  sorted(p[1] for p in ss.QUALITY_PRESETS)
                  and all(1 <= p[2] <= 30 for p in ss.QUALITY_PRESETS))
    chk("画质挡位表合法（四项、最长边递增、帧率≤30）",
        presets_ok and 0 <= ss.DEFAULT_QUALITY < len(ss.QUALITY_PRESETS),
        ss.QUALITY_PRESETS)
    # 默认挡位必须是 30 帧：这是用户报"为什么不像原版一样能跑 30 帧"之后
    # 立的目标。挡位表被人改回 8/10/12 时，这条会立刻炸出来。
    chk("★ 默认挡位就是 30 帧（用户要的 30fps 守门员）",
        ss.QUALITY_PRESETS[ss.DEFAULT_QUALITY][2] == 30,
        ss.QUALITY_PRESETS[ss.DEFAULT_QUALITY])
    chk("每档带宽提示条数与挡位一一对应（界面 tooltip 直接按下标取）",
        len(ss.QUALITY_BANDWIDTH) == len(ss.QUALITY_PRESETS),
        (len(ss.QUALITY_BANDWIDTH), len(ss.QUALITY_PRESETS)))

    # ---- 静止画面的判定：用合成数据验取舍，不依赖屏幕当前在放什么 ----
    # 背景：本机桌面上**总有一个 20×20 的小块在闪**（实测每帧稳定产生约
    # 390 字节差异，占画面 0.006%）。逐字节比较因此从不命中，"画面没变就
    # 只发心跳"这条省流量优化会整个失效 —— 静止桌面也按满帧率灌 3.6MB/s。
    base = bytes(range(256)) * 4000
    tiny = bytearray(base)
    # 只改**非采样点**上的字节，模拟真实观察到的那种抖动 —— 本机那个闪烁
    # 的小块实测落在采样点之外（`[::4]` 判为同一画面 7/7）。
    for i in range(1, 400, ss.SAMPLE_STEP):
        tiny[i] ^= 0x0F
    chk("★ 采样签名：小块闪烁算同一画面（逐字节比较在这里从不命中，"
        "省流量优化因此失效）",
        ss.frame_signature(base) == ss.frame_signature(bytes(tiny)),
        "%d 字节里改了 %d" % (len(base),
                            sum(1 for x, y in zip(base, tiny) if x != y)))
    big = bytearray(base)
    for i in range(0, len(big), ss.SAMPLE_STEP):   # 成片变化（滚动/视频/拖窗口）
        big[i] ^= 0xFF
    chk("★ 采样签名：成片变化必然判为不同画面（不会把真实变化吞掉）",
        ss.frame_signature(base) != ss.frame_signature(bytes(big)), len(big))
    chk("静止兜底刷新是有限值（万一样本漏判，观看端也能自己纠正回来）",
        0 < ss.IDLE_REFRESH <= 5, ss.IDLE_REFRESH)
    t0 = time.perf_counter()
    for _ in range(50):
        ss.frame_signature(base)
    cost = (time.perf_counter() - t0) / 50 * 1000
    chk("采样签名足够廉价（远低于 JPEG 编码的十几毫秒）", cost < 3.0,
        "%.2fms / %d 字节" % (cost, len(base)))

    # ================================================================== B 观看码
    # 观看码是唯一的门槛。用 secrets 而不是 random，是因为虚拟网/局域网里
    # 任何一台机器都能连到端口，"可预测"等于没门槛。
    samples = [ss.random_token() for _ in range(300)]
    chk("局域网观看码是 6 位纯数字（方便口头念给朋友）",
        all(len(t) == 6 and t.isdigit() for t in samples),
        [t for t in samples if not (len(t) == 6 and t.isdigit())][:3])
    chk("观看码不是固定值 / 明显可预测（300 次采样高度分散）",
        len(set(samples)) > 200, len(set(samples)))
    if ls is not None:
        chk("★ 房间观看码与临时云盘同口径（房间内点「看屏幕」免输观看码的前提）",
            ss.make_token("ROOM-1", "pw-1") == ls.make_token("ROOM-1", "pw-1"),
            (ss.make_token("ROOM-1", "pw-1"), ls.make_token("ROOM-1", "pw-1")))
    else:
        chk("★ 房间观看码与临时云盘同口径（房间内免输观看码的前提）", False,
            "lan_share 不可导入，无法比对")
    chk("房间观看码对密码敏感",
        ss.make_token("ROOM-1", "pw-1") != ss.make_token("ROOM-1", "pw-2"),
        ss.make_token("ROOM-1", "pw-1"))
    chk("房间观看码对房间码敏感",
        ss.make_token("ROOM-1", "pw") != ss.make_token("ROOM-2", "pw"))
    chk("房间观看码长度 24 位（与云盘 token 等长）",
        len(ss.make_token("A", "B")) == 24, len(ss.make_token("A", "B")))

    # ========================================================= C 房间与端口发布
    ss.set_active_room()
    chk("不在房间里时没有房间观看码（这时只能走局域网+随机码）",
        ss.room_view_token() == "", repr(ss.room_view_token()))
    ss.set_active_room("ROOM-9", "pwd-9", "10.126.126.1")
    room = ss.active_room()
    chk("房间登记能被原样读回",
        (room["code"], room["password"], room["ip"]) ==
        ("ROOM-9", "pwd-9", "10.126.126.1"), room)
    chk("房间内观看码 = 房间码+密码派生",
        ss.room_view_token() == ss.make_token("ROOM-9", "pwd-9"),
        ss.room_view_token())

    published = []
    ss.set_port_publisher(published.append)
    ss.publish_screenshare(45890)
    chk("开共享时把端口交给联机层（队友据此长出「看屏幕」按钮）",
        published == [45890], published)
    ss.publish_screenshare(0)
    chk("停共享时上报 0（对端的按钮要能消失）",
        published == [45890, 0], published)
    ss.set_port_publisher(None)
    ss.publish_screenshare(45890)
    chk("没在房间里（没人登记回调）时静默忽略",
        published == [45890, 0], published)

    def _boom(_port):
        raise RuntimeError("publisher failed")

    ss.set_port_publisher(_boom)
    try:
        ss.publish_screenshare(1234)
        safe = True
    except Exception:
        safe = False
    chk("发布回调抛异常不会冒到共享页（页面不该被联机层的锅弄崩）", safe)
    ss.set_port_publisher(None)

    # 信标里只放端口、不放观看码：不然等于把准入凭证贴在广播包里，
    # 虚拟网里任何一台机器都能收到。这条断言是给未来的改动立的界碑。
    room_token = ss.room_view_token()
    try:
        import etier as _et
        payload = _et.NickBeacon("10.126.126.1", "SelfTest")._payload()
        chk("★ 信标只广播端口、绝不广播观看码", room_token.encode() not in payload,
            payload[:160])
    except Exception as exc:
        chk("★ 信标只广播端口、绝不广播观看码", False, repr(exc))

    ss.set_active_room()
    chk("退房后房间观看码清空", ss.room_view_token() == "")

    # ============================================================== D 端口探测
    # Windows 上"能不能绑"只检查**新 socket** 有没有 SO_REUSEADDR——
    # 新 socket 一旦自己设了，端口被别人 listen 着也照样绑成功。这一条要是
    # 写错，"端口被占"的提示就永远不可能出现（v0.11beta 实测踩过）。
    hold, busy = None, 0
    try:
        hold = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # 故意设上 SO_REUSEADDR：模拟"另一个 Yuhub 实例正占着这个端口"
        hold.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        hold.bind(("127.0.0.1", 0))
        hold.listen(5)
        busy = hold.getsockname()[1]
        chk("被占的端口 port_free 如实报 False", ss.port_free("127.0.0.1", busy) is False,
            busy)
        chk("被占时 pick_port 会往后顺延",
            ss.pick_port(busy, tries=3, ip="127.0.0.1") != busy,
            ss.pick_port(busy, tries=3, ip="127.0.0.1"))
    finally:
        if hold is not None:
            hold.close()
    if busy:
        chk("释放之后 port_free 报 True", ss.port_free("127.0.0.1", busy) is True, busy)
        chk("释放之后 pick_port 拿回原端口",
            ss.pick_port(busy, tries=3, ip="127.0.0.1") == busy,
            ss.pick_port(busy, tries=3, ip="127.0.0.1"))

    # ================================================================ E 抓屏
    # 抓屏这一段刻意不依赖 Qt：它要能整个搬到工作线程里跑（Qt 的抓屏只能
    # 在主线程），而屏幕上那点尺寸/比例换算错了，用户看到的就是拉伸变形的画面。
    screens = ss.list_screens()
    chk("能枚举到显示器", isinstance(screens, list) and len(screens) >= 1, screens)
    chk("显示器项字段齐全且尺寸为正",
        all(s.get("w", 0) > 0 and s.get("h", 0) > 0 and s.get("label")
            for s in screens), screens)
    chk("恰好一块主显示器",
        sum(1 for s in screens if s.get("primary")) == 1,
        [s.get("primary") for s in screens])
    info("screens", [(s["w"], s["h"], s["primary"]) for s in screens])

    cap = ss.GdiCapture()
    chk("fit 把长边压到上限且保持比例",
        cap.fit(2560, 1600, 1280) == (1280, 800), cap.fit(2560, 1600, 1280))
    chk("fit 不放大本来就够小的画面",
        cap.fit(800, 600, 1280) == (800, 600), cap.fit(800, 600, 1280))
    chk("fit 对竖屏按高算长边",
        cap.fit(1080, 1920, 960) == (540, 960), cap.fit(1080, 1920, 960))
    chk("fit 对非法尺寸返回 0×0（不让后面拿 0 去建位图）",
        cap.fit(0, 0, 1280) == (0, 0) and cap.fit(100, 100, 0) == (100, 100),
        (cap.fit(0, 0, 1280), cap.fit(100, 100, 0)))

    prim = next((s for s in screens if s.get("primary")), screens[0])
    rect = (prim["x"], prim["y"], prim["w"], prim["h"])
    ow, oh = cap.fit(prim["w"], prim["h"], 1280)
    data = cap.grab(rect, ow, oh)
    chk("抓一帧画面成功（字节数 = 宽×高×4 的 BGRA）",
        data is not None and len(data) == ow * oh * 4,
        None if data is None else len(data))
    data2 = cap.grab(rect, ow, oh)
    chk("同尺寸连抓第二帧正常（DIB 复用路径，不重建位图）",
        data2 is not None and len(data2) == ow * oh * 4,
        None if data2 is None else len(data2))
    data3 = cap.grab(rect, 64, 48)
    chk("换尺寸抓也正常（位图按需重建）",
        data3 is not None and len(data3) == 64 * 48 * 4,
        None if data3 is None else len(data3))
    hot = sum(1 for i in range(0, min(len(data or b""), 40000), 97)
              if data[i:i + 3] != b"\x00\x00\x00")
    info("grab_nonblank_samples", hot)
    cap.close()
    cap.close()
    data4 = cap.grab(rect, 32, 24)
    chk("close() 可重入，且之后还能继续用（自动重建 DC/位图）",
        data4 is not None and len(data4) == 32 * 24 * 4,
        None if data4 is None else len(data4))
    cap.close()

    # ============================================================ F 窗口枚举
    # "只共享某个窗口"这条路上，把输入法候选框、UWP 幽灵窗口列进去会让用户
    # 一脸问号；把自己这个进程列进去更没意义。
    wins = ss.list_windows()
    chk("窗口枚举不抛异常且返回列表", isinstance(wins, list), type(wins).__name__)
    chk("窗口项结构合法（hwnd>0、标题非空）",
        all(hasattr(w, "hwnd") and isinstance(w.hwnd, int) and w.hwnd > 0
            and isinstance(w.title, str) and w.title for w in wins), len(wins))
    chk("窗口标题里没有空串/纯空白项",
        all(w.title.strip() for w in wins),
        [w.title for w in wins if not w.title.strip()])
    info("windows", [w.title[:40] for w in wins][:20])
    raw_wins = _raw_visible_titled()
    info("raw_visible_titled", len(raw_wins))
    chk("★ 枚举确实在工作（有别进程的可见窗口时至少要列出 1 个）——空列表时"
        "上面几条 all() 是真空通过，这条是它们的对照组",
        (not raw_wins) or len(wins) >= 1, (len(raw_wins), len(wins)))
    chk("枚举里没有最小化的窗口（抓屏抓的是屏幕区域，选它只能看到一片黑）",
        not _iconic(wins), _iconic(wins)[:3])
    # window_rect 得能拿真实窗口的矩形；现场有可见窗口时才验（无头/锁屏环境
    # 可能一个都没有，那就如实记成 skipped，不假装通过）。
    if wins:
        r = ss.window_rect(wins[0].hwnd)
        chk("window_rect 能拿到真实窗口的屏幕矩形",
            r is not None and r[2] > 0 and r[3] > 0, (wins[0].title[:30], r))
    else:
        info("window_rect", "skipped：当前没有可见的顶层窗口")

    # 抓屏坐标系必须自洽：窗口矩形要落在显示器矩形的并集里。这条能抓出一个
    # 真实存在的坑——**非 DPI 感知**的进程里 EnumDisplayMonitors 报的是逻辑
    # 像素、DWM 报的是物理像素，两者差一个缩放倍率（这台 150% 缩放的机器上
    # 是 1707 对 2560）。此时"只共享某个窗口"会抓到旁边那块区域。
    if wins and screens:
        left = min(s["x"] for s in screens)
        top = min(s["y"] for s in screens)
        right = max(s["x"] + s["w"] for s in screens)
        bottom = max(s["y"] + s["h"] for s in screens)
        checked, over = 0, []
        for w in wins:
            r = ss.window_rect(w.hwnd)
            if not r:
                continue
            checked += 1
            # 32px 容差：窗口被拖出屏幕一点点是正常的，差一个量级才是坐标系错位
            ox = max(left - r[0], (r[0] + r[2]) - right, 0)
            oy = max(top - r[1], (r[1] + r[3]) - bottom, 0)
            if max(ox, oy) > 32:
                over.append((w.title[:24], r))
        chk("★ 窗口矩形与显示器在同一坐标系（差一个量级＝「共享某个窗口」抓错区域）",
            checked == 0 or not over, (checked, over[:2]))
    else:
        info("window_in_screen_space", "skipped：没有窗口或显示器可测")

    # ==================================================== G 端到端（127.0.0.1）
    token = ss.make_token("SELF-TEST", "pw")
    events = []
    srv = ss.ShareServer(token, on_event=lambda k, p: events.append((k, p)))
    ok, res = srv.start("127.0.0.1", 0, name="SelfTestHost")
    chk("服务端能在指定地址上开始共享", ok, res)
    port = srv.port
    chk("拿到可用端口", isinstance(port, int) and 1024 < port < 65536, port)
    info("port", port)

    try:
        chk("没有观看者时 push 直接返回 False（不白做编码/发送）",
            srv.push(b"\xff\xd8jpeg", 1600, 1000) is False)

        # ---- 鉴权：观看码是唯一门槛，错了必须铁面拒绝并且不计入观看者
        bad = ss.FrameClient("127.0.0.1", port, "000000",
                             on_frame=lambda *a: None)
        ok_bad, msg_bad = bad.start()
        chk("观看码不对被拒（且给出人话提示）",
            (not ok_bad) and "观看码" in msg_bad, (ok_bad, msg_bad))
        bad.stop()
        empty = ss.FrameClient("127.0.0.1", port, "", on_frame=lambda *a: None)
        ok_empty, msg_empty = empty.start()
        chk("空观看码被拒（假设自己的 token 是空时的兜底）",
            not ok_empty, (ok_empty, msg_empty))
        empty.stop()
        chk("被拒的连接不算观看者", srv.viewers == 0, srv.viewer_list())
        chk("被拒事件已上报（界面能提示「有人试过」）",
            any(k == "rejected" for k, _ in events),
            [k for k, _ in events])

        # ---- 人数上限：塞满假观看者，真连接必须被挡在门外
        stubs = [_StubViewer() for _ in range(ss.MAX_VIEWERS)]
        with srv._lock:
            srv._viewers.extend(stubs)
        full = ss.FrameClient("127.0.0.1", port, token, on_frame=lambda *a: None)
        ok_full, msg_full = full.start()
        full.stop()
        with srv._lock:
            srv._viewers = [v for v in srv._viewers if v not in stubs]
        chk("观看人数满时拒绝新连接（上限 %d 人）" % ss.MAX_VIEWERS,
            (not ok_full) and "满" in msg_full, (ok_full, msg_full))

        # ---- 正常链路
        got = []
        cli = ss.FrameClient(
            "127.0.0.1", port, token,
            on_frame=lambda typ, seq, w, h, payload:
                got.append((typ, seq, w, h, payload)))
        ok_cli, peer = cli.start()
        chk("持正确观看码握手成功并拿到对端名字",
            ok_cli and peer == "SelfTestHost", (ok_cli, peer))
        chk("观看者被登记进名单", _wait(lambda: srv.viewers == 1, 3.0),
            srv.viewer_list())
        chk("加入事件带上了当前人数",
            any(k == "viewer_join" and p.get("total") == 1 for k, p in events),
            [k for k, _ in events])

        jpeg1 = os.urandom(4096)                   # 假装是一帧 JPEG
        chk("投递画面帧返回 True",
            srv.push(jpeg1, 1600, 1000) is True)
        chk("观看端收到这一帧", _wait(lambda: len(got) >= 1, 3.0), len(got))
        if got:
            typ, _seq, w, h, payload = got[0]
            chk("帧类型/宽高/负载逐字节正确",
                typ == ss.TYPE_FRAME and (w, h) == (1600, 1000)
                and payload == jpeg1, (typ, w, h, len(payload)))
        else:
            chk("帧类型/宽高/负载逐字节正确", False, "没收到帧")

        # ---- 画面没变就不重发：异地经中继时省流量的命根子
        before = srv.bytes_sent
        again = srv.push(jpeg1, 1600, 1000)
        chk("★ 画面没变时只发 17 字节心跳、不重发整帧",
            again is False and (srv.bytes_sent - before) == ss.HEADER.size,
            (again, srv.bytes_sent - before))
        chk("心跳也送到了观看端（让他知道连接还活着）",
            _wait(lambda: len(got) >= 2, 3.0), len(got))
        if len(got) >= 2:
            chk("心跳帧类型正确且负载为空",
                got[1][0] == ss.TYPE_IDLE and got[1][4] == b"",
                (got[1][0], len(got[1][4])))

        jpeg2 = os.urandom(4096)
        chk("画面变了就重新发整帧", srv.push(jpeg2, 1600, 1000) is True)
        chk("新画面到达观看端", _wait(lambda: len(got) >= 3, 3.0), len(got))
        if len(got) >= 3:
            chk("新帧负载与旧帧不同（没被误判成「画面没变」）",
                got[2][4] != jpeg1 and got[2][4] == jpeg2, len(got[2][4]))

        stats = srv.viewer_stats()
        chk("观看者统计（谁在看/看了多少）可用",
            len(stats) == 1 and stats[0]["frames"] >= 2, stats)

        # ---- 观看端断开后名单必须归零。
        # 画面静止时发送端可能几秒不发东西，光靠 sendall 报错根本发现不了，
        # 界面上会一直挂着一个早就关掉的"观看者"（这条以前真坏过）。
        cli.stop()
        chk("★ 观看端断开后发送端名单归零（界面上的人数要能降下来）",
            _wait(lambda: srv.viewers == 0, 4.0), srv.viewer_list())
        chk("离开事件已上报",
            any(k == "viewer_leave" for k, _ in events),
            [k for k, _ in events])
    finally:
        srv.stop()

    # ---- 停止之后端口必须真的还回来（不退房/重开共享时不能"端口已被占用"）
    chk("★ 停止共享后监听端口已释放", ss.port_free("127.0.0.1", port) is True, port)
    late = ss.FrameClient("127.0.0.1", port, token, on_frame=lambda *a: None)
    ok_late, msg_late = late.start()
    late.stop()
    chk("停止共享后连不上（画面不会继续外流）", not ok_late, (ok_late, msg_late))

    # ==================================================== H 慢观看者 / 丢帧
    # 每人的"待发帧"只有一格：新帧直接覆盖旧的。慢的观看者只掉帧，不能让
    # 发送端的 socket 缓冲区越积越多（那是内存泄漏的经典姿势），也不能拖慢
    # 别人。这里先做结构级验证，再跑一次真实的"只看不读"观看者。
    shell = ss._Viewer.__new__(ss._Viewer)
    shell._lock = threading.Lock()
    shell._slot = None
    shell._event = threading.Event()
    for i in range(200):
        shell.offer(b"x" * i, True)
    chk("★ 慢观看者的待发帧只有一格（覆盖而不是排队堆积）",
        shell._slot is not None and shell._slot[0] == b"x" * 199
        and not isinstance(shell._slot, list),
        type(shell._slot).__name__)

    # 心跳不许顶掉还没发出去的画面帧。真机上暴露过一次：画面静止时编码
    # 线程按抓屏节奏发心跳，而一帧上百 KB 的 JPEG 还没 sendall 完，心跳就把
    # 那一格盖掉了 —— 观看端只收到心跳，**刚连上就黑屏**，要等一秒的兜底
    # 刷新才出画（冻结态自检里就是这条断言先炸的）。
    # 心跳本身不含信息，让它让位没有任何损失。
    shell._slot = None
    shell.offer(b"FRAME", True)
    shell.offer(b"BEAT", False)
    chk("★ 心跳不顶掉还没发出去的画面帧（顶掉的话观看端刚连上就是黑屏）",
        shell._slot == (b"FRAME", True), shell._slot)
    shell._slot = None
    shell.offer(b"BEAT", False)
    chk("画面帧发完之后心跳照常入队（保活不能断）",
        shell._slot == (b"BEAT", False), shell._slot)
    shell._slot = None
    shell.offer(b"F1", True)
    shell.offer(b"F2", True)
    chk("新画面帧仍然覆盖旧画面帧（慢的人只掉帧，不堆积内存）",
        shell._slot == (b"F2", True), shell._slot)

    token2 = ss.make_token("SLOW-TEST", "pw")
    srv2 = ss.ShareServer(token2)
    ok2, res2 = srv2.start("127.0.0.1", 0, name="SlowHost")
    chk("第二个共享实例能同时起（端口不冲突）", ok2, res2)
    raw = None
    try:
        # 一个只完成握手、之后再也不读的观看者（模拟异地带宽不够的那端）
        raw = socket.create_connection(("127.0.0.1", srv2.port), timeout=3)
        raw.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 2048)
        raw.sendall(("%s %s\n" % (ss.PROTOCOL, token2)).encode("utf-8"))
        line = ss._recv_line(raw, ss.MAX_HELLO)
        chk("只连不读的观看者也能握手成功", bool(line), line)
        chk("他被正常登记", _wait(lambda: srv2.viewers == 1, 3.0), srv2.viewer_list())

        burst = 40
        last_pushed = []
        t0 = time.monotonic()
        for i in range(burst):
            last_pushed.append(os.urandom(64 * 1024))
            srv2.push(last_pushed[-1], 1280, 720)
        elapsed = time.monotonic() - t0
        chk("★ 慢观看者不会阻塞发送端（%d 帧大图投递耗时 < 3s）" % burst,
            elapsed < 3.0, "%.2fs" % elapsed)
        info("slow_viewer_push_sec", round(elapsed, 3))

        # 把积压读出来：必须能看到最后一帧（丢旧不丢新），且明显少发了几帧
        seen = []
        raw.settimeout(0.4)
        deadline = time.monotonic() + 4.0
        try:
            while len(seen) < burst and time.monotonic() < deadline:
                head = ss._recv_exact(raw, ss.HEADER.size)
                if head is None:
                    break
                _m, _t, _s, _w, _h, ln = ss.HEADER.unpack(head)
                body = ss._recv_exact(raw, ln) if ln else b""
                if body is None:
                    break
                seen.append(body)
        except OSError:                            # 含 socket.timeout
            pass
        info("slow_viewer_frames_delivered", len(seen))
        chk("★ 慢观看者只掉旧帧：最后发出的那一帧一定送达",
            bool(seen) and seen[-1] == last_pushed[-1], len(seen))
        chk("确实发生了丢帧（不是把 %d 帧全排在内存里）" % burst,
            len(seen) < burst, len(seen))
    finally:
        if raw is not None:
            try:
                raw.close()
            except OSError:
                pass
        srv2.stop()

    # ============================================================ I 信标 ss 键
    # 队友的成员行靠这个键长出「看屏幕」按钮。开着才发、停掉就消失，
    # 并且旧版客户端（没有这个键）必须照样能用。
    try:
        import etier

        beacon = etier.NickBeacon("10.126.126.1", "SelfTest", ss=0)
        p0 = beacon._payload()
        chk("没在共享屏幕时信标里不带 ss 键",
            b'"ss"' not in p0, p0[:140])
        beacon.set_screenshare(45890)
        p1 = beacon._payload()
        parsed = etier.NickBeacon._parse_info(p1)
        chk("开共享后信标带上屏幕共享端口",
            b'"ss"' in p1 and parsed.get("ss") == 45890, parsed)
        chk("解析仍带回昵称（没有把老键挤掉）",
            parsed.get("name") == "SelfTest", parsed)
        beacon.set_screenshare(0)
        chk("★ 停掉共享后 ss 归零（对端的「看屏幕」按钮会消失）",
            etier.NickBeacon._parse_info(beacon._payload()).get("ss") == 0,
            beacon._payload()[:140])
        old = b'{"yuhub-nick-v1":"OldPeer","game":"MC","port":25565}'
        chk("旧版报文（没有 ss 键）向后兼容，解析为 0",
            etier.NickBeacon._parse_info(old).get("ss") == 0,
            etier.NickBeacon._parse_info(old))
    except Exception as exc:
        chk("信标 ss 键全链路", False, repr(exc))

    # ================================================== J 界面层端到端（需要 Qt）
    # 上面全是引擎层。但真正会砸锅的地方在界面层：`ShareServer` 的
    # viewer_join / rejected / viewer_leave 分别是在 accept 线程与每个观看者
    # 线程里发出的，`FrameClient` 的 closed 是在接收线程里发出的。界面层要是
    # 把这些回调直接接到处理函数上（而不是先丢进 Qt 信号队列），就等于在非
    # GUI 线程里创建 QWidget —— Qt 只留一句
    # `QObject::setParent: Cannot set parent, new parent is in a different
    # thread`，然后整个进程消失，连 Python 堆栈都留不下来。
    # v0.12beta 实测踩过：别人一点「看屏幕」，共享端立刻闪退；而当时的自检
    # 只把事件收进一个 list，所以它一路绿灯。这一节就是为了让那种改动现形。
    try:
        _ui_e2e(chk, info, ss)
    except Exception as exc:                       # pragma: no cover - 防御
        chk("界面层端到端（两页互相对看）", False, repr(exc))

    result["info"]["elapsed_ms"] = int((time.monotonic() - started) * 1000)
    return _finish(out_file, result)


def _ui_e2e(chk, info, ss):
    """两个共享页互相对看：共享端开共享，观看端走完整的「看屏幕」路径。

    这条链覆盖三件事，缺一条都会漏掉上面说的那种闪退：
      1. 引擎的**后台线程事件**要能安全地影响到界面（跨线程必须走信号）；
      2. 收到的 JPEG 要能解码上屏（不是只收进一个 list 就算数）；
      3. 有人观看 / 断开 / 被拒时，界面状态要跟着变。

    Qt 起不来（理论上不会，随包就有）就如实记成 skipped，不假装通过。
    """
    try:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QImage
        from PySide6.QtWidgets import QApplication
        from ui.pages.share_page import SharePage
    except Exception as exc:
        info("ui_e2e", "skipped：Qt 不可用（%r）" % (exc,))
        return

    app = QApplication.instance() or QApplication([])

    # ---- 这一节的核心：把事件处理包一层，记录每个事件是**在哪个线程**里被处理的。
    #
    # 为什么断言线程而不是"进程崩没崩"：那种错法（后台线程直接碰控件）在
    # `offscreen` 平台下**并不会崩**，只是安静地把界面写坏；只有在真实平台
    # 上才闪退。所以"跑下来没崩"根本证明不了什么——必须直接盯住根因：
    # 凡是引擎事件（它们来自 accept 线程 / 观看者线程 / 接收线程），都必须
    # 已经被 Qt 信号队列搬到了主线程再处理。
    calls = []
    _orig_engine = SharePage._on_engine

    def _spy(self, kind, payload):
        calls.append((kind,
                      threading.current_thread() is threading.main_thread()))
        return _orig_engine(self, kind, payload)

    SharePage._on_engine = _spy

    def pump(sec):
        end = time.monotonic() + sec
        while time.monotonic() < end:
            app.processEvents()
            time.sleep(0.01)

    def until(pred, timeout):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                if pred():
                    return True
            except Exception:
                pass
            app.processEvents()
            time.sleep(0.05)
        return False

    host = watch = None
    toasts = []
    try:
        host = SharePage(notify=toasts.append)
        host.resize(900, 620)
        host.show()
        host.on_shown()
        pump(0.6)
        # 下拉里可能一个候选都没有（没网的环境），塞一条进去，让这一节在
        # 任何机器上都能跑完，而不是因为"没网"就静默跳过。
        # 注意 itemData 的格式是 (ip, 是否虚拟网卡) —— 共享模式由它决定。
        if not host._addr_combo.currentData():
            addr = ss.primary_lan_ip() or "127.0.0.1"
            host._addr_combo.addItem(addr + "（本地网络地址）", (addr, False))
            host._addr_combo.setCurrentIndex(host._addr_combo.count() - 1)
        host._start_host()
        chk("共享页能开始共享（模式由选中的网卡决定）",
            until(lambda: bool(host.sharing), 20), host._lbl_stats.text())
        if not host.sharing:
            return
        chk("★ 抓屏与编码跑在两条不同线程上（30 帧成立的前提，合并回一条线程"
            "就只能跑 32fps、没有余量）",
            host._sender._cap is not host._sender._enc
            and host._sender._cap.ident != host._sender._enc.ident
            and host._sender._cap.is_alive() and host._sender._enc.is_alive(),
            [t.name for t in (host._sender._cap, host._sender._enc)])
        chk("选中本地地址 → 判定为局域网共享（异地才需要房间码）",
            host._mode == "lan", host._mode)
        pump(1.2)
        ip = host._server.bind_ip
        port = int(host._lbl_port.text())
        token = host._token_edit.text()
        info("ui_host", "%s:%d" % (ip, port))

        watch = SharePage(notify=toasts.append)
        watch.resize(900, 620)
        watch.show()
        watch.on_shown()
        pump(0.6)
        # 全屏是给"看的人"用的：没在观看时按钮必须是灰的，点它也不能凭空
        # 开出一块黑屏（那会让人以为程序挂了）。
        chk("没在观看时「全屏观看」是灰的（全屏只对观看方有意义）",
            not watch._btn_full.isEnabled(), watch._btn_full.isEnabled())
        mark_off = len(toasts)
        watch._open_full()
        chk("没在观看时点全屏只提示、不开窗口",
            watch._full is None
            and any("先开始观看" in t for t in toasts[mark_off:]),
            (watch._full, toasts[mark_off:]))
        watch.watch_peer(ip, port, token, "自测机")
        # 用 packets（含心跳）判断"连上了"：画面静止时发送端只发心跳，
        # frames 会停在 1，用它做连接判据会假失败。
        chk("★ 观看端连得上并收到数据",
            until(lambda: watch._client is not None
                  and watch._client.packets >= 3, 20),
            None if watch._client is None else watch._client.packets)
        pump(0.8)
        pix = watch._view._pix
        chk("★ 收到的 JPEG 真的解码上屏了",
            pix is not None and not pix.isNull(),
            None if pix is None else (pix.width(), pix.height()))
        # ---- 全屏观看（用户要求：「观看共享屏幕的人需要可以全屏观看」）----
        chk("开始观看后「全屏观看」变为可用",
            until(lambda: watch._btn_full.isEnabled(), 5),
            watch._btn_full.isEnabled())
        watch._open_full()
        pump(0.3)
        chk("★ 全屏开出的是独立全屏窗口（不是把主窗口最大化 —— 主窗口有侧栏，"
            "最大化后画面周围仍有一圈非画面区域）",
            watch._full is not None and watch._full.isVisible()
            and watch._full.isFullScreen()
            and watch._full.parent() is None,
            None if watch._full is None else (
                watch._full.isVisible(), watch._full.isFullScreen(),
                watch._full.parent()))
        chk("★ 一进全屏就有画面，不用等下一帧",
            watch._full is not None and watch._full._view._pix is not None
            and not watch._full._view._pix.isNull(), None)
        # 这两条守的是两个"默认值陷阱"：QWidget 默认 NoFocus（Esc 收不到）、
        # 默认不跟踪鼠标（提示条浮不出来）。都很容易被后来的人顺手删掉。
        chk("全屏窗口能收键盘（FocusPolicy 不是默认的 NoFocus，否则 Esc 失灵）",
            watch._full.focusPolicy() != Qt.FocusPolicy.NoFocus,
            watch._full.focusPolicy())
        chk("全屏窗口开着鼠标跟踪（否则「怎么退出」的提示永远浮不出来）",
            watch._full.hasMouseTracking(), watch._full.hasMouseTracking())
        # 新到的一帧要**同时**刷到页面里那份和全屏那份 —— 只刷其中一份的话
        # 全屏看的就是一张定格图（最典型的"能全屏但画面不动"）。
        from PySide6.QtCore import QBuffer, QIODevice
        _img = QImage(96, 64, QImage.Format.Format_RGB32)
        _img.fill(0x3366CC)
        _buf = QBuffer()
        _buf.open(QIODevice.OpenModeFlag.WriteOnly)
        _img.save(_buf, "JPEG", 80)
        _data = bytes(_buf.data())
        _buf.close()
        watch._view._pix = None
        watch._full._view._pix = None
        watch._on_engine("frame", _data)
        pump(0.2)
        chk("★ 新帧同时刷到页面与全屏两份画面（否则全屏看到的是一张定格图）",
            watch._view._pix is not None and watch._full._view._pix is not None,
            (watch._view._pix is not None, watch._full._view._pix is not None))

        from PySide6.QtCore import QEvent, QPointF
        from PySide6.QtGui import QKeyEvent, QMouseEvent
        watch._full._tip.hide()
        watch._full.mouseMoveEvent(QMouseEvent(
            QEvent.Type.MouseMove, QPointF(20.0, 20.0), QPointF(20.0, 20.0),
            Qt.MouseButton.NoButton, Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier))
        chk("鼠标一动就浮出「怎么退出全屏」的提示条",
            watch._full._tip.isVisible(), watch._full._tip.isVisible())

        watch._full._view.double_clicked.emit()          # 双击画面
        pump(0.2)
        chk("双击画面能退出全屏", not watch._full.isVisible(),
            watch._full.isVisible())
        watch._open_full()
        pump(0.2)
        watch._full.keyPressEvent(QKeyEvent(
            QEvent.Type.KeyPress, Qt.Key.Key_Escape,
            Qt.KeyboardModifier.NoModifier))
        pump(0.2)
        chk("Esc 能退出全屏", not watch._full.isVisible(), watch._full.isVisible())
        chk("退出全屏只是隐藏窗口（留同一个实例复用，不重建）",
            watch._full is not None, watch._full)

        chk("共享端看到有人观看", host._server.viewers == 1,
            host._server.viewer_list())
        chk("★ 共享端弹出了「有人开始观看」提示",
            any("观看" in t for t in toasts), toasts[-2:])
        chk("共享端统计行进入了「有人在看」状态",
            "人正在观看" in host._lbl_stats.text(), host._lbl_stats.text())

        watch._open_full()
        pump(0.2)
        watch._stop_watch()
        chk("★ 停止观看会自动退出全屏（不然会剩一块黑屏盖住整个桌面，像死机）",
            not watch._full.isVisible(), watch._full.isVisible())
        chk("观看端断开后共享端人数归零",
            until(lambda: host._server.viewers == 0, 8),
            host._server.viewer_list())
        chk("断开后共享端统计行回落（每秒刷一次，给一拍）",
            until(lambda: "等待观看者接入" in host._lbl_stats.text(), 5),
            host._lbl_stats.text())

        bad = ss.FrameClient(ip, port, "000000", on_frame=lambda *a: None)
        bad.start()
        bad.stop()
        chk("错误观看码被拒时共享端不崩、且给出提示",
            until(lambda: any("观看码" in t for t in toasts), 8), toasts[-2:])

        # 共享模式**只由选中的网卡决定**，没有第二个开关。选虚拟网卡就必须
        # 是异地链路：没进房间时应当明确拒绝，而不是静默退回局域网共享
        # （那样队友会在虚拟网里等一个永远连不上的地址）。
        host._stop_host()
        host._addr_combo.addItem("10.126.126.7（异地联机虚拟网卡）",
                                 ("10.126.126.7", True))
        host._addr_combo.setCurrentIndex(host._addr_combo.count() - 1)
        chk("★ 选中虚拟网卡即判为异地链路（共享模式唯一由地址决定）",
            host._addr_mode() == ("10.126.126.7", True), host._addr_mode())
        mark = len(toasts)
        host._start_host()
        chk("★ 选了虚拟网卡但没进房间 → 拒绝并说明，不静默退回局域网",
            not host.sharing and any("房间" in t for t in toasts[mark:]),
            toasts[mark:])

        kinds = [k for k, _ in calls]
        info("ui_events", kinds)
        chk("★ 引擎事件全部在主线程里处理（后台线程直接碰控件＝真实平台上闪退）",
            bool(calls) and all(main for _, main in calls), calls)
        chk("有人观看 / 离开 / 被拒三类事件都到齐了",
            {"viewer_join", "viewer_leave", "rejected"} <= set(kinds),
            sorted(set(kinds)))
    finally:
        SharePage._on_engine = _orig_engine
        for page in (watch, host):
            if page is not None:
                try:
                    page.shutdown()
                except Exception:
                    pass
        pump(0.4)
