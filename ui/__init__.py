"""Yuhub 桌面应用 UI 包。"""

# 版本号统一出口，改这里即可全局生效
# 0.12beta：屏幕共享从「内嵌 Piik（WebRTC + 浏览器内核 + 公网隧道）」整体
# 换成自研轻量流（GDI 抓屏 + 一条 TCP），随包不再带那三个二进制。
# 跳过 0.11beta 是因为那版（270MB）可能已经流出去过，同版本号不会触发老
# 客户端的自动更新（见 updater.parse_version 的比较规则）。
# 0.13beta：屏幕共享做到 30 帧（抓屏与编码拆成两条线程）、共享模式合并成
# 「选哪块网卡就走哪条链路」、修好"静止画面只发心跳"这条优化（逐字节比较
# 在真实桌面上从不命中，等于白写 —— 改成采样签名 + 每秒兜底刷新）。
# 0.14beta：按 emilkowalski/skills（设计工程技能库）做了一轮"工艺层"改造 ——
# 动效令牌统一（自定义贝塞尔曲线 + 时长预算）、补齐所有按钮的按下与键盘焦点
# 反馈、提示气泡改成"从下缘滑入 / 沿同一条边滑出"且退场快于进场、页面切换
# 加轻量淡入、新增「界面动效」三档设置。
# 0.15beta：按 Kyant0/AndroidLiquidGlass（Compose 液态玻璃库）改了两件事 ——
# ① 选中态从"实心主题蓝"换成**浅蓝液态玻璃**（侧栏指示条重画成玻璃片：
#    对角反光 + 内侧光带 + 内阴影 + 左右色散；分段选中段同步改成玻璃渐变）；
#    文字随之从白改成深藏蓝（浅蓝底 + 白字只有 1.9:1，读不出来）。
# ② 切换/开关的动画改用**弹簧曲线**（冲过目标再弹回）：侧栏指示条过冲 16%、
#    页面位移过冲 8%、开关滑块额外"移动中被拉成胶囊 + 按下放大"。
#    同时**移除**了 0.14beta 的「界面动效」三档调节（用户要求固定完整档）。
# 0.15.1beta：修两个用户实测出来的问题 ——
# ① 开关滑块在切换瞬间"白色圆圈变成正方形"：拉伸写成单侧拖尾，左缘直接跑到
#    −5.4px（滑块半径才 9），被控件边界一刀削平。改成**圆心对称拉伸 + 边界
#    钳制**（顶到端点时自动收窄到 24，正好与轨道端部半圆内切）。
# ② 「所有切换功能的地方都没有动画」：新增 ui/widgets.SegmentSlider ——
#    一块会**滑**的浅蓝液态玻璃选中片，装在 SegmentedControl 与内存页的
#    定时挡位 / 过载阈值选择器上（qss 的 `:checked` 是瞬时换色，做不出滑动，
#    所以选中底的绘制整体从 qss 挪到自绘）。
# 0.15.2beta：修「定时自动优化里选中『自定义』必须双击才出现切换动画」。
#    根因是 Qt 的点击时序：`QPushButton` 被点一下是**先 `nextCheckState()`
#    翻自己的 checked、再 emit `clicked`**，所以在 `toggled(True)` 的瞬间
#    旧按钮往往还没被取消 —— 滑动选中片只按"扫第一个 checked"定目标，就会
#    算到**旧**位置上，正好被"目标没变就不重播"的判据吃掉（『自定义』按钮
#    排在最末，是唯一会踩到这条的挡位）。改成由信号闭包**显式带上是哪个
#    按钮**，并给 `_apply` 补上"先全清、再点亮唯一一个"。
# 0.15.3beta：观看端新增**全屏观看**（用户要求：「观看共享屏幕的人需要可以
#    全屏观看」）。`ui/pages/share_page.FullScreenViewer` —— 一块纯黑的
#    顶层无边框窗口铺满整块屏，Esc / F11 / 双击画面退出，鼠标一动浮出
#    「怎么退出」的提示条再自动淡出。入口两个：画面区上方的「全屏观看」
#    按钮（没在观看时是灰的）+ 双击画面。**不是**把主窗口最大化 —— 主窗口
#    有标题栏、侧栏和卡片留白，最大化以后画面周围仍有一圈非画面区域，
#    而且用户全屏看完回列表时主窗口的窗口状态不该被程序改掉。
# 0.15.4beta：修「左侧工具栏有几个功能鼠标移上去没有选中的动画」（用户报）。
#    根因**不在我们的样式写法**：QSS 的 `:hover` 在这批侧栏按钮上根本不生效
#    —— 实测 `QStyleOptionButton.state & State_MouseOver` 恒为 False（鼠标
#    确实在按钮上、`underMouse()` 为 True、`WA_Hover` 也为 True），真机截屏
#    hover 前后**逐像素零差异**。改用**自绘**：`NavButton` 自己画一层比选中片
#    淡得多的浅蓝液态玻璃膜（alpha 92 vs 196），用与选中片**同一条**弹簧曲线
#    （16% 过冲）从两侧"收着"铺开，文字同时由 dim 提亮到正常亮度；选中项不叠膜。
#    ⚠️ 顺带记一条：`QWidget.grab()/render()` 渲染时会临时剥掉 `WA_UnderMouse`，
#    所以"用截图证明 qss 的 :hover 有效果"**永远测不到** —— 这也是这套反馈
#    必须自绘的原因（自绘看的是自己的 `_hover_t`，截图能测到、断言才立得住）。
# 0.15.5beta：新增「Steam加速」页（hosts 网络加速，Steam + GitHub）。
#    方案与 Chinachani/steam-hosts-tools (MIT) 同源：DoH 加密解析防 UDP 53
#    污染 → TCP 443 并发测速选最快边缘节点 → 写系统 hosts（带时间戳备份 +
#    区块标记，幂等可重复优选）→ 自动刷新 DNS 缓存。引擎 hostsaccel.py 纯
#    标准库；提权复用 cleaner.py 的 runas 模式（Yuhub.exe --hosts-elevated，
#    解析与测速留在普通进程，提权进程只写盘）。侧栏新增第 9 项。
# 0.15.6beta：加速功能升级为 Steam++ 同形态（本地反向代理）。
#    新增 hostssniproxy.py（纯标准库）：本机监听 443/80，HTTPS 按 TLS SNI
#    **透传**转发到优选真实节点（不解密、无需证书），HTTP 按 Host 转发。
#    hosts 条目指向 127.0.0.1，真实 IP 存映射缓存（accel_map.json），代理
#    resolver 缓存未命中时现场 DoH+测速兜底。页面新增加速模式分段
#    （反向代理/直连）；退出时自动停代理并清反代条目（on_shown 自愈可把
#    代理拉回来）。DNS 接管后 steamcommunity 等原本 hosts 无效的域名可达。
# 1.0：首个正式版（去掉 beta 后缀）。功能集与 0.15.6beta 一致 —— 系统清理、
#    内存优化、硬件监控、文件下载、游戏加速（Steam / GitHub）、屏幕共享、
#    局域网联机、主题包、开机自启、自动更新，正式发布到 GitHub。
#    ⚠️ 版本号规则：修复也必须升版本号（同版本号不触发老客户端自动更新）。
#    1.0 之后建议走 1.0.1 / 1.1.0 这类语义化版本。
# 1.0.1：修三个用户实测出来的问题 ——
#    ① 软件卸载「清理残留」总提示权限不够、删不掉：根因是**只读属性**被当成
#       权限问题（只读文件连管理员都删不掉，必须先摘属性），且**权限失败**被
#       MoveFileExW 误报成"已标记重启后删除"（它对权限问题会假装成功）。
#       现在先摘只读、识别真·权限失败（PermissionError / winerror 5）并如实
#       上报，交由提权批次重试。
#    ② 加速页改成 Steam++ 同形态的**一键开关**：每服务一个开关，拨开即优选
#       并写 hosts、拨回即恢复默认，去掉了"一键优选加速 / 恢复默认"两个按钮。
#    ③ 写 hosts 老报权限不足 + 恢复不了默认：补 `elevation_capable()` 前置
#       检测（源码无 exe 时给人话而不是干等 120 秒）、**已是管理员时直接写盘
#       不再弹 UAC**（try_write_direct）、提权结果文件路径加 PID+序号防并发
#       互踩、"没有条目"时恢复默认直接短路（不再白弹一次 UAC）。
#       另外**修掉一个自检自身的严重缺陷**：测试机是管理员时 `is_admin()`
#       为真，UI 自检会绕过打桩**真的写进系统 hosts** —— 现在 `run()` 开头
#       就把 is_admin 钉成 False，并比对真实 hosts 的 sha256 确保一字未改。
#    ④ 异地联机「游戏快连」新增「其他游戏」（无端口）选项：选中后复制的是
#       **纯 IP**、不带 `:端口`，下拉文案也不显示"（0）"；队友明确选了该选项
#       时同样只给纯 IP（判据用 game 字段，而不是 `port or 我的端口`）。
# 1.0.2：修三个用户实测出来的问题 ——
#    ① 联机「其他游戏」下拉最后一项点不中、鼠标移过去变成上下缩放光标：
#       根因是 `MainWindow._edge_at` 只看 `frameGeometry()` 判边，而 QComboBox
#       的下拉列表是**独立顶层窗口**，从 combo 下方铺下来时会伸到主窗口
#       下边缘带（后 5px）里 —— 于是最后几项被判成"下边缘"，光标变
#       SizeVerCursor，左键按下还被 `startSystemResize` 吞掉（点不动）。
#       现在用 `QApplication.topLevelAt()` 先确认光标在**主窗口**上，不是就
#       不做边缘缩放（右键菜单等其它弹出窗同理受益）。
#    ② Steam / GitHub 加速开关关不掉、拨回就自动弹回：根因是
#       `ShellExecuteW` 返回 HINSTANCE（指针宽度），`ctypes.windll` 默认按
#       `c_long`（32 位）取返回，高位非零被截断成小值/负数 → `rc <= 32`
#       误判成"用户取消了 UAC" → 开关失败回弹。现在统一走
#       `hostsaccel.shell_execute_runas()`，显式声明 `restype = c_void_p`
#       与 `argtypes`。
#    ③ 卸载残留"就算有权限也删不掉"：两个根因 —— (a) 目录 ACL 被收紧，
#       连管理员都删不掉，需要 `takeown /F … /R /D Y` + `icacls … /reset
#       /T /C /Q` 先夺回所有权再删（`_take_ownership`）；(b)
#       `_is_permission_error` 先判 `isinstance(PermissionError)`，而 Python
#       把 WinError 32（文件被占用）也映射成 PermissionError → 占用被误报
#       成"权限不足"。现在**先看 winerror**（只有 5 = ERROR_ACCESS_DENIED
#       才算真权限），32/33 是占用、145 是非空，都不算权限；管理员环境下
#       再补一次 takeown + icacls 重试。
# 1.0.3：加速做成"秒加速"（对齐 Steam++ / Watt Toolkit 的形态）+ 修「关不掉」
#    ① 打开加速要等十几秒才好：根因是**启用路径上做了一次全量网络测量** ——
#       对 20~25 个域名逐个跑 DoH（4 个源）+ TCP 443 握手测速，6 路并发也要
#       十几秒。而 Steam++ 点一下就能用的原因是它**根本不测**：hosts 只写
#       127.0.0.1，真实 IP 交给本地反向代理按 SNI 现场解析。
#       现在照做：反向代理模式（默认）打开开关 = 启动代理 + 立刻写全量
#       127.0.0.1，零测速；"域名→IP"映射缓存放后台预热，不挡启用。
#       直连模式（必须知道真实 IP）改为**先查上次优选缓存**：缓存齐全就
#       秒写，只有不齐时才退化为先测速。
#       附带：`accel_map.json` 加时间戳（CACHE_TTL=6h），缓存又新又全时连
#       后台预热都跳过；hosts 内容已是目标状态时连提权都省掉（重复拨开关
#       因此也是瞬时的，不再反复弹 UAC）。
#    ② 「加速关不掉」：v1.0.3 的取消事件只解决了"忙碌挡关闭"，但真正的
#       病灶是"开/关是两条独立提权操作"——UAC 等待期间改主意会静默早退、
#       开关弹回 hosts 真实状态，用户再点一次就触发反向操作、状态机分叉。
#    ③ v1.0.4 架构级重构：**只保留本地反向代理**（直连 hosts 模式删除，
#       UI 不再有模式分段），开/关统一为「意图文件」—— 每次拨开关把
#       会话期望态覆盖写进 %LOCALAPPDATA%\Yuhub\hosts_intent.json（seq
#       递增），提权子进程应用的是读文件那一刻的**最新意图**（带 0.6s
#       稳定窗，UAC 等待期间改的主意被同一次提权吸收）—— 永不分叉、
#       永不多弹 UAC。开关位置显示**用户意图**而非 hosts 真实状态，
#       杜绝"点了被弹回"的错觉；应用失败时意图对齐回真实状态并明确报错。
#       应用严格串行（同时最多一个提权在跑）；已是目标状态时不发起提权。
#    ④ v1.0.5 写盘方式对标 Steam++（Watt Toolkit 的 HostsService）：
#       原来"同目录 tmp + os.replace（改名覆盖）"需要**删除系统文件**的
#       权限 —— 装了火绒/360 的机器上这一步会被「hosts 文件保护」拦成
#       WinError 5，现象是"第一次能开、之后每次写/清都失败（hosts 未被
#       改动 / 关不掉）"。Steam++ 的做法是
#           File.SetAttributes(hosts, Normal);  File.WriteAllLines(...)
#       即**先摘只读、再原地覆盖写**，全程不删不改名。现在照做：
#       落盘阶梯 = ① 原地写（首选，Steam++ 同款，只用 FILE_WRITE_DATA）
#                 → ② 原子替换（兜底，① 被拦时用）
#       两种方法各重试 2 次（间隔 0.25s，给安全软件扫描/弹窗留窗口），
#       落地后**回读校验**，不一致就用原文还原并如实报失败（原地写没有
#       原子性，这一步是它的保险）；失败信息同时带上两种方法的原始错误
#       + 「把 Yuhub.exe 加进火绒信任区」的处置指引；残留的
#       hosts.yuhub.tmp 会被自动清掉。apply 结果新增 admin/method 字段，
#       下次再出问题一眼能定性（没权限还是被拦）。
#    ⑤ v1.0.6 常驻管理员启动（对标 Steam++ 的 requireAdministrator）：
#       读 SteamTools 源码时发现一个容易忽略但很关键的事实 ——
#       `source/SteamTools/app.manifest` 里写死
#           <requestedExecutionLevel level="requireAdministrator" />
#       也就是**它的主程序从启动就常驻管理员**：UAC 只在启动弹一次，
#       hosts 写入永远发生在**同一个已提权进程**里，火绒/360 这类软件也
#       只需要放行同一个程序一次。
#       Yuhub 现在直接照做：PyInstaller 的 `uac_admin=True` 把
#       requireAdministrator 写进 exe 清单。于是 hosts 写入 / 卸载残留清理 /
#       内存优化 / 进房间拉虚拟网卡全都变成"同一个进程直接做" —— 不再每次
#       操作都 `runas` 拉一个新进程。那正是两件老毛病的共同根因：
#         · HIPS 会把每个"新来的实例"重新审视一遍，拦一次就拿 WinError 5
#           （用户看到的"hosts 未被改动 / 关不掉"）；
#         · 进房间时那颗 UAC 弹窗会把桌面变暗、等用户点击（用户感知的
#           "打开联网房间卡顿"）。
#       v1.0.5 那套"加速页权限状态条 + 以管理员身份重启 + 单实例接管"
#       因此**整体删除** —— 启动即提权之后，它是多余的一层。
#    ⑥ v1.0.6 内存与体量优化：
#       · 九个页面改成**惰性构造**（`main_window._LazyPages`）：启动只建首屏
#         那一页，其余等你点进去才建。实测构造耗时 149ms → 45ms、构造后
#         常驻内存 85MB → 70MB；没访问过的页面一分内存都不占。
#         `_pages[key]` / `_pages.get(key)` 会自动触发构造，所以既有调用点
#         与自检都无需改动（自检里的 `win._pages["lan"]` 照样能用）。
#       · hosts 落地前整理行（`hostsaccel._tidy_lines`）：去掉尾部空行、
#         压缩连续空行。原来 `write_service_block` 在区块前插一个空行、
#         `clean_service` 不摘它 → 每开关一次就在 hosts 尾巴上多留一个空行
#         （用户机器上实测累积了 13 个）。现在反复开关后 hosts 逐字节回到原样。
#       · 打包体量：58.76MB → 42.11MB（-15.9MB / -28%）。真正削得动的只有一块 ——
#         PySide6 的钩子会**整包收集**一堆本程序一次都用不到的东西：
#         `collect_extra_binaries()` 无条件收 19.7MB 的软件 OpenGL
#         （opengl32sw.dll），QML/Quick/Pdf/虚拟键盘 的 DLL 也被未收集的插件
#         连带拖进来（合计又 ~18MB），另有 96 个 Qt 自带翻译（6.4MB）而
#         main.py 只会加载 `qtbase_zh_CN.qm` 一个名字。现在按 basename 在
#         spec 里过滤掉，并保留 offscreen/minimal 平台插件（自检要用）、
#         tls 后端、qsvg 图标与中文翻译。
#       · 死代码清理：13 处（cleaner 的 CleanTarget/_empty_dir、uninstaller 的
#         _enum_values/_registry_value/verify_removed、ui/widgets 的 FeatureCard
#         64 行、live_monitor.describe_media、updater.github_latest、
#         ui/glass.liquid_colors），净减 ~480 行。
#       ⚠️ 踩坑记录（教训写进 spec 与自检）：一开始把 `PySide6.QtTest` 也排掉了，
#       而 `lan_selftest` 里 `from PySide6.QtTest import QTest` 要驱动分享页点击
#       → 冻结态炸 ModuleNotFoundError，**十套自检全绿、只有 lan 红**，且 GUI
#       子系统没有 stderr、异常被兜底吞成 rc=5。现在 ① main 的兜底会把
#       traceback 落到 `<结果>.err`；② hostsaccel_selftest 增加一条断言，
#       逐一比对"源码里真正引用的 PySide6 子模块"与 spec 的 excludes。
VERSION = "1.0.6"
VERSION_LABEL = f"v{VERSION}"
APP_NAME = "Yuhub"
