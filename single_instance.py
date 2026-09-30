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

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

MSG_ACTIVATE = b"activate"
MSG_ACK = b"ack"
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

    # ------------------------------------------------------------ 角色二
    def _probe(self):
        """连一下已有实例，递出「把窗口显示出来」的请求，并**等回执**。

        返回 True 表示有活着的实例（消息已尽力送达）。
        """
        sock = QLocalSocket()
        sock.connectToServer(self.name)
        if not sock.waitForConnected(CONNECT_TIMEOUT_MS):
            sock.abort()
            return False
        try:
            sock.write(MSG_ACTIVATE)
            sock.flush()
            sock.waitForBytesWritten(CONNECT_TIMEOUT_MS)
            # 一定要等回执再断开（实测踩过）：
            # 写完立刻 disconnect，服务端可能还没读到，Windows 上它只会看到
            # "对端已关闭"、readAll() 返回空 —— 消息就这么没了。表现是
            # "第二个实例确实退出了，但第一个实例的窗口根本不弹"。
            # 有 ack 才能确定对方真的读到并处理了。
            if sock.waitForReadyRead(ACK_TIMEOUT_MS):
                bytes(sock.readAll())
        finally:
            sock.disconnectFromServer()
            if sock.state() != QLocalSocket.LocalSocketState.UnconnectedState:
                sock.waitForDisconnected(CONNECT_TIMEOUT_MS)
        return True

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
