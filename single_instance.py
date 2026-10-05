"""单实例守卫：只允许一个 Yuhub 在跑；重复启动时把已有窗口唤到前台。

选型：`QLocalServer` + `QLocalSocket`（Windows 下底层是命名管道）。
一次调用同时解决「判定」和「唤醒」两件事——比纯 Mutex 方案多出来的能力，
正是把已经在跑的那个实例叫到前台（Mutex 只能告诉你"有人了"，
想再递一句话得另搭一套 IPC）。

## 顺序不能反：必须先 connect 探活，再 listen

直觉上会写成「直接 listen，失败就说明已经有实例了」。这在**正常退出**下没问题，
但进程被强杀（任务管理器结束进程、崩溃）时命名管道可能没被释放，
于是新实例 listen 也失败 → **再也起不来了**，用户只能重启电脑。

所以顺序是：
  1. 先 connect 试着连一下。连得上 = 对方真的活着，递激活消息后自己退出。
  2. 连不上才 removeServer 清残留 + listen。

## 竞态与降级

两个实例几乎同时启动时，可能双方都 connect 失败、都去 listen，第二个必然失败。
此时再探活一次：连得上就老实退出；连不上说明只是环境不允许命名管道
（极少数安全策略下会这样），那就**放弃单例保护、照常启动**——
宁可同时开两个窗口，也不要让用户怎么点都打不开。

## 递消息要有回执，读完之前不能等

两条都是实测踩出来的，写在这里免得以后"优化"掉：

  1. **客户端必须等到 ack 再断开**。写完就 disconnect 的话，服务端可能还没读到，
     Windows 上它只会看到"对端已关闭"、`readAll()` 返回空 —— 消息静默丢失，
     表现是"第二个实例退出了，但第一个实例的窗口不弹"。
  2. **服务端读数据时绝不能 `waitForReadyRead`**。它会开嵌套事件循环，
     里层会把 `disconnected` 先派发进来，导致重入读取时 socket 已断开、数据读不到。
     数据到达交给 `readyRead` 信号驱动即可。
"""

import os
import time

from PySide6.QtCore import QCoreApplication, QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

MSG_ACTIVATE = b"activate"
MSG_ACK = b"ack"
# 「请让位，我要接管」—— 只用于"以管理员身份重启"这条路径（见 acquire_takeover）。
MSG_TAKEOVER = b"takeover"
DEFAULT_KEY = "Yuhub-SingleInstance"

# 连接探测超时。Windows 上管道不存在时会**立刻**失败，不会真的等这么久；
# 这个值只是给"有管道但不响应"的病态情况兜底，别设太大（会拖慢启动）。
CONNECT_TIMEOUT_MS = 400

# 等对方回执的超时。给得比较宽松：第一实例可能正在忙（比如刚启动还在扫硬件），
# 只要它在跑事件循环就一定会回。
ACK_TIMEOUT_MS = 1200


def _current_user():
    try:
        import getpass

        return getpass.getuser()
    except Exception:  # noqa: BLE001
        return ""


def server_name(key=DEFAULT_KEY):
    """把「键」补成完整的服务名（按登录用户区分）。

    Windows 的命名管道默认在**当前登录会话**内，但不同用户同时登录
    （快速用户切换 / 远程桌面）时同名管道会互相干扰，且跨会话访问会被权限拒掉。
    带上用户名最省事。

    **幂等**：已经带过后缀的名字再传进来不会重复拼接。
    这个防护是踩坑加的——`is_another_running(server_name())` 这种写法很自然，
    但双重套娃会算出 `xxx-M1racle-M1racle`，于是"明明有实例却探测不到"，
    而且不报错、只是静默失联，很难查。
    """
    if not key:
        return key
    user = _current_user()
    if not user:
        return key
    suffix = f"-{user}"
    return key if key.endswith(suffix) else key + suffix


class SingleInstance(QObject):
    """单实例守卫。

    用法：
        guard = SingleInstance()
        if not guard.acquire():      # 已有实例在跑（并且已被唤醒显示）
            return 0
        guard.activate_requested.connect(window.show_from_tray)
        ...
        guard.close()                # 退出前调用

    注意：必须在 `QApplication` 建好之后再用（QLocalSocket 依赖事件循环对象）。
    """

    # 收到「第二个实例想启动」的请求 → 应该把窗口显示出来
    activate_requested = Signal()

    # 收到「提权重启的新实例要接管」→ 应该保存状态并退出，把管道让出去
    takeover_requested = Signal()

    def __init__(self, key=DEFAULT_KEY, parent=None):
        super().__init__(parent)
        # key 是"半成品"，name 才是真正拿去 listen/connect 的服务名（带用户名后缀）。
        # 两者都公开，方便诊断时打印。
        self.key = key
        self.name = server_name(key)
        self._server = None
        self._sockets = []
        # 是否真的抢到了监听权。用于诊断：为 False 时单例保护是降级的
        self.listening = False

    # ------------------------------------------------------------ 角色一
    def acquire(self):
        """我是第一个实例吗？

        True  → 抢到了，开始监听，可以继续启动。
        False → 已经有实例在跑（且已被唤醒），当前进程应当直接退出。
        """
        if self._probe():
            return False

        QLocalServer.removeServer(self.name)      # 清掉可能残留的管道
        server = QLocalServer(self)
        server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        if not server.listen(self.name):
            # 失败原因要么是竞态（对方刚抢到），要么是环境不支持命名管道。
            # 再探一次活：连得上就说明对方真的起来了，老实退出。
            if self._probe():
                return False
            self._server = server          # 没能监听，但要留住对象避免被 GC
            self.listening = False
            return True

        server.newConnection.connect(self._on_new_connection)
        self._server = server
        self.listening = True
        return True

    # ------------------------------------------------------------ 角色三
    def _alive(self):
        """只探活，**不递任何消息**（不同于 `_probe`）。

        提权重启时要判断"旧实例让位了没有"，这时候最怕的就是顺手敲一下门
        —— 那会把旧窗口激活，用户以为重启失败。
        """
        sock = QLocalSocket()
        sock.connectToServer(self.name)
        ok = sock.waitForConnected(CONNECT_TIMEOUT_MS)
        if ok:
            sock.disconnectFromServer()
            if sock.state() != QLocalSocket.LocalSocketState.UnconnectedState:
                sock.waitForDisconnected(CONNECT_TIMEOUT_MS)
        else:
            sock.abort()
        return ok

    def request_takeover(self):
        """递出「请让位」并等回执。返回 True 表示确实连上了旧实例。"""
        return self._send(MSG_TAKEOVER)

    def acquire_takeover(self, timeout_ms=12000, interval_ms=150):
        """提权重启专用：请旧实例让位，等管道释放后自己接管。

        **不能直接调 `acquire()`**：`acquire()` 的第一件事是 `_probe()`，
        也就是"敲旧实例的门、让它把窗口显示出来"。提权重启时旧实例本来就
        要退出，这一敲的后果是**旧窗口被激活、新实例自己退出** —— 用户点
        了"以管理员身份重启"，结果程序闪了一下还是普通权限的旧窗口。

        超时没等到让位就退化成普通 `acquire()`（至少保证程序可用，
        最坏是"重启没成功"，而不是"程序打不开了"）。
        """
        if not self._alive():
            return self.acquire()          # 旧实例早退了，直接拿
        self.request_takeover()
        deadline = time.monotonic() + timeout_ms / 1000.0
        while time.monotonic() < deadline:
            if not self._alive():
                if self.acquire():
                    return True
            time.sleep(interval_ms / 1000.0)
        return self.acquire()

    # ------------------------------------------------------------ 发消息
    def _send(self, msg):
        """连上已有实例 → 写 msg → 收到回执 → 断开。返回 True=确实连上了。

        ⚠️ **不能只靠 `waitForBytesWritten` / `waitForReadyRead`**（实测踩过）：
        当客户端和服务端**在同一个进程**里时（自检是这样；"以管理员身份重启"
        时新旧实例也短暂共存），这两个 wait 都不保证把消息推出去 —— 字节还
        躺在 Qt 的写缓冲里，而紧接着的 `disconnectFromServer()` 会把它丢掉，
        对端一个字都收不到。症状非常隐蔽：`request_takeover()` 返回 True
        （连接确实建立了），旧实例却始终不让位。

        所以这里**显式泵事件循环**，直到 `bytesToWrite()` 归零并拿到回执。
        """
        sock = QLocalSocket()
        sock.connectToServer(self.name)
        if not sock.waitForConnected(CONNECT_TIMEOUT_MS):
            sock.abort()
            return False
        app = QCoreApplication.instance()
        try:
            sock.write(msg)
            deadline = time.monotonic() + ACK_TIMEOUT_MS / 1000.0
            acked = False
            while True:
                if app is not None:
                    # 同进程时，对端就是靠这一下才读到消息
                    app.processEvents()
                sock.flush()
                sock.waitForBytesWritten(20)
                if sock.bytesToWrite() == 0:
                    if acked:
                        break
                    if app is not None:
                        app.processEvents()
                    if sock.waitForReadyRead(30):
                        bytes(sock.readAll())
                        acked = True
                        break
                if time.monotonic() >= deadline:
                    break
        finally:
            sock.disconnectFromServer()
            if sock.state() != QLocalSocket.LocalSocketState.UnconnectedState:
                sock.waitForDisconnected(CONNECT_TIMEOUT_MS)
        return True

    # ------------------------------------------------------------ 角色二
    def _probe(self):
        """连一下已有实例，递出「把窗口显示出来」的请求，并**等回执**。

        返回 True 表示有活着的实例（消息已尽力送达）。
        """
        return self._send(MSG_ACTIVATE)

    # ------------------------------------------------------------ 监听端
    def _on_new_connection(self):
        if self._server is None:
            return
        while self._server.hasPendingConnections():
            conn = self._server.nextPendingConnection()
            if conn is None:
                break
            self._sockets.append(conn)
            conn.readyRead.connect(lambda c=conn: self._read(c))
            conn.disconnected.connect(lambda c=conn: self._drop(c))
            # 顺手读一次：对方若在服务端处理 newConnection 之前就把数据写到了，
            # 这一次能直接读走。注意**不能在这里等待**，原因见 _read。
            self._read(conn)

    def _read(self, conn):
        """读取并处理对端消息。**绝不能阻塞。**

        曾经在这里用 `waitForReadyRead` 等数据，结果丢消息：它会开一个
        **嵌套事件循环**，里层会把同一连接的 `disconnected` 先派发进来 →
        `_drop` 重入调用本函数 → 此时 socket 已是 UnconnectedState、
        `readAll()` 只剩空 → 消息被吃掉，回到外层也无从补救。
        所以这里只做"有什么读什么"，数据到达交给 readyRead 信号驱动。
        """
        try:
            data = bytes(conn.readAll())
        except RuntimeError:
            return
        if not data:
            return
        # 接管请求优先：提权重启时旧实例必须先让位（否则新实例 listen 不上，
        # 用户会看到"程序重启了一次却还是普通权限"）。
        if MSG_TAKEOVER in data:
            self._ack(conn)
            self.takeover_requested.emit()
            return
        if MSG_ACTIVATE in data:
            self._ack(conn)
            self.activate_requested.emit()

    def _ack(self, conn):
        """回执：告诉对方「我读到了」。对方据此才断开连接。"""
        try:
            conn.write(MSG_ACK)
            conn.flush()
        except RuntimeError:
            pass

    def _drop(self, conn):
        # 断开前可能还有没读走的尾包
        self._read(conn)
        try:
            conn.deleteLater()
        except RuntimeError:
            pass
        if conn in self._sockets:
            self._sockets.remove(conn)

    # ------------------------------------------------------------ 收尾
    def close(self):
        """退出前释放：断开所有连接、关掉监听、清掉管道。

        不清管道的话，下次启动会遇到一个"存在但没人应答"的管道，
        虽然我们的探活逻辑能兜住，但没必要留脏东西。
        """
        for conn in list(self._sockets):
            try:
                conn.abort()
            except RuntimeError:
                pass
        self._sockets.clear()
        if self._server is not None:
            try:
                self._server.close()
            except RuntimeError:
                pass
            self._server = None
        try:
            QLocalServer.removeServer(self.name)
        except Exception:  # noqa: BLE001
            pass
        self.listening = False


def is_another_running(key=DEFAULT_KEY):
    """诊断用：当前是否已有实例在监听。不会发送激活消息，不改变任何状态。

    参数是**原始 key**（和 `SingleInstance(key)` 一致），不是 `server_name()`
    的返回值——后者的用户名后缀会被再补一次。传完整名虽然也不会出错
    （`server_name` 已做幂等），但别依赖这个。
    """
    sock = QLocalSocket()
    sock.connectToServer(server_name(key))
    ok = sock.waitForConnected(CONNECT_TIMEOUT_MS)
    if ok:
        sock.disconnectFromServer()
        if sock.state() != QLocalSocket.LocalSocketState.UnconnectedState:
            sock.waitForDisconnected(CONNECT_TIMEOUT_MS)
    else:
        sock.abort()
    return ok
