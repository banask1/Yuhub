# -*- coding: utf-8 -*-
"""帧驱动动画引擎自检（**纯计算，不碰文件/注册表/网络**）。

守什么
------
`ui/motion_engine.py` 是路线 B 的地基，它坏了的表现全是"看着还行、其实
不对"：弹簧不过冲（果冻感没了）、收敛不了（CPU 一直烧）、改目标丢速度
（连点变顿）、停表失效（后台一直跑）、预算闸门失灵（掉帧时装饰不让路）。
这些都不会报错，只会"手感变了"——必须靠断言钉死。

三个必须用**真调用**证明的点（不许拿函数的输出当自己的基准）：
  1. 弹簧的过冲与收敛 —— 数值积分整条轨迹，量峰值与收敛时刻；
  2. 可打断 —— 中途改目标，断言速度**没有归零**（这是"接力"与"重启"的
     唯一区别），且值连续（不跳变）；
  3. 闲时停表 —— 最后一个动画收敛后定时器真的 `isActive()` 为假。

时间用假时钟注入（`_FakeElapsed`）：真定时器在冻结态/CI 上节拍不可控，
用它才能断言"间隔是多少"而不是"大概跑了几帧"。

用法： ``Yuhub.exe --motion-selftest <结果json路径>``
"""

import json

#: 自检里统一用 60fps 的假 dt
DT60 = 1.0 / 60.0


def _dump(result, out_path):
    try:
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=2)
    except OSError:
        pass


class _FakeElapsed:
    """QElapsedTimer 的替身：每次 restart() 固定返回 dt_ms。

    只实现引擎真正用到的四个方法。用它可以断言"定时器该不该开、开多快"，
    而且**不受**宿主机负载影响 —— 真定时器在自检里跑出来的间隔是不可信的。
    """

    def __init__(self, dt_ms=16.0):
        self.dt_ms = float(dt_ms)
        self._valid = False

    def isValid(self):
        return self._valid

    def start(self):
        self._valid = True

    def invalidate(self):
        self._valid = False

    def restart(self):
        self._valid = True
        return self.dt_ms


class _Ticker:
    """最小 ticker：记录被调了几次，按 `life` 决定何时结束。"""

    def __init__(self, life=None, sleep_ms=0.0, boom=False):
        self.calls = 0
        self.life = life
        self.sleep_ms = sleep_ms
        self.boom = boom
        self.detached = 0

    def step(self, dt):
        self.calls += 1
        if self.boom:
            raise RuntimeError("模拟已析构的 C++ 对象")
        if self.sleep_ms:
            import time
            time.sleep(self.sleep_ms / 1000.0)
        if self.life is None:
            return True
        return self.calls < self.life

    def on_detached(self):
        self.detached += 1


def run(out_path):
    result = {"ok": True, "checks": []}

    def chk(name, passed, detail=""):
        result["checks"].append(
            {"name": name, "passed": bool(passed), "detail": str(detail)})
        if not passed:
            result["ok"] = False

    try:
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance() or QApplication([])
        _ = app
        from ui import motion
        from ui import motion_engine as me
    except Exception as exc:                                   # noqa: BLE001
        result["ok"] = False
        result["error"] = repr(exc)
        _dump(result, out_path)
        return 1

    # ================= ① 预设与常量 =================
    chk("帧间隔常量：交互 16ms / 装饰 42ms / 交互必须更快",
        me.FRAME_MS == 16 and me.AMBIENT_MS == 42 and me.FRAME_MS < me.AMBIENT_MS,
        (me.FRAME_MS, me.AMBIENT_MS))
    chk("优先级顺序：交互 < 转场 < 装饰（数值越小越要紧）",
        me.PRIO_INTERACTIVE < me.PRIO_TRANSITION < me.PRIO_AMBIENT,
        (me.PRIO_INTERACTIVE, me.PRIO_TRANSITION, me.PRIO_AMBIENT))
    chk("两条弹簧预设都合法（k>0, c>0）",
        all(k > 0 and c > 0 for k, c in
            (motion.SPRING_JELLY, motion.SPRING_SOFT)),
        (motion.SPRING_JELLY, motion.SPRING_SOFT))

    # ================= ② 弹簧：过冲 / 收敛 / 可打断 =================
    sp = me.Spring(0.0, motion.SPRING_JELLY)
    peak, settle_ms, frames = 0.0, None, 0
    sp.to(1.0)
    for i in range(240):                       # 最多 4 秒
        alive = sp.step(DT60)
        frames += 1
        peak = max(peak, sp.value)
        if not alive:
            settle_ms = frames * DT60 * 1000.0
            break
    chk("弹簧 0→1 会过冲（果冻感的物理来源，>1.05）", peak > 1.05, round(peak, 3))
    chk("过冲幅度在合理区间（5%~30%，太小感觉不到、太大会晃）",
        1.05 < peak < 1.30, round(peak, 3))
    chk("弹簧能收敛（否则会一直烧 CPU）",
        settle_ms is not None, settle_ms)
    chk("收敛时间与令牌 PILL/PAGE_IN 同量级（150~450ms）",
        settle_ms is not None and 150 <= settle_ms <= 450,
        settle_ms)
    chk("收敛后 step 返回 False（时钟据此摘掉它）", sp.step(DT60) is False)

    # -- 可打断：中途改目标，速度必须接力（不归零）--
    sp2 = me.Spring(0.0, motion.SPRING_JELLY)
    sp2.to(1.0)
    for _ in range(6):
        sp2.step(DT60)
    v_mid = sp2.velocity
    x_mid = sp2.value
    sp2.to(0.0)                                # 半路反向
    chk("可打断：中速运行时改目标，速度没有归零（接力而非重启）",
        abs(v_mid) > 0.05 and sp2.velocity == v_mid,
        (round(v_mid, 3), round(sp2.velocity, 3)))
    chk("可打断：改目标瞬间值不跳变",
        abs(sp2.value - x_mid) < 1e-9, (sp2.value, x_mid))
    # 反向之后必须掉头回落（否则弹簧是"锁死"的）
    for _ in range(30):
        sp2.step(DT60)
    chk("反向改目标后真的往回走（没有卡在旧目标）",
        sp2.value < x_mid + 1e-6, (round(sp2.value, 3), round(x_mid, 3)))

    # -- 帧率无关：同一段动画在 60fps 与 120fps 下落点一致 --
    def _settle_at(dt):
        s = me.Spring(0.0, motion.SPRING_JELLY)
        s.to(1.0)
        n = 0
        while s.step(dt) and n < 4000:
            n += 1
        return n * dt * 1000.0

    t60, t120 = _settle_at(DT60), _settle_at(DT60 / 2.0)
    chk("帧率无关：60fps 与 120fps 的收敛时间差 < 15%",
        abs(t60 - t120) <= max(t60, t120) * 0.15,
        (round(t60, 1), round(t120, 1)))
    # 抖动帧（16ms/33ms 交替）也不能发散
    sj = me.Spring(0.0, motion.SPRING_JELLY)
    sj.to(1.0)
    worst = 0.0
    for i in range(400):
        if sj.step(0.016 if i % 2 == 0 else 0.033) is False:
            break
        worst = max(worst, sj.value)
    chk("帧间隔抖动（16/33ms 交替）时过冲不发散（<1.4）", worst < 1.4,
        round(worst, 3))

    # ================= ③ Tween：时长精确 =================
    val = {"v": None, "done": 0}
    tw = me.Tween(0.0, 1.0, 180, easing=motion.CURVE_OUT,
                  on_change=lambda v, _vel: val.__setitem__("v", v),
                  on_done=lambda: val.__setitem__("done", val["done"] + 1))
    tw.start()
    est = 0.0
    while True:
        est += DT60 * 1000.0
        if not tw.step(DT60):
            break
        if est > 1000:
            break
    chk("Tween 在设定时长附近结束（±1 帧）",
        abs(est - 180) <= 17, round(est, 1))
    chk("Tween 结束时值精确等于目标", val["v"] == 1.0, val["v"])
    chk("Tween 的 on_done 只调一次（没被重复触发）", val["done"] == 1, val["done"])

    # ================= ④ 时钟：停表 / 降帧 / 闸门 =================
    clk = me.FrameClock()
    fake = _FakeElapsed(16.0)
    clk._elapsed = fake

    chk("空时钟不跑（闲时零开销的前提）", not clk.is_running())

    amb = _Ticker()
    clk.add(amb, me.PRIO_AMBIENT)
    chk("只有装饰动画时用 AMBIENT_MS（24fps）",
        clk.is_running() and clk._interval == me.AMBIENT_MS, clk._interval)

    hot = _Ticker()
    clk.add(hot, me.PRIO_INTERACTIVE)
    chk("出现交互动画立刻升到 FRAME_MS（60fps）",
        clk._interval == me.FRAME_MS, clk._interval)

    for _ in range(4):
        clk._tick()
    chk("两个车道的 ticker 都被推进",
        amb.calls == 4 and hot.calls == 4, (amb.calls, hot.calls))

    # 交互动画结束后应回到装饰节奏
    hot.life = 5
    clk._tick()
    clk._tick()
    chk("交互动画结束后回到装饰节奏（不浪费 60fps）",
        clk._interval == me.AMBIENT_MS, clk._interval)

    # 装饰层全部结束后必须停表
    clk.remove(amb)
    chk("最后一个动画移除后定时器停表", not clk.is_running(), clk._interval)

    # -- ambient 闸门 --
    clk2 = me.FrameClock()
    clk2._elapsed = _FakeElapsed(42.0)
    a2 = _Ticker()
    clk2.add(a2, me.PRIO_AMBIENT)
    clk2.set_ambient_enabled(False)
    chk("装饰层被关掉后即使还有 ticker 也停表（窗口最小化即停）",
        not clk2.is_running(), clk2._interval)
    clk2.set_ambient_enabled(True)
    chk("窗口回来立刻恢复装饰节奏", clk2.is_running() and clk2._interval == me.AMBIENT_MS,
        clk2._interval)
    clk2._tick()
    chk("恢复后装饰 ticker 真的被推进了", a2.calls >= 1, a2.calls)

    # -- mark_busy：转场期间保持高帧率，并跳过装饰层 --
    clk3 = me.FrameClock()
    clk3._elapsed = _FakeElapsed(16.0)
    a3 = _Ticker()
    clk3.add(a3, me.PRIO_AMBIENT)
    clk3.mark_busy(300)
    chk("转场期间保持 FRAME_MS", clk3._interval == me.FRAME_MS, clk3._interval)
    before = a3.calls
    clk3._tick()
    chk("转场期间装饰层让路（帧留给交互/转场）",
        a3.calls == before and clk3.skipped_ambient == 1,
        (a3.calls, clk3.skipped_ambient))

    # -- 预算闸门：上一帧超预算 → 本帧跳装饰 --
    clk4 = me.FrameClock()
    clk4._elapsed = _FakeElapsed(16.0)
    a4 = _Ticker()
    clk4.add(a4, me.PRIO_AMBIENT)
    clk4.last_ms = me.BUDGET_MS + 5.0          # 假装上一帧超了预算
    clk4._tick()
    chk("上一帧超预算时本帧跳过装饰层（掉帧优先保手感）",
        a4.calls == 0 and clk4.skipped_ambient == 1,
        (a4.calls, clk4.skipped_ambient))

    # -- 装饰时间的"暂停"语义：跳过期间不前进，恢复时不瞬移 --
    clk6 = me.FrameClock()
    clk6._elapsed = _FakeElapsed(16.0)
    a6 = _Ticker()
    clk6.add(a6, me.PRIO_AMBIENT)
    clk6._tick()
    _t1 = clk6._anim_time
    clk6.mark_busy(200)                        # 转场期间跳装饰
    clk6._tick()
    _t2 = clk6._anim_time
    chk("装饰层被跳过时动画时间不前进（恢复时不会瞬移几像素）",
        _t1 > 0 and _t2 == _t1, (round(_t1, 4), round(_t2, 4)))

    # -- 掉帧检测：帧间隔明显超出目标 → 装饰让路 --
    # 这一条用 80ms 的假间隔（目标 42ms）模拟"上一帧被大页面重绘拖住"。
    clk7 = me.FrameClock()
    clk7._elapsed = _FakeElapsed(80.0)
    a7 = _Ticker()
    clk7.add(a7, me.PRIO_AMBIENT)
    clk7._tick()
    chk("帧间隔远超目标（掉帧）时装饰层让路",
        a7.calls == 0 and clk7.skipped_ambient == 1,
        (a7.calls, clk7.skipped_ambient))

    # -- 死对象防护：ticker 抛 RuntimeError 时必须被摘掉且不影响其它 ticker --
    clk5 = me.FrameClock()
    clk5._elapsed = _FakeElapsed(16.0)
    dead = _Ticker(boom=True)
    live = _Ticker()
    clk5.add(dead, me.PRIO_INTERACTIVE)
    clk5.add(live, me.PRIO_INTERACTIVE)
    clk5._tick()
    chk("ticker 抛 RuntimeError（控件已析构）不会拖垮整帧",
        live.calls == 1, (dead.calls, live.calls))
    chk("抛异常的 ticker 被摘掉并收到 on_detached",
        dead.detached == 1 and clk5.active(me.PRIO_INTERACTIVE) == 1,
        (dead.detached, clk5.active(me.PRIO_INTERACTIVE)))
    clk5._tick()
    chk("从车道移除后不再被调用", dead.calls == 1, dead.calls)

    # -- 统计口径 --
    st = clk4.stats()
    chk("stats() 有帧耗时分位数（p50 ≤ p95 ≤ peak）",
        st["p50_ms"] <= st["p95_ms"] <= st["peak_ms"] + 1e-6,
        (st["p50_ms"], st["p95_ms"], st["peak_ms"]))
    chk("stats() 记录装饰跳帧次数", "skipped_ambient" in st)

    # ================= ⑤ 集成：控件真的挂在引擎上 =================
    try:
        from ui.widgets import ToggleSwitch

        sw = ToggleSwitch(False)
        sw.resize(42, 24)
        sw.setChecked(True)                     # 触发 toggled → spring.to(1.0)
        chk("开关的滑块由弹簧驱动（target 已指向 1）",
            abs(sw._spring.target - 1.0) < 1e-9, sw._spring.target)
        y0 = sw._offset
        for _ in range(6):
            me.clock()._tick()                  # 手动推进（自检里不依赖真定时器）
        chk("开关滑块随帧推进真的在动", abs(sw._offset - y0) > 0.01,
            (round(y0, 3), round(sw._offset, 3)))
        chk("开关的拉伸量由速度换算（帧率无关）",
            sw._stretch > 0.0, round(sw._stretch, 3))
        sw.deleteLater()
    except Exception as exc:                                # noqa: BLE001
        chk("开关已接入帧驱动引擎", False, repr(exc))

    try:
        from ui.ink import BambooStrip

        bs = BambooStrip("mini")
        bs.resize(120, 120)
        chk("竹子注册在装饰车道（可被预算闸门跳过）",
            me.clock().active(me.PRIO_AMBIENT) >= 1,
            me.clock().active(me.PRIO_AMBIENT))
        chk("竹子的 step() 返回 True（保持占用车道）", bs.step(DT60) is True)
        bs.deleteLater()
    except Exception as exc:                                # noqa: BLE001
        chk("竹子已接入帧驱动引擎", False, repr(exc))

    try:
        from ui.ink import InkPond

        pd = InkPond(3)
        pd.resize(320, 200)
        chk("鱼池注册在装饰车道",
            pd in me.clock()._lanes[me.PRIO_AMBIENT],
            me.clock().active(me.PRIO_AMBIENT))
        # ⚠️ 这条是 v1.3.0 真踩过的 bug：`step()` 忘了 return True → 返回
        # None → 时钟把 falsy 当成"已结束"，第一次 tick 就把鱼池摘掉 →
        # 鱼一动不动，而界面上没有任何报错。断言必须精确到 `is True`。
        chk("鱼池的 step() 显式返回 True（None 会被当成'已结束'而静默摘除）",
            pd.step(DT60) is True, repr(pd.step(DT60)))
        for _ in range(3):
            me.clock()._tick()
        chk("鱼池 tick 之后仍在车道里（没有被误摘）",
            pd in me.clock()._lanes[me.PRIO_AMBIENT],
            me.clock().active(me.PRIO_AMBIENT))
        pd.deleteLater()
    except Exception as exc:                                # noqa: BLE001
        chk("鱼池已接入帧驱动引擎", False, repr(exc))

    _dump(result, out_path)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    import sys
    sys.exit(run(sys.argv[1] if len(sys.argv) > 1 else "motion_selftest.json"))
