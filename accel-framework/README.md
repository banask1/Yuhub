# Yuhub 加速框架（`YuhubAccel`）

这是 **Yuhub 本体之外的、按需下载的加速内核**。本体（`Yuhub.exe`，约 42MB）不再
自带任何加速能力；用户要加速时，由本体下载这个框架，装好之后加速才存在。

- 技术栈：**.NET 8 + Native AOT**
- 产物：**单文件、不需要 .NET 运行时**
- 参考体积：本页这个"只有证书体系"的早期版本 **2.56 MB**；加上反向代理
  （YARP）与 HTTPS 之后约 **16 MB**（已在 v1.0.7 的探针里实测：16.24 MB）

## 为什么是独立框架，而不是做进本体

要让"加速"能改写 HTTPS 内容（注入脚本、在 HTTP 层改写与转发），就必须在本机
自建一个根 CA —— 这是 **TLS 中间人**，等于把全机 HTTPS 的信任交出去。这个代价
不该强加给每一个只想清理 C 盘的用户。所以：

```
不装框架 → 本体就是纯工具箱，零信任代价
装了框架 → 加速能力存在，且可以随时 uninstall-ca 撤回信任
```

## 🔴 三条不可动摇的红线

1. **CA 私钥只在本机生成、只留在本机** —— 绝不从服务器下载、绝不上传。
   私钥外流 = 拿到它的人能给任意网站签证书、解密所有信任该 CA 的流量。
   （反面教材：网上有第三方仓库把别人的 `steamcommunityCA.key` 公开挂出来。）
2. **全程不联网**：生成 / 签发 / 安装 / 卸载。
3. **只存证书，不落盘任何流量内容。**

## 命令

```bash
# 本机生成 CA（已存在则复用）。无需管理员。
YuhubAccel gen-ca

# 查指纹 / 有效期 / 是否已被系统信任。无需管理员。
YuhubAccel status

# 装进「受信任的根证书颁发机构」/ 移除。需要管理员。
YuhubAccel install-ca
YuhubAccel uninstall-ca

# 现场签发一张叶证书（验证用，不进信任库）。
YuhubAccel issue store.steampowered.com
```

## 文件落点

```
%LOCALAPPDATA%\Yuhub\accel\ca\
    YuhubAccelCA.pfx   根证书 + 私钥（无口令，靠用户目录隔离）
    YuhubAccelCA.cer   根证书 DER（交给 certutil 装信任）
```

> ⚠️ 私钥明文存放是**刻意**的：加速器是后台常驻服务，不可能每次启动让用户输口令；
> SteamTools / Steamcommunity_302 同样明文落盘。要升级成口令保护，
> 改动点只有 `Ca.Load()` 与 `Ca.Ensure()` 两处。

## 构建

```bash
# 需要：.NET SDK 8+ ，以及 VS Build Tools 的 MSVC 链接器（Native AOT 硬要求）
dotnet publish -c Release -r win-x64 -o out
```

`--norestart` 这类参数**必须**与 `--quiet`/`--passive` 同用；VS Build Tools 安装时的
组件选择与 AOT 前置，见项目记忆里的详细记录。

## 当前进度

- [x] 证书体系：CA 生成 / 载入 / 按域名签发叶证书 / 信任查询 / 安装 / 卸载
- [ ] 反向代理内核（YARP）：按 SNI 动态签发 + 故障转移 + 健康检查
- [ ] 加速规则集（域名清单）加载
- [ ] 连通性测试
- [ ] 与 Yuhub 本体的拉起 / 停止 / 状态回传
