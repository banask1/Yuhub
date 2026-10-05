"""Steam / GitHub hosts 加速页 —— 同一套「一键开关」交互（同 Steam++）。

用户要的是"像 Steam++ 一样，只需要开启和关闭"，所以这一版把原来
「加速模式分段 + 每服务一键优选/恢复默认」两套控制合并成**每服务一个开关**：

  本地反向代理（推荐，同 Steam++ 的形态）
    hosts 把加速域名指向 127.0.0.1，本机监听 443/80，按 TLS SNI / HTTP Host
    把流量转发到测速选出的真实节点。DNS 完全由我们接管，社区主站等
    "写 hosts 也不生效"的域名也能走通；不解密流量、无需证书。
  直连 hosts
    把测速最优的公网 IP 直接写进 hosts。最简单，但对 DNS 之外的问题
    （SNI 阻断等）无能为力。

**秒加速（v1.0.3）**：Steam++ 点一下就能用的原因，是它的启用路径上
**没有任何网络测量** —— hosts 只写 127.0.0.1，真实 IP 交给本地代理按
SNI 现场解析。这一版照做：

  打开开关（反向代理）→ 启动本地代理 → 立刻写 127.0.0.1（不测速）
                      → 后台预热"域名→IP"映射缓存（不挡启用）
  打开开关（直连）    → 上次优选缓存齐全时直接秒写；不齐才退化为先测速
  关闭开关            → 作废在途任务 → 立即清理（永不被"忙碌"挡住）

线程纪律（本项目硬规则）：引擎回调一律来自后台线程，回调里**只 emit 信号**，
所有界面改动都留在主线程槽函数里。代理解析回调发生在代理工作线程，
只碰 engine 侧的锁与缓存，绝不碰任何 QWidget。
"""

import threading

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
)

import hostsaccel as ha
import hostssniproxy as hp
from ..widgets import SegmentedControl, ToggleSwitch, info_card
from .base_page import BasePage

_MODE_PROXY = "proxy"
_MODE_DIRECT = "direct"

_SERVICE_META = {
    ha.SERVICE_STEAM: {
        "title": "Steam 加速",
        "desc": "商店、登录、社区、API 与下载/图片 CDN（含国内山海云节点）。",
    },
    ha.SERVICE_GITHUB: {
        "title": "GitHub 加速",
        "desc": "主站、API、代码下载（codeload / objects）与页面资源提速。",
    },
}


class AccelPage(BasePage):
    TINT_KEY = "tile_8"
    BADGE = "已上线"

    # 后台线程 → 主线程的信号（参数都是简单类型，跨线程安全）
    _domain_done = Signal(str, int, int, str, str, float, str)
    _measure_finished = Signal(str, bool, str, list)
    _write_finished = Signal(str, bool, str)
    _clean_finished = Signal(str, bool, str)
    _prefetch_finished = Signal(str, list)

    def __init__(self, notify=None, parent=None):
        super().__init__(
            "Steam / GitHub 加速",
            "与 Steam++（Watt Toolkit）同形态的本地加速：开启即写 hosts、立刻生效；"
            "本地反向代理按 SNI 把流量转发到最优节点，免代理直连。",
            icon="🚀",
            notify=notify,
            parent=parent,
        )
        # 每个服务独立的忙碌标记与最近一次优选结果
        self._busy = {ha.SERVICE_STEAM: False, ha.SERVICE_GITHUB: False}
        self._pending = {ha.SERVICE_STEAM: [], ha.SERVICE_GITHUB: []}
        # 取消事件（每个服务一份，每次"打开"换一个新的）。
        # 作用：用户把开关拨回"关"时 set 一下，在途的测速/预热线程看到就
        # 丢弃结果 —— 这是 v1.0.3 修「关不掉」的关键：关闭动作不再被
        # "正在测速"的忙碌状态挡住。
        self._cancel = {s: threading.Event() for s in ha.SERVICES}
        # 是否有提权写盘正在进行（决定"关闭"是直接清还是等写盘收尾再清）
        self._writing = {s: False for s in ha.SERVICES}
        # 后台预热映射缓存的状态（同一服务不并发跑两次）
        self._prefetch_running = {s: False for s in ha.SERVICES}
        # 优选映射缓存（domain -> 真实 IP），代理 resolver 的数据源
        self._ipmap = ha.load_map_cache()
        self._ipmap_lock = threading.Lock()
        self._proxy = None               # 惰性创建（SniProxy）

        self._domain_done.connect(self._on_domain_done)
        self._measure_finished.connect(self._on_measure_finished)
        self._write_finished.connect(self._on_write_finished)
        self._clean_finished.connect(self._on_clean_finished)
        self._prefetch_finished.connect(self._on_prefetch_finished)

        self._build_content()

    # ------------------------------------------------------- 开关与模式
    def _switch_for(self, svc):
        """取某服务的开关控件（测试与内部调用共用）。"""
        card = self._cards.get(svc)
        return getattr(card, "_switch", None) if card is not None else None

    def _sync_switch(self, svc):
        """把开关位置对齐到 hosts 的**真实状态**（程序改的，不触发信号）。

        这一点很重要：on_shown 自愈、写入完成、清理完成之后都要把开关
        重新对齐，否则会出现"hosts 已经加速了、开关还显示关着"的错位。
        """
        sw = self._switch_for(svc)
        if sw is None:
            return
        on = bool(ha.current_entries(svc))
        blocked = sw.blockSignals(True)
        sw.setChecked(on)
        sw.blockSignals(blocked)

    def _toggled(self, svc, on):
        """开关被用户点动：开 → 加速，关 → 恢复默认。

        ⚠️ **关闭动作永远不被忙碑挡住**。v1.0.2 的写法是"忙碌中一律把开关
        弹回真实状态"，配上"打开开关要跑十几秒测速"，就变成了用户报的
        「关不掉」：点关 → 开关弹回去 → 什么都不发生，得干等测速跑完。
        现在关闭会直接作废在途任务（见 _start_disable）。
        """
        if on:
            if self._busy[svc]:
                self._sync_switch(svc)       # 打开动作仍防重入
                return
            self._start_enable(svc)
        else:
            self._start_disable(svc)

    def _abort_job(self, svc):
        """作废该服务在途的测速/预热任务（线程看到事件就丢弃结果）。"""
        ev = self._cancel.get(svc)
        if ev is not None:
            ev.set()

    def _start_disable(self, svc):
        """关闭加速 —— 立即生效，不等任何在途任务。

        先 set 取消事件（在途测速线程会在回调前看到并闭嘴），再走清理。
        若此刻正在提权写盘（多半是卡在 UAC 弹窗上），先不抢着清理：
        让 _on_write_finished 以"关"为准补一刀，避免出现"开关显示关着、
        hosts 里却还留着条目"的错位。
        """
        self._abort_job(svc)
        self._busy[svc] = False
        if self._writing.get(svc):
            self._prefetch_running[svc] = False
            self._refresh_status()
            return
        self._start_clean(svc)

    # ------------------------------------------------------------ 秒加速
    def _start_enable(self, svc):
        """打开加速 —— **秒加速**入口（Steam++ 同形态）。

        代理模式（默认、也是推荐）
            不做**任何**测速：直接写 127.0.0.1 + 确保本地代理在跑，真实 IP
            由代理按 SNI 现场解析（缓存命中用缓存，未命中才 DoH+测速兜底）。
            启用路径上只有一次"改 hosts"，所以点一下立刻生效。
            映射缓存的预热放到**后台**，不挡启用。
        直连模式
            必须知道真实 IP，所以先看上次优选缓存：齐全就秒写；不齐只能
            退化为"先测速"（这一步躲不掉，但可以随时关掉取消）。

        另外：如果 hosts 里已经是我们要写的那套内容，连提权都省掉 ——
        重复拨开关是瞬时的，也不会反复弹 UAC。
        """
        if self._busy[svc]:
            return
        mode = self._current_mode()
        ev = threading.Event()
        self._cancel[svc] = ev
        self._busy[svc] = True
        card = self._cards[svc]

        if mode == _MODE_PROXY:
            ok, why = self._ensure_proxy()       # 代理必须先监听，再写 hosts
            if not ok:
                self._busy[svc] = False
                self._sync_switch(svc)
                self.toast("本地代理启动失败：%s。可切到「直连 hosts」模式" % why)
                self._refresh_status()
                return
            entries = [(ha.PROXY_IP, d) for d in ha.DOMAINS[svc]]
            self._pending[svc] = entries
            card._progress.setVisible(True)
            card._progress.setText("正在写入 hosts（%d 个域名，无需测速）…"
                                   % len(entries))
            self._write_entries(svc, entries, True, ev)
            self._prefetch_map(svc)
            return

        # ---- 直连模式：缓存齐全 → 秒写；不齐 → 只能先测速 ----
        entries, missing = ha.instant_entries(svc, _MODE_DIRECT, self._ipmap)
        if not missing and entries:
            self._pending[svc] = entries
            card._progress.setVisible(True)
            card._progress.setText("正在写入 hosts（%d 个域名，来自上次优选缓存）…"
                                   % len(entries))
            self._write_entries(svc, entries, False, ev)
            self._prefetch_map(svc)
            return
        self._busy[svc] = False
        self._start_optimize(svc)

    def _write_entries(self, svc, entries, proxy_mode, cancel):
        """把条目落盘：已在管理员 → 直写；否则提权。

        提权要等 UAC，一律放后台线程里轮询，绝不阻塞主线程。
        """
        card = self._cards[svc]
        # 内容已经一致 → 不写盘、不弹 UAC（重复拨开关因此也是瞬时的）
        if ha.entries_match(ha.current_entries(svc), entries):
            ha.flush_dns_cache()
            self._on_write_finished(svc, True, "")
            return
        if ha.is_admin():
            res = ha.try_write_direct("write", svc, entries, proxy=proxy_mode)[1]
            if res and res.get("ok"):
                ha.flush_dns_cache()
                self._on_write_finished(svc, True, "")
            else:
                self._on_write_finished(
                    svc, False, (res or {}).get("error") or "写入 hosts 失败")
            return

        card._progress.setText("正在写入系统 hosts（请在 UAC 弹窗中确认）…")
        self._writing[svc] = True

        def work():
            # 引擎/提权都在后台线程：只 emit 信号，界面改动全在主线程槽
            res = ha.run_elevated("write", svc, entries, proxy=proxy_mode)
            if res is None:
                self._write_finished.emit(svc, False, "未完成写入（取消了 UAC 或超时）")
                return
            if res.get("ok"):
                ha.flush_dns_cache()
                self._write_finished.emit(svc, True, "")
            else:
                self._write_finished.emit(
                    svc, False, res.get("error") or "写入 hosts 失败")

        threading.Thread(target=work, name="YuhubHostsWrite", daemon=True).start()

    def _prefetch_map(self, svc):
        """后台预热"域名 → 真实 IP"映射缓存（代理按 SNI 查的就是它）。

        纯后台、不需提权、失败也无所谓 —— 代理未命中缓存时会现场解析。
        缓存又新又全时直接跳过，免得每次拨开关都白跑一遍测速。
        """
        if self._prefetch_running.get(svc):
            return
        with self._ipmap_lock:
            complete = all(self._ipmap.get(d) for d in ha.DOMAINS[svc])
        if complete and ha.map_cache_fresh():
            return
        self._prefetch_running[svc] = True

        def work():
            try:
                pairs = [(ip, d) for d, ip in ha.optimize_service_map(svc).items()]
            except Exception:
                pairs = []
            self._prefetch_finished.emit(svc, pairs)

        threading.Thread(target=work, name="YuhubHostsPrefetch",
                         daemon=True).start()

    def _on_prefetch_finished(self, svc, pairs):
        self._prefetch_running[svc] = False
        if pairs:
            with self._ipmap_lock:
                for ip, d in pairs:
                    self._ipmap[d] = ip
            ha.save_map_cache(self._ipmap)
            self._refresh_status()

    # ---------------------------------------------------------------- 代理
    def _proxy_resolver(self, domain):
        """给 SniProxy 的回调：domain -> ip or None。

        在代理工作线程里跑 —— 只碰锁和引擎函数，绝不碰界面。
        缓存未命中时现场 DoH 解析 + 测速兜底（比拒绝连接强得多）。
        """
        with self._ipmap_lock:
            ip = self._ipmap.get(domain)
        if ip and ha.is_valid_public_ipv4(ip):
            return ip
        svc = None
        for s in ha.SERVICES:
            if domain in ha.DOMAINS[s]:
                svc = s
                break
        if svc is None:
            return None
        res = ha.optimize_domain(domain, svc)
        if not res:
            return None
        with self._ipmap_lock:
            self._ipmap[domain] = res[0]
        ha.save_map_cache(self._ipmap)
        return res[0]

    def _ensure_proxy(self):
        """确保本地代理在跑（幂等）。返回 (ok, 原因) —— 只在主线程调用。"""
        if self._proxy is not None and self._proxy.running:
            return True, ""
        if self._proxy is None:
            self._proxy = hp.SniProxy(self._proxy_resolver)
        ok, why = self._proxy.start()
        return ok, why

    def _proxy_running(self):
        return self._proxy is not None and self._proxy.running

    def _stop_proxy(self):
        if self._proxy is not None:
            self._proxy.stop()

    # ---------------------------------------------------------------- 构建
    def _build_content(self):
        # ---- 加速模式（全局一行，决定"打开开关"时走哪条链路）----
        mode_card = QFrame()
        mode_card.setObjectName("Card")
        mv = QVBoxLayout(mode_card)
        mv.setContentsMargins(18, 16, 18, 16)
        mv.setSpacing(8)
        mt = QLabel("加速模式")
        mt.setObjectName("CardTitle")
        mv.addWidget(mt)
        self._mode_seg = SegmentedControl(
            [(_MODE_PROXY, "本地反向代理（推荐）"), (_MODE_DIRECT, "直连 hosts")],
            current=_MODE_PROXY)
        self._mode_seg.changed.connect(self._on_mode_changed)
        mv.addWidget(self._mode_seg)
        self._mode_desc = QLabel("")
        self._mode_desc.setObjectName("CardDesc")
        self._mode_desc.setWordWrap(True)
        mv.addWidget(self._mode_desc)
        self._proxy_status = QLabel("")
        self._proxy_status.setObjectName("Muted")
        self._proxy_status.setWordWrap(True)
        mv.addWidget(self._proxy_status)
        self.add(mode_card)
        self._update_mode_desc()

        # ---- 每个服务一块卡片（卡片里只有一个开关）----
        self._cards = {}
        for svc in ha.SERVICES:
            card = self._build_service_card(svc)
            self.add(card)
            self._cards[svc] = card

        self.add(info_card(
            "这是怎么工作的",
            "国内运营商的 UDP 53 端口 DNS 可能返回被污染的假 IP。反向代理模式下，"
            "hosts 把域名指向 127.0.0.1，本机代理按 SNI 把流量转发到最优真实节点"
            "（同 Steam++ 的做法，不解密流量、无需证书）。因为启用时不需要先测速，"
            "开关是点一下就生效的；真实 IP 由代理现场用 DoH（加密 DNS）解析并做 "
            "TCP 443 握手测速，结果缓存下来复用。直连模式则把真实 IP 直接写进 hosts，"
            "有缓存时同样秒开。写 hosts 前自动备份到 %LOCALAPPDATA%\\Yuhub\\hosts_backup，"
            "写后自动刷新 DNS 缓存。修改 hosts 需要管理员权限，打开开关时会弹一次 "
            "UAC 确认；如果 Yuhub 本身就以管理员身份运行，则不再重复弹窗；"
            "hosts 内容已经是目标状态时也不会重复写入。",
        ))
        self.add(info_card(
            "能力边界（先说清楚，免得误会）",
            "① 反向代理模式接管了 DNS 解析：Steam 商店/登录/图片、GitHub 主站/"
            "API/下载等绝大多数站点可免代理直连（已实测真实 TLS 握手通过）；"
            "但 steamcommunity.com 社区主站存在 **SNI 层阻断**——即使拿到正确"
            " IP，TLS 握手也会被重置，透传代理不装证书不解密，绕不过这一层，"
            "完全访问仍需专业代理工具。② 反向代理模式会占用本机 443/80 端口："
            "与 Steam++ 等同类工具同时开加速会冲突，二选一即可。③ 退出 Yuhub"
            " 时会自动停代理并提示恢复 hosts。测速结果缓存在本地，觉得变慢了"
            "把开关关掉再打开就是一次重新优选。",
        ))
        self.add_stretch()
        self._refresh_status()

    def _build_service_card(self, svc):
        meta = _SERVICE_META[svc]
        card = QFrame()
        card.setObjectName("Card")
        v = QVBoxLayout(card)
        v.setContentsMargins(18, 16, 18, 16)
        v.setSpacing(10)

        # 标题行：左边标题，右边开关（同 Steam++ 的卡片形态）
        head = QHBoxLayout()
        head.setSpacing(10)
        title = QLabel(meta["title"])
        title.setObjectName("CardTitle")
        head.addWidget(title)
        head.addStretch(1)
        switch = ToggleSwitch(False)
        switch.setToolTip("开启后自动优选并写入 hosts；关闭即恢复默认")
        switch.toggled.connect(lambda on, s=svc: self._toggled(s, on))
        head.addWidget(switch)
        v.addLayout(head)

        desc = QLabel(meta["desc"])
        desc.setObjectName("CardDesc")
        desc.setWordWrap(True)
        v.addWidget(desc)

        # 当前状态（on_shown / 写入完成后刷新）
        status = QLabel("")
        status.setObjectName("Muted")
        status.setWordWrap(True)
        v.addWidget(status)

        # 优选结果展示（等宽字体，一行一个域名）
        entries = QLabel("")
        entries.setObjectName("Faint")
        font = QFont()
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setFamily("Consolas")
        font.setPointSize(8)
        entries.setFont(font)
        entries.setWordWrap(True)
        entries.setTextInteractionFlags(Qt.TextSelectableByMouse)
        entries.setVisible(False)
        v.addWidget(entries)

        # 进度行（测速中逐域名更新）
        progress = QLabel("")
        progress.setObjectName("Faint")
        progress.setWordWrap(True)
        progress.setVisible(False)
        v.addWidget(progress)

        card._status = status
        card._entries = entries
        card._switch = switch
        card._progress = progress
        return card

    # ------------------------------------------------------------ 模式切换
    def _on_mode_changed(self, value):
        self._update_mode_desc()
        if value == _MODE_DIRECT and self._proxy_running():
            # 还有反代条目在用 127.0.0.1 时切直连：提示重新优选，避免半新半旧
            self.toast("已切到直连模式：对已有服务请重新「一键优选加速」以写入真实 IP")
        self._refresh_status()

    def _current_mode(self):
        try:
            if self._mode_seg._buttons[_MODE_PROXY].isChecked():
                return _MODE_PROXY
        except (KeyError, RuntimeError):
            pass
        return _MODE_DIRECT

    def _update_mode_desc(self):
        if self._current_mode() == _MODE_PROXY:
            self._mode_desc.setText(
                "hosts 指向 127.0.0.1，本机监听 443/80，按 SNI 转发到最优节点"
                "（同 Steam++）。开启即时生效：不预先测速，真实 IP 由本地"
                "代理现场解析。接管 DNS，社区等站点也能访问；不解密流量、无需证书。")
        else:
            self._mode_desc.setText(
                "把测速最优的公网 IP 直接写进 hosts。最简单、不占端口；"
                "有上次优选缓存时开启是即时的，否则需要先测量一次。")

    # ------------------------------------------------------------ 状态展示
    def _refresh_status(self):
        # 代理状态行
        if self._proxy_running():
            st = self._proxy.stats()
            self._proxy_status.setText(
                "本地代理运行中（127.0.0.1:443 / :80）—— 已转发 %d 条连接"
                % st["conns"])
        else:
            why = ""
            if self._proxy is not None:
                why = self._proxy.stats().get("last_error") or ""
            self._proxy_status.setText(
                "本地代理未运行。" + (why if why else ""))

        for svc, card in self._cards.items():
            entries = ha.current_entries(svc)
            if entries:
                mode = ha.entries_mode(entries)
                mode_txt = ("本地反向代理（127.0.0.1）" if mode == "proxy"
                            else "直连（真实 IP）")
                card._status.setText(
                    "当前状态：已加速 %d 个域名（%s，来自上次优选）"
                    % (len(entries), mode_txt))
                card._entries.setVisible(True)
                card._entries.setText("\n".join(
                    "%-16s %s" % (ip, d) for ip, d in entries))
            else:
                card._status.setText("当前状态：未加速（系统 hosts 里没有本服务条目）")
                card._entries.setVisible(False)
                card._entries.setText("")
            # 开关位置永远对齐 hosts 的真实状态（程序改的，不触发信号）
            self._sync_switch(svc)

    def on_shown(self):
        # hosts 文件可能被别的工具改过，每次进页都重读
        self._refresh_status()
        # 自愈：上次退出没来得及恢复的 127.0.0.1 条目还在 hosts 里，
        # 代理若没在跑，这些域名现在全是死路 —— 立刻把代理拉起来
        need_proxy = False
        for svc in ha.SERVICES:
            if ha.entries_mode(ha.current_entries(svc)) == "proxy":
                need_proxy = True
                break
        if need_proxy and not self._proxy_running():
            ok, why = self._ensure_proxy()
            if ok:
                self.toast("检测到反向代理模式的加速条目，本地代理已自动启动")
            else:
                self.toast("警告：hosts 有 127.0.0.1 加速条目但代理启动失败（%s）"
                           % why)
        self._refresh_status()

    # ------------------------------------------------------------ 优选流程
    def _start_optimize(self, svc):
        """直连模式且缓存不全时的兜底：先测速，测完再写真实 IP。

        直连模式必须知道真实 IP，所以没有缓存时躲不掉这一步 —— 但整个流程
        **可取消**：用户把开关拨回"关"时 cancel 事件会被 set，回调里直接
        丢弃结果，同时进度行也不再刷（避免"关了还在滚"的错觉）。
        """
        if self._busy[svc]:
            return
        self._busy[svc] = True
        self._pending[svc] = []
        self._cancel.setdefault(svc, threading.Event())
        ev = self._cancel[svc]
        card = self._cards[svc]
        card._progress.setVisible(True)
        card._progress.setText("正在解析与测速（0/%d）…" % len(ha.DOMAINS[svc]))

        def on_domain(done, total, domain, ip, ms, error):
            # 引擎回调在工作线程：只发信号，绝不碰界面
            if ev.is_set():
                return
            self._domain_done.emit(svc, done, total, domain, ip, ms, error)

        def work():
            entries = ha.optimize_service(svc, on_domain=on_domain)
            if ev.is_set():
                return                       # 用户已经关掉了，结果作废
            ok = bool(entries)
            msg = "" if ok else "未能测出任何可用节点，请检查网络连接"
            self._measure_finished.emit(svc, ok, msg, entries)

        threading.Thread(target=work, name="YuhubHostsOpt", daemon=True).start()

    def _on_domain_done(self, svc, done, total, domain, ip, ms, error):
        # 主线程槽：更新进度行
        card = self._cards[svc]
        if error:
            card._progress.setText("正在解析与测速（%d/%d）… %s ✗" % (done, total, domain))
        else:
            card._progress.setText(
                "正在解析与测速（%d/%d）… %s → %s（%.0fms）" % (done, total, domain, ip, ms))

    def _on_measure_finished(self, svc, ok, msg, entries):
        card = self._cards[svc]
        if not ok:
            self._busy[svc] = False
            card._progress.setVisible(False)
            self._sync_switch(svc)
            self.toast(msg)
            return
        proxy_mode = (self._current_mode() == _MODE_PROXY)
        if proxy_mode and not self._proxy_running():
            # 兜底：代理必须先在监听，hosts 才能指向 127.0.0.1
            #（防代理中途挂掉 / 直接走本方法的重试路径）。
            ok2, why2 = self._ensure_proxy()
            if not ok2:
                self._busy[svc] = False
                card._progress.setVisible(False)
                self._sync_switch(svc)
                self.toast("本地代理启动失败（%s），未写入 hosts。"
                           "可切到「直连 hosts」模式" % why2)
                self._refresh_status()
                return
        if proxy_mode:
            # 真实 IP 进映射缓存（代理按 SNI 查的就是它）
            with self._ipmap_lock:
                for ip, d, _ms in entries:
                    self._ipmap[d] = ip
            ha.save_map_cache(self._ipmap)
            write_entries = [(ha.PROXY_IP, d) for _ip, d, _ms in entries]
        else:
            write_entries = [(ip, d) for ip, d, _ms in entries]
        self._pending[svc] = write_entries
        card._progress.setText("优选完成，正在写入系统 hosts…")
        self._cancel.setdefault(svc, threading.Event())
        self._write_entries(svc, write_entries, proxy_mode, self._cancel[svc])

    def _on_write_finished(self, svc, ok, msg):
        card = self._cards[svc]
        card._progress.setVisible(False)
        self._writing[svc] = False
        ev = self._cancel.get(svc)
        if ev is not None and ev.is_set():
            # 写入途中用户把开关拨到"关"了：以"关"为准，立刻补一刀清理 ——
            # 否则会出现"开关显示关着、hosts 里还留着条目"的错位。
            self._busy[svc] = False
            self._start_clean(svc, quiet=True)
            return
        self._busy[svc] = False
        if ok:
            n = len(self._pending[svc])
            mode_txt = ("本地反向代理" if self._current_mode() == _MODE_PROXY
                        else "直连")
            self.toast("加速已开启：%d 个域名已写入 hosts（%s模式），DNS 缓存已刷新"
                       % (n, mode_txt))
        else:
            self.toast("未能开启加速：%s。开关已保持为当前真实状态，"
                       "请重试一次并在 UAC 弹窗里点「是」。" % (msg or "未知原因"))
        self._refresh_status()

    # ------------------------------------------------------------ 恢复默认
    def _start_clean(self, svc, quiet=False):
        """关掉加速：清掉本服务的 hosts 条目 + 不再需要时停代理。

        quiet=True 用于"写入途中被用户关掉"的补刀场景 —— 这时不该再弹
        "已是默认状态"这种提示，用户只关心"关掉了没有"。
        """
        if self._busy[svc]:
            return
        entries = ha.current_entries(svc)
        if not entries:
            # 本来就没有条目：不需要任何权限，直接报成功（旧版这里被提权入口挡住了）
            if not quiet:
                self.toast("本服务当前没有加速条目，已是默认状态")
            self._refresh_status()
            return
        self._busy[svc] = True
        card = self._cards[svc]
        card._progress.setVisible(True)

        # 已是管理员 → 直接清，不弹 UAC
        if ha.is_admin():
            card._progress.setText("正在恢复默认…")
            res = ha.try_write_direct("clean", svc)[1]
            if res and res.get("ok"):
                ha.flush_dns_cache()
                self._on_clean_finished(svc, True, "")
            else:
                self._on_clean_finished(svc, False,
                                        (res or {}).get("error") or "清理 hosts 失败")
            return

        card._progress.setText("正在恢复默认（请在 UAC 弹窗中确认）…")

        def work():
            res = ha.run_elevated("clean", svc)
            if res is None:
                self._clean_finished.emit(svc, False, "未完成（取消了 UAC 或超时）")
            elif res.get("ok"):
                ha.flush_dns_cache()
                self._clean_finished.emit(svc, True, "")
            else:
                self._clean_finished.emit(svc, False,
                                          res.get("error") or "清理 hosts 失败")

        threading.Thread(target=work, name="YuhubHostsClean", daemon=True).start()

    def _on_clean_finished(self, svc, ok, msg):
        self._busy[svc] = False
        card = self._cards[svc]
        card._progress.setVisible(False)
        if ok:
            self.toast("已恢复默认")
        else:
            # 关不掉时必须说清楚"为什么关不掉、现在是什么状态"——
            # 只弹一句"未完成"的话，用户会以为开关坏了（而且开关确实会
            # 因为 hosts 还有条目而弹回"开"，看起来像点击无效）。
            self.toast("未能关闭加速：%s。开关已保持为当前真实状态，"
                       "请重试一次并在 UAC 弹窗里点「是」。" % (msg or "未知原因"))
        # 两个服务都没有代理模式条目了 → 停掉代理（端口让出来）
        still_proxy = any(
            ha.entries_mode(ha.current_entries(s)) == "proxy"
            for s in ha.SERVICES)
        if not still_proxy and self._proxy_running():
            self._stop_proxy()
        self._refresh_status()

    # ------------------------------------------------------------ 退出收尾
    def shutdown(self):
        """真退出前被 MainWindow.teardown() 调用。

        停代理 + 把**反代模式**的条目清掉（否则 hosts 指着 127.0.0.1、
        代理死了，这些域名就全断了）。清理由提权进程完成 —— UAC 会在退出
        时弹一次；用户取消也没关系，下次启动 on_shown 的自愈逻辑会把代理
        重新拉起来。直连模式的条目不动（它们不依赖本机代理）。
        """
        self._stop_proxy()
        for svc in ha.SERVICES:
            if ha.entries_mode(ha.current_entries(svc)) == "proxy":
                threading.Thread(
                    target=ha.run_elevated, args=("clean", svc),
                    name="YuhubHostsExitClean", daemon=True).start()
