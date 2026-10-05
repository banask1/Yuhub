"""Steam / GitHub hosts 加速页 —— 本地反向代理 + 「意图应用」状态机。

形态与 Steam++（Watt Toolkit）一致：**每服务一个开关**，hosts 把加速域名
指向 127.0.0.1，本机监听 443/80，按 TLS SNI / HTTP Host 把流量转发到
DoH 解析出的真实节点。DNS 由我们接管，社区主站等"写 hosts 也不生效"
的域名也能走通；不解密流量、无需证书。

**秒加速（v1.0.3 起）**：启用路径上没有任何网络测量 —— 直接写 127.0.0.1，
真实 IP 由本地代理按 SNI 现场解析（缓存命中用缓存，未命中才 DoH+测速
兜底）。测速只发生在后台预热线程里，不挡开关。

**为什么开/关统一走"意图文件"（v1.0.4，修「关不掉」的架构级方案）**

以前"开"和"关"是两条独立的提权操作，各弹各的 UAC，踩过三个坑：
  1. "开启的 UAC 还没点"时用户关掉开关 → 旧实现静默早退 + 开关被
     _refresh_status 弹回 hosts 真实状态（开）→ 用户看到"点了没反应"；
  2. 用户自然会再点一次 → 此时忙碌标记已被清掉，第二次点击触发**反向
     操作**（再开一次），状态机分叉、UAC 接连弹；
  3. "写入途中被关"要补一刀清理 → 第二次 UAC，用户懵了取消掉 → hosts
     留着刚写的条目，开关弹回"开" → 彻底"关不掉"。

现在的状态机：

  拨开关 → 更新会话期望态 _intent_state → 写意图文件（seq 递增）
         → 保证**恰好一个**"应用"在跑（管理员直写 / 提权子进程）

  应用在跑时再拨开关 → 只更新意图文件（提权子进程带 0.6s 稳定窗，
                       会吸收"UAC 等待期间改的主意"，同一次 UAC 按
                       最新意图执行 —— 永不分叉、永不多弹）
  应用完成 → seq 落后于意图文件就再补一轮；否则按结果对齐开关/提示

开关位置永远显示**用户意图**（不是 hosts 真实状态），杜绝"点了又被
弹回去"的错觉；hosts 真实状态由每张卡片的状态行如实展示。

线程纪律（本项目硬规则）：应用/预热的引擎调用都在后台线程，回调里
**只 emit 信号**，所有界面改动都留在主线程槽函数里。
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
from ..widgets import ToggleSwitch, info_card
from .base_page import BasePage

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
    _apply_finished = Signal(dict)
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
        # 会话内期望态快照：初始化成 hosts 真实状态。每次拨开关更新对应
        # 服务并覆盖写进意图文件 —— 它是开/关的唯一事实来源。
        self._intent_state = {s: bool(ha.current_entries(s)) for s in ha.SERVICES}
        # 意图文件最近一次写入的 seq（与提权结果里的 seq 比对用）
        self._intent_seq = 0
        # 是否有一个"应用"在跑（覆盖两个服务的全局一份，串行化一切操作）
        self._apply_running = False
        # 后台预热映射缓存的状态（同一服务不并发跑两次）
        self._prefetch_running = {s: False for s in ha.SERVICES}
        # 优选映射缓存（domain -> 真实 IP），代理 resolver 的数据源
        self._ipmap = ha.load_map_cache()
        self._ipmap_lock = threading.Lock()
        self._proxy = None               # 惰性创建（SniProxy）

        self._apply_finished.connect(self._on_apply_finished)
        self._prefetch_finished.connect(self._on_prefetch_finished)

        self._build_content()

    # ------------------------------------------------------- 开关与状态
    def _switch_for(self, svc):
        """取某服务的开关控件（测试与内部调用共用）。"""
        card = self._cards.get(svc)
        return getattr(card, "_switch", None) if card is not None else None

    def _set_switch(self, svc, checked):
        """程序改开关位置（不触发 toggled 信号）。"""
        sw = self._switch_for(svc)
        if sw is None:
            return
        blocked = sw.blockSignals(True)
        sw.setChecked(bool(checked))
        sw.blockSignals(blocked)

    def _sync_switch(self, svc):
        """开关对齐**用户意图**（而不是 hosts 真实状态）。

        这是「关不掉」修复的一半：拨了开关就被尊重、停在哪就显示哪，
        绝不弹回。hosts 真实状态由状态行如实展示；应用失败时由
        _resync_intent 把意图对齐回真实状态（伴随明确报错提示）。
        """
        self._set_switch(svc, self._intent_state.get(svc, False))

    def _state_matches(self, svc):
        """意图是否已经和 hosts 真实状态一致（一致就不用发起应用）。"""
        want = self._intent_state.get(svc, False)
        entries = ha.current_entries(svc)
        if want:
            # 开：要求是"全量代理区块"（幂等判据，与 apply_intent 一致）
            return (ha.entries_mode(entries) == "proxy"
                    and [d for _ip, d in entries] == list(ha.DOMAINS[svc]))
        # 关：既没有条目也没有残留的区块标记
        return not entries and not ha.service_enabled(svc)

    def _resync_intent(self, svcs=None):
        """把意图对齐回 hosts 真实状态（应用失败后用，防开关与事实错位）。"""
        for s in (svcs or ha.SERVICES):
            self._intent_state[s] = bool(ha.current_entries(s))
            self._sync_switch(s)

    def _toggled(self, svc, on):
        """开关被用户点动：更新意图 → 写意图文件 → 保证有一个应用在跑。"""
        if on:
            # 代理必须先监听，hosts 才能指向 127.0.0.1（顺序错了会断网）
            ok, why = self._ensure_proxy()
            if not ok:
                self._set_switch(svc, False)     # 拨回去（意图没变）
                self.toast("本地代理启动失败：%s" % why)
                self._refresh_status()
                return
        self._intent_state[svc] = on
        seq = ha.write_intent(self._intent_state)
        if seq < 0:
            self._resync_intent([svc])
            self.toast("无法记录加速意图（本地文件不可写），操作未执行")
            return
        self._intent_seq = seq
        if self._state_matches(svc):
            # 已经是目标状态：不提权、不弹 UAC（重复拨开关因此是瞬时的）
            self._update_card(svc)
            self.toast("已是%s状态，无需改动" % ("开启" if on else "默认"))
            self._refresh_status()
            return
        self._show_apply_progress()
        self._ensure_apply()

    # ------------------------------------------------------------ 应用
    def _ensure_apply(self):
        """保证恰好一个"应用"在跑。在跑就什么都不做 —— 那一次会应用
        最新意图（子进程稳定窗吸收）；它收尾后若 seq 落后会自动补一轮。
        """
        if self._apply_running:
            return
        self._apply_running = True
        if ha.is_admin():
            threading.Thread(target=self._apply_worker_direct,
                             name="YuhubHostsApply", daemon=True).start()
        else:
            threading.Thread(target=self._apply_worker_elevated,
                             name="YuhubHostsApplyElev", daemon=True).start()

    def _apply_worker_direct(self):
        """管理员：当前进程直接应用（不弹 UAC）。"""
        intent = ha.read_intent() or dict(self._intent_state, seq=self._intent_seq)
        res = ha.apply_intent(intent)
        self._apply_finished.emit(res)

    def _apply_worker_elevated(self):
        """非管理员：提权子进程应用最新意图（等 UAC，绝不阻塞主线程）。"""
        res = ha.run_elevated_apply()
        if res is None:
            res = {"ok": False, "error": "未完成（当前系统不支持提权）", "seq": 0}
        self._apply_finished.emit(res)

    def _show_apply_progress(self):
        """给"意图与真实状态不一致"的卡片挂进度行（开启/关闭措辞分开）。"""
        for svc in ha.SERVICES:
            card = self._cards[svc]
            want = self._intent_state[svc]
            have = bool(ha.current_entries(svc))
            if want and not have:
                card._progress.setVisible(True)
                card._progress.setText("正在开启加速（首次需要管理员授权）…")
            elif not want and have:
                card._progress.setVisible(True)
                card._progress.setText("正在关闭加速，恢复系统默认…")
            elif self._apply_running and card._progress.isVisible():
                card._progress.setText("正在应用最新意图…")

    def _on_apply_finished(self, res):
        """应用收尾：对齐开关、报结果、必要时补一轮。"""
        self._apply_running = False
        for svc in ha.SERVICES:
            self._cards[svc]._progress.setVisible(False)

        # 应用期间意图又变了（子进程稳定窗没吸收到，极少见）：补一轮
        cur = ha.read_intent()
        if cur and cur.get("seq", 0) > (res.get("seq") or 0):
            self._show_apply_progress()
            self._ensure_apply()
            return

        services = res.get("services") or {}
        if not services or not res.get("ok"):
            # 整体失败（取消 UAC / 超时 / 意图不可读）：如实报错 + 意图
            # 对齐回真实状态（开关与 hosts 保持一致，绝不假装成功）
            self._resync_intent()
            self.toast("操作未完成：%s。 hosts 未被改动，可重试。"
                       % (res.get("error") or "未知原因"))
            self._refresh_status()
            return

        changed = []
        for svc, sub in services.items():
            if not sub.get("ok"):
                self._resync_intent([svc])
                self.toast("%s：未能%s（%s）"
                           % (_SERVICE_META[svc]["title"],
                              "开启" if sub.get("action") == "write" else "关闭",
                              sub.get("error") or "未知原因"))
                continue
            if sub.get("action") in ("write", "clean"):
                changed.append(svc)
        if changed:
            ha.flush_dns_cache()
            names = "、".join(_SERVICE_META[s]["title"].replace(" 加速", "")
                              for s in changed)
            actions = [services[s]["action"] for s in changed]
            verb = ("已开启" if actions[0] == "write" else "已关闭")
            self.toast("%s加速%s，DNS 缓存已刷新" % (names, verb))
        self._refresh_status()
        self._maybe_stop_proxy()

    def _maybe_stop_proxy(self):
        """两个服务都没有代理条目了 → 停代理（端口让出来）。"""
        still_proxy = any(
            ha.entries_mode(ha.current_entries(s)) == "proxy"
            for s in ha.SERVICES)
        if not still_proxy and self._proxy_running():
            self._stop_proxy()

    def _prefetch_map(self, svc):
        """后台预热"域名 → 真实 IP"映射缓存（代理按 SNI 查的就是它）。

        纯后台、不需提权、失败也无所谓 —— 代理未命中缓存时会现场解析。
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
        # ---- 代理状态行（本地代理是这套加速的心脏，状态要一直可见）----
        self._proxy_status = QLabel("")
        self._proxy_status.setObjectName("Muted")
        self._proxy_status.setWordWrap(True)
        self.add(self._proxy_status)

        # ---- 每个服务一块卡片（卡片里只有一个开关，同 Steam++ 形态）----
        self._cards = {}
        for svc in ha.SERVICES:
            card = self._build_service_card(svc)
            self.add(card)
            self._cards[svc] = card

        self.add(info_card(
            "这是怎么工作的",
            "国内运营商的 UDP 53 端口 DNS 可能返回被污染的假 IP。本功能把加速"
            "域名的 hosts 指向 127.0.0.1，本机代理按 TLS SNI 把流量转发到真实"
            "最优节点（同 Steam++ 的做法，不解密流量、无需证书）。真实 IP 由"
            "代理现场用 DoH（加密 DNS）解析并做 TCP 443 握手测速，结果缓存"
            "下来复用。开启/关闭只需一次管理员授权：授权等待期间再拨开关，"
            "同一次授权会按最新的开关状态执行。写 hosts 前自动备份到"
            " %LOCALAPPDATA%\\Yuhub\\hosts_backup，写后自动刷新 DNS 缓存；"
            "hosts 已是目标状态时不会重复写盘。",
        ))
        self.add(info_card(
            "能力边界（先说清楚，免得误会）",
            "① 本功能接管了加速域名的 DNS 解析：Steam 商店/登录/图片、"
            "GitHub 主站/API/下载等绝大多数站点可免代理直连（已实测真实 "
            "TLS 握手通过）；但 steamcommunity.com 社区主站存在 **SNI 层"
            "阻断**——即使拿到正确 IP，TLS 握手也会被重置，透传代理不装"
            "证书不解密，绕不过这一层，完全访问仍需专业代理工具。"
            "② 会占用本机 443/80 端口：与 Steam++ 等同类工具同时开加速"
            "会冲突，二选一即可。③ 退出 Yuhub 时会自动停代理并提示恢复"
            " hosts。",
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
        switch.setToolTip("开启后 hosts 指向本机代理；关闭即恢复系统默认")
        switch.toggled.connect(lambda on, s=svc: self._toggled(s, on))
        head.addWidget(switch)
        v.addLayout(head)

        desc = QLabel(meta["desc"])
        desc.setObjectName("CardDesc")
        desc.setWordWrap(True)
        v.addWidget(desc)

        # 当前状态（on_shown / 应用完成后刷新）—— 如实展示 hosts 真实状态
        status = QLabel("")
        status.setObjectName("Muted")
        status.setWordWrap(True)
        v.addWidget(status)

        # 当前条目展示（等宽字体，一行一个域名）
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

        # 进度行（应用中 / 预热中更新）
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

    # ------------------------------------------------------------ 状态展示
    def _update_card(self, svc):
        """刷新单张卡片的"当前状态"行与条目展示（hosts 真实状态）。"""
        card = self._cards[svc]
        entries = ha.current_entries(svc)
        if entries:
            # v1.0.3 及以前写过"直连（真实 IP）"条目，升级后如实标注
            mode = ha.entries_mode(entries)
            mode_txt = ("hosts 指向本机代理" if mode == "proxy"
                        else "直连真实 IP（旧版写入，建议关闭后重开）")
            card._status.setText(
                "当前状态：已加速 %d 个域名（%s）" % (len(entries), mode_txt))
            card._entries.setVisible(True)
            card._entries.setText("\n".join(
                "%-16s %s" % (ip, d) for ip, d in entries))
        else:
            card._status.setText("当前状态：未加速（系统 hosts 里没有本服务条目）")
            card._entries.setVisible(False)
            card._entries.setText("")

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

        for svc in ha.SERVICES:
            self._update_card(svc)
            # 开关对齐意图（不是 hosts 真实状态 —— 见 _sync_switch）
            self._sync_switch(svc)

    def on_shown(self):
        # hosts 文件可能被别的工具改过，每次进页都重读真实状态；
        # 会话内没有进行中的应用时，意图跟着真实状态走（对齐外部改动）
        if not self._apply_running:
            self._resync_intent()
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
                self.toast("检测到加速条目，本地代理已自动启动")
            else:
                self.toast("警告：hosts 有 127.0.0.1 加速条目但代理启动失败（%s）"
                           % why)
        self._refresh_status()

    # ------------------------------------------------------------ 退出收尾
    def shutdown(self):
        """真退出前被 MainWindow.teardown() 调用。

        停代理 + 把加速条目清掉（否则 hosts 指着 127.0.0.1、代理死了，
        这些域名就全断了）。清理由提权进程完成 —— UAC 会在退出时弹一次；
        用户取消也没关系，下次启动 on_shown 的自愈逻辑会把代理重新拉起来。
        """
        self._stop_proxy()
        dirty = {s: False for s in ha.SERVICES
                 if self._intent_state.get(s) or ha.current_entries(s)}
        if not dirty:
            return
        ha.write_intent(dirty)
        threading.Thread(target=ha.run_elevated_apply,
                         name="YuhubHostsExitApply", daemon=True).start()
