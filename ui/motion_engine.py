# -*- coding: utf-8 -*-
"""帧驱动动画引擎：一个时钟、物理弹簧、预算闸门（路线 B，v1.3.0）。

为什么要有这一层
----------------
v1.2.x 之前，动画是"各管各的"：每个动画自己 `QPropertyAnimation`、每个
自绘动画自己 `QTimer`。这带来三个改不动的问题：

1. **没有统一时钟**。N 个动画 = N 个定时器唤醒，相位各不相同；也没有任何
   地方能回答"这一帧到底花了多久"。
2. **不可打断**。固定时长 + 曲线插值，用户连点只能 `stop()` 再从头走一遍：
   速度丢失，观感是"顿一下再走"。移动端的手感来自**物理弹簧**——改目标
   不重置速度，连点是接力而不是重启。
3. **装饰动画没有预算**。竹子/鱼池这类背景装饰和用户正在做的交互抢同一
   份帧预算，而它们的重要性差着量级。

这个模块提供三样东西：
    ``Spring``  物理弹簧（半隐式欧拉、固定子步长、带速度接力、自动收敛）
    ``Tween``   按时间精确结束的过渡（Toast 退场、弹窗进场用）
    ``clock``   唯一帧时钟（优先级分道 + 预算闸门 + 闲时停表）

省性能的四条硬机制（路线 B 敢上的前提）
--------------------------------------
1. **闲时停表**：没有任何活跃动画时定时器直接 `stop()`，常驻开销为 0。
   竹子/鱼池以前是永久 24/30fps，现在是"有动画才有时钟"。
2. **降帧**：只有装饰动画时把间隔拉到 ``AMBIENT_MS``(42ms≈24fps)；一旦有
   交互/转场立刻回到 ``FRAME_MS``(16ms)。用户手指在动时才有必要 60fps。
3. **遮挡即停**：窗口最小化 / 隐藏 / 被完全遮挡时由窗口状态驱动
   ``set_ambient_enabled(False)``，装饰层连 tick 都不发生
   （原来竹子只查 ``isVisible()``，最小化时仍然在为不可见的画面算每一片叶子）。
4. **超预算跳装饰**：上一帧耗时超过 ``BUDGET_MS``，本帧只推进交互与转场，
   装饰层跳过 —— 掉帧时优先保住手感，而不是让竹子继续摇头。

⚠️ 两条必须守住的实现约束：
* 每帧只算**一次** dt，所有动画共用（各自 `time.monotonic()` 会让同一帧里
  不同动画的相位差一点点，叠在弹簧上就是"抖"）。
* 定时器只在"需要"的时候按"需要"的间隔开，**不要**每帧 `setInterval`
  （在运行的 QTimer 上改间隔会重置相位，等于每帧丢一次节拍）。
"""

import time

from PySide6.QtCore import QElapsedTimer, QObject, Qt, QTimer

from . import motion

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
#: 有交互/转场时的目标帧间隔（≈60fps）
FRAME_MS = 16
#: 只有装饰动画时的帧间隔（≈24fps）。与竹子原来的节奏一致 —— 风是低通
#: 滤波出来的慢变量，24fps 看不出台阶，开销却少 60%。
AMBIENT_MS = 42
#: 单帧预算（毫秒，纯动画推进耗时，不含光栅化）。超了下一帧跳过装饰层。
BUDGET_MS = 6.0
#: 帧间隔比目标长出多少就算"掉帧"，此时装饰层让路（秒）。
#: 12ms ≈ 一帧 60fps 的量：正常的定时器抖动（±1~2ms）不会误触发，
#: 而一次真实的大页面重绘（常见 20~40ms）一定触发。
LAG_TOLERANCE_S = 0.012

#: 优先级：数值越小越"要紧"
PRIO_INTERACTIVE = 0    # 用户正在操作：开关滑块、hover、按压
PRIO_TRANSITION = 1     # 系统响应：页面切换、弹窗、Toast
PRIO_AMBIENT = 2        # 背景装饰：竹子、鱼池 —— 随时可以被跳过


# ---------------------------------------------------------------------------
# 帧时钟
# ---------------------------------------------------------------------------
class FrameClock(QObject):
    """整进程唯一的动画时钟。

    三个优先级各一条"车道"（lane），每帧按 交互 → 转场 → 装饰 的顺序推进。
    注册的对象只需要实现 ``step(dt) -> bool``：返回 False 表示自己已经结束，
    时钟会把它从车道上摘掉（省掉下一次遍历）。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.PreciseTimer)
        #: 别用 CoarseTimer：Windows 上会被对齐到 15.6ms 网格，弹簧每次
        #: 收到的 dt 都在跳，积分出来肉眼可见的"一顿一顿"。
        self._timer.timeout.connect(self._tick)
        self._elapsed = QElapsedTimer()
        self._lanes = ([], [], [])
        self._interval = -1          # -1 = 停表中
        self._busy_until = 0.0
        self._ambient_on = True

        # ---- 观测（--perf-hud / 自检读它）----
        self.frames = 0
        self.skipped_ambient = 0
        self.last_ms = 0.0
        self.peak_ms = 0.0
        self._cost = []
        #: 装饰层专用的**累计时间**（秒）。
        #: 为什么不给装饰动画用墙钟：闸门会跳过装饰帧（转场 / 掉帧 / 窗口
        #: 隐藏），如果它们按墙钟求值，恢复的那一帧会"瞬移"到当前相位 ——
        #: 实测竹子顶端会一次跳 4~5px，正好在页面切换时被看见。用累计时间
        #: 就只有"暂停"，恢复时从原地继续，没有任何跳变。
        self._anim_time = 0.0

    # -- 注册 ---------------------------------------------------------------
    def add(self, ticker, prio=PRIO_INTERACTIVE):
        lane = self._lanes[prio]
        if ticker in lane:
            return ticker
        lane.append(ticker)
        self._sync()
        return ticker

    def remove(self, ticker):
        for lane in self._lanes:
            try:
                lane.remove(ticker)
            except ValueError:
                pass
        self._sync()

    def active(self, prio=None):
        if prio is None:
            return sum(len(l) for l in self._lanes)
        return len(self._lanes[prio])

    def is_running(self):
        return self._timer.isActive()

    # -- 闸门 ---------------------------------------------------------------
    def mark_busy(self, ms):
        """接下来 ``ms`` 毫秒内保持高帧率（页面切换、弹窗进场这类）。

        为什么需要它：转场动画走的是 Qt 自己的 QPropertyAnimation（仍在
        Qt 的时钟上），本引擎看不见它。用这个"占位"让装饰层在转场期间
        让路 —— 恰恰是这 300ms 里，帧预算最紧张。
        """
        self._busy_until = max(self._busy_until,
                               time.monotonic() + max(0, int(ms)) / 1000.0)
        self._sync()

    def set_ambient_enabled(self, on):
        """装饰层开关（窗口最小化 / 隐藏 / 被遮挡时置 False）。"""
        on = bool(on)
        if on == self._ambient_on:
            return
        self._ambient_on = on
        self._sync()

    def ambient_enabled(self):
        return self._ambient_on

    # -- 时钟本体 -----------------------------------------------------------
    def _sync(self):
        """按当前负载决定"要不要跑、多久跑一次"。

        只在间隔真的变了时才 `setInterval` —— 在运行的 QTimer 上改间隔会
        重置相位，每帧改一次等于每帧丢一次节拍。
        """
        hot = bool(self._lanes[PRIO_INTERACTIVE]) or \
            bool(self._lanes[PRIO_TRANSITION])
        busy = time.monotonic() < self._busy_until
        ambient = self._ambient_on and bool(self._lanes[PRIO_AMBIENT])

        if hot or busy:
            want = FRAME_MS
        elif ambient:
            want = AMBIENT_MS
        else:
            want = 0

        if want == 0:
            if self._timer.isActive():
                self._timer.stop()
            self._interval = -1
            self._elapsed.invalidate()      # 下次启动重新计时，别把停表时长算进 dt
            return

        if want != self._interval or not self._timer.isActive():
            self._timer.setInterval(want)
            self._interval = want
            if not self._elapsed.isValid():
                self._elapsed.start()
            self._timer.start()

    def _tick(self):
        if not self._elapsed.isValid():
            self._elapsed.start()
            return
        dt = self._elapsed.restart() / 1000.0
        if dt <= 0.0:
            return
        # 长卡顿（拖窗口、断点）之后别让物理一次冲过头；弹簧本身有多子步，
        # 这里只兜住"几分钟没跑"这种极端值。
        if dt > 0.10:
            dt = 0.10

        t0 = time.perf_counter()
        self.frames += 1
        # 三种让路：
        #   ① 正在转场（mark_busy）；
        #   ② **真的掉帧了**：本帧间隔比目标长出 >12ms，说明上一帧的事件循环
        #      里有别的东西在抢时间（大页面重绘、磁盘/网络回调）。这时装饰层
        #      让路比继续摇头更有价值。
        #   ③ 上一帧推进本身超预算（病态：某个 step 里做了重活）。
        #
        # 关于②：不能用"引擎自己的 tick 耗时"当判据 —— 实测 p50 只有 0.07ms，
        # 永远到不了预算；真正的耗时发生在 paint 里，而 paint 在事件循环的
        # 另一段。用"帧间隔是否明显超出目标"才是可观测的真实信号。
        #
        # 为什么跳掉装饰不会"看出来"：竹子/鱼池用的是**累计动画时间**
        # （anim_time()），跳过期间它不前进 —— 恢复时从原地继续，不会瞬移。
        busy = time.monotonic() < self._busy_until
        lag = dt - (self._interval / 1000.0 if self._interval > 0 else dt)
        skip_ambient = busy or self.last_ms > BUDGET_MS or lag > LAG_TOLERANCE_S

        self._step_lane(PRIO_INTERACTIVE, dt)
        self._step_lane(PRIO_TRANSITION, dt)
        if self._ambient_on:
            if skip_ambient:
                if self._lanes[PRIO_AMBIENT]:
                    self.skipped_ambient += 1
            else:
                # 装饰层的相位时间只在**真的推进它**时累加（见 _anim_time 注释）
                self._anim_time += dt
                self._step_lane(PRIO_AMBIENT, dt)

        self.last_ms = (time.perf_counter() - t0) * 1000.0
        if self.last_ms > self.peak_ms:
            self.peak_ms = self.last_ms
        self._cost.append(self.last_ms)
        if len(self._cost) > 240:
            del self._cost[:120]
        self._sync()          # 有动画收敛了 → 可能要降帧/停表

    def _step_lane(self, prio, dt):
        lane = self._lanes[prio]
        if not lane:
            return
        done = None
        for t in tuple(lane):
            try:
                alive = t.step(dt)
            except RuntimeError:
                # 控件的 C++ 对象已经析构（窗口关了、页面销毁了）。
                # 这个项目在 Toast / 页面淡入上已经栽过三次"碰到已析构对象"，
                # 引擎这一层必须自己兜住，不能让一个死控件拖垮整帧。
                alive = False
            if not alive:
                if done is None:
                    done = []
                done.append(t)
        if done:
            for t in done:
                try:
                    lane.remove(t)
                except ValueError:
                    pass
            for t in done:
                cb = getattr(t, "on_detached", None)
                if cb is not None:
                    try:
                        cb()
                    except Exception:                          # noqa: BLE001
                        pass

    # -- 观测 ---------------------------------------------------------------
    def stats(self):
        """给 --perf-hud / 自检用的快照。"""
        c = sorted(self._cost) or [0.0]
        n = len(c)
        return {
            "frames": self.frames,
            "running": self._timer.isActive(),
            "interval_ms": self._interval,
            "interactive": len(self._lanes[PRIO_INTERACTIVE]),
            "transition": len(self._lanes[PRIO_TRANSITION]),
            "ambient": len(self._lanes[PRIO_AMBIENT]),
            "ambient_enabled": self._ambient_on,
            "skipped_ambient": self.skipped_ambient,
            "last_ms": round(self.last_ms, 3),
            "p50_ms": round(c[n // 2], 3),
            "p95_ms": round(c[min(n - 1, int(n * 0.95))], 3),
            "peak_ms": round(self.peak_ms, 3),
        }

    def reset_stats(self):
        self.frames = 0
        self.skipped_ambient = 0
        self.last_ms = 0.0
        self.peak_ms = 0.0
        self._cost = []


# ---------------------------------------------------------------------------
# 弹簧
# ---------------------------------------------------------------------------
class Spring:
    """一维阻尼弹簧（物理动画的最小单元）。

    与"固定时长 + 曲线"的区别只有一条，但决定了手感：**改目标不重置速度**。
    连续点击时值是接力的，不是"停住 → 从当前值重新加速"。

    积分用半隐式欧拉 + 固定子步长（≤8.5ms）：显式欧拉在 dt 抖动时会发散
    （表现为过冲越来越大、最后开始振荡），而弹簧恰恰是"过冲"最显眼的动画。
    """

    #: 收敛判据：位移与速度都小到看不见了
    EPS = 1.2e-3

    def __init__(self, value=0.0, preset=motion.SPRING_JELLY,
                 on_change=None, prio=PRIO_INTERACTIVE, eps=None):
        self.value = float(value)
        self.target = self.value
        self.velocity = 0.0
        self.stiffness, self.damping = preset
        self.mass = 1.0
        self.prio = prio
        self._on_change = on_change
        self._eps = self.EPS if eps is None else float(eps)
        self._attached = False

    # -- 控制 ---------------------------------------------------------------
    def to(self, target, keep_velocity=True):
        """改目标。带速度接力 —— 这就是"可打断"的全部秘密。"""
        target = float(target)
        if not keep_velocity:
            self.velocity = 0.0
        self.target = target
        if abs(self.value - target) < self._eps and abs(self.velocity) < self._eps:
            self.value = target
            self.velocity = 0.0
            self._emit()
            return self
        if not self._attached:
            _clock().add(self, self.prio)
            self._attached = True
        return self

    def snap(self, value):
        """瞬间到位（首次布局、禁动画时用）。"""
        self.value = self.target = float(value)
        self.velocity = 0.0
        self._emit()
        return self

    def stop(self):
        self.target = self.value
        self.velocity = 0.0
        return self

    def overshoot(self):
        """这条弹簧从 0 到 1 的最大过冲比例（自检/调参用，一次数值积分）。"""
        x, v, peak = 0.0, 0.0, 0.0
        h = 1.0 / 240.0
        for _ in range(240 * 3):
            a = (self.stiffness * (1.0 - x) - self.damping * v) / self.mass
            v += a * h
            x += v * h
            peak = max(peak, x)
        return peak

    # -- 帧推进 -------------------------------------------------------------
    def step(self, dt):
        n = 1 if dt <= 0.0085 else min(6, int(dt / 0.0085) + 1)
        h = dt / n
        k, c, m = self.stiffness, self.damping, self.mass
        x, v = self.value, self.velocity
        for _ in range(n):
            v += ((k * (self.target - x) - c * v) / m) * h
            x += v * h
        self.value, self.velocity = x, v
        self._emit()
        if (abs(x - self.target) < self._eps
                and abs(v) < self._eps * 8.0):
            self.value = self.target
            self.velocity = 0.0
            self._emit()
            return False
        return True

    def on_detached(self):
        self._attached = False

    def _emit(self):
        if self._on_change is not None:
            self._on_change(self.value, self.velocity)


# ---------------------------------------------------------------------------
# 时间精确的过渡
# ---------------------------------------------------------------------------
class Tween:
    """按"时长 + 曲线"推进的过渡（不属于弹簧的场合用它）。

    典型用法：Toast 退场、弹窗进场 —— 这些要求"必须在 X ms 内结束"，
    弹簧的收敛时间只是近似值，不适合。
    """

    def __init__(self, frm, to, ms, easing=motion.CURVE_OUT,
                 on_change=None, on_done=None, prio=PRIO_TRANSITION):
        self.frm = float(frm)
        self.to = float(to)
        self.ms = max(1, int(ms))
        self.prio = prio
        self._curve = motion.curve(easing)
        self._t = 0.0
        self._on_change = on_change
        self._on_done = on_done
        self._attached = False

    def start(self):
        if not self._attached:
            _clock().add(self, self.prio)
            self._attached = True
        return self

    def step(self, dt):
        self._t += dt
        p = self._t / (self.ms / 1000.0)
        if p >= 1.0:
            if self._on_change is not None:
                self._on_change(self.to, 0.0)
            if self._on_done is not None:
                self._on_done()
            return False
        v = self.frm + (self.to - self.frm) * self._curve.valueForProgress(p)
        if self._on_change is not None:
            self._on_change(v, 0.0)
        return True

    def on_detached(self):
        self._attached = False


# ---------------------------------------------------------------------------
# 单例与便捷入口
# ---------------------------------------------------------------------------
_clock_obj = None


def _clock():
    global _clock_obj
    if _clock_obj is None:
        _clock_obj = FrameClock()
    return _clock_obj


def clock():
    return _clock()


def add_ambient(ticker):
    """注册一个装饰层 ticker（``step(dt) -> bool``）。"""
    return _clock().add(ticker, PRIO_AMBIENT)


def remove(ticker):
    _clock().remove(ticker)


def mark_busy(ms):
    _clock().mark_busy(ms)


def set_ambient_enabled(on):
    _clock().set_ambient_enabled(on)


def anim_time():
    """装饰动画该用的时间（秒，累计推进量）。

    与 `time.monotonic()` 的区别：闸门跳过 / 暂停期间它**不前进**，所以
    恢复时动画从原地继续，而不是瞬移到"现在本该在的位置"。

    ⚠️ 名字不能叫 `time`：会遮蔽本模块 `import time`，运行到
    `time.monotonic()` 时变成 `'function' object has no attribute` ——
    一接窗口就崩，且报错位置离真正原因很远。
    """
    return _clock()._anim_time


def stats():
    return _clock().stats()


def spring(value=0.0, preset=motion.SPRING_JELLY, on_change=None,
           prio=PRIO_INTERACTIVE):
    return Spring(value, preset, on_change, prio)
