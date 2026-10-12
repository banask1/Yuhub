// 加速框架 · 反向代理内核
//
// 流量路径：
//   浏览器/游戏 --(hosts 已指向 127.0.0.1)--> 本地 443 (Kestrel)
//     → 按 SNI 用本机 CA 现场签叶证书、终结 TLS
//     → 原样转发到该域名**预先解析好的真实 IP**（SNI/Host 仍是原域名，
//       上游证书照常走系统验证 —— 我们不做任何内容篡改）
//     → 响应流式回传
//
// 为什么转发要「钉 IP」：hosts 已把域名指向 127.0.0.1，若让 HttpClient
// 正常解析会形成回环。所以在改 hosts **之前**先解析好真实 IP，
// 转发时用 SocketsHttpHandler.ConnectCallback 直连该 IP。
//
// 支持 HTTP/1.1 与 Upgrade（WebSocket）：升级握手转发后是两条裸流的
// 对拷，不需要解析帧 —— 所以 Discord 网关这类 WS 流量也能过。
// HTTP/2 直连（无 Upgrade）走常规转发。

using System.Collections.Concurrent;
using System.Net;
using System.Net.Security;
using System.Net.Sockets;
using System.Text;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Http;
using Microsoft.AspNetCore.Server.Kestrel.Core;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;
using X509Certificates = System.Security.Cryptography.X509Certificates;

namespace YuhubAccel;

internal static class Proxy
{
    private static readonly Dictionary<string, X509Certificates.X509Certificate2> _leafCache = new();
    private static readonly object _leafLock = new();

    /// <summary>domain → 启动前解析好的真实 IPv4。</summary>
    private static readonly Dictionary<string, IPAddress> _ipMap = new();

    /// <summary>domain → 前置域名（仅直连测速全败的域名，见 Rules.Fronts）。</summary>
    private static readonly Dictionary<string, string> _frontMap = new();

    /// <summary>domain → 全部测速通过的候选 IP（延迟升序）；转发层做节点兜底。</summary>
    private static readonly Dictionary<string, List<IPAddress>> _candMap = new();

    /// <summary>逐请求要连的 IP：转发层发请求前写入，ConnectCallback 取出（节点兜底）。</summary>
    private static readonly ConcurrentDictionary<string, IPAddress> _nextIp = new();

    /// <summary>
    /// 常驻入口。参数：
    ///   --services steam,discord,…   要加速的平台（Rules 里的 key）
    ///   --port N                     监听端口（默认 443）
    ///   --no-hosts                   只起代理、不改 hosts（自检用）
    /// 返回 0 = 正常退出；4 = 端口被占；5 = 没有任何域名可用。
    /// </summary>
    public static async Task<int> RunAsync(string[] args)
    {
        string keysArg = "steam", hostsMode = "hosts";
        var port = 443;
        for (int i = 0; i < args.Length; i++)
        {
            if (args[i] == "--services" && i + 1 < args.Length) keysArg = args[++i];
            else if (args[i] == "--port" && i + 1 < args.Length
                     && int.TryParse(args[++i], out var p)) port = p;
            else if (args[i] == "--no-hosts") hostsMode = "no-hosts";
        }

        // 🔴 启动先清掉上一次可能残留的加速标记块：若上轮崩溃没清干净，
        // 域名仍指向 127.0.0.1，本轮 Dns 解析就会拿到 127.0.0.1、把流量
        // 环回自己（所有加速域名集体 502 / 超时）。必须在解析真实 IP 之前清。
        if (hostsMode == "hosts")
        {
            try
            {
                var (okC, msgC) = Hosts.Clean();
                if (!okC)
                    Console.Error.WriteLine("警告：启动前清理 hosts 失败（" + msgC +
                        "）—— 若残留加速记录，本次可能自环");
            }
            catch (Exception ex)
            {
                Console.Error.WriteLine("警告：启动前清理 hosts 异常：" + ex.Message);
            }
        }

        var ca = Ca.Ensure();
        if (!Ca.IsTrusted(ca))
            Console.Error.WriteLine("警告：根证书尚未被系统信任，客户端会报证书错误"
                                    + "（先在 Yuhub 里点「信任证书」）");

        var domains = Rules.DomainsFor(keysArg.Split(','));
        if (domains.Count == 0)
        {
            Console.Error.WriteLine("所选平台没有可加速的域名规则。");
            return 5;
        }

        // ---- ① 改 hosts 前先把真实 IP 选好（详见文件头）----
        // 不再"解析到哪个用哪个"：对每个域名的多个 A 记录做 TLS 握手测速，
        // 挑握手最快的可用节点 —— Akamai 的部分边缘节点在国内是连不通的，
        // 只取第一个 A 记录就会踩中（GitHub 能通、Steam 社区不通就是这么来的）。
        // 🔴 全部域名**并发**解析 + 并发测速：串行时 20+ 个域名要 20~70 秒，
        //    并发后总耗时 ≈ 最慢的一个域名（约 3 秒）。
        //
        // 远程规则源（Steam++ 公开接口）：拿每个域名的 ForwardDomainNames
        // （官方养的转发域名，DNS 出来的 IP 是活的）和 FakeServerName
        // （SNI 伪装名，对抗 SNI 点名阻断）。拉取失败不阻塞，退回纯 DNS。
        var remote = RemoteRules.Fetch(out var ruleSource);
        Console.WriteLine(ruleSource == ""
            ? "远程规则：未获取（走纯 DNS 模式）"
            : $"远程规则：已加载（{remote.Count} 条，来源 {ruleSource}）");

        var resolved = await Task.WhenAll(domains.Select(async d =>
        {
            try
            {
                // DNS 也要设超时：被墙域名的解析可能挂起十几秒，
                // 不设限的话 READY 会被最慢的那个域名拖死
                var resolve = Dns.GetHostAddressesAsync(d);
                var winner = await Task.WhenAny(
                    resolve, Task.Delay(2000));
                var addrs = winner == resolve
                    ? (await resolve)
                        .Where(a => a.AddressFamily == AddressFamily.InterNetwork)
                        .Distinct().Take(4).ToList()
                    : new List<IPAddress>();
                // 转发域名的 IP 也是候选（官方养的活节点）
                remote.TryGetValue(d, out var rr);
                if (rr?.Forward is not null)
                {
                    try
                    {
                        var fr = Dns.GetHostAddressesAsync(rr.Forward);
                        var fw = await Task.WhenAny(fr, Task.Delay(2000));
                        if (fw == fr)
                            addrs.AddRange((await fr)
                                .Where(a => a.AddressFamily == AddressFamily.InterNetwork)
                                .Take(4));
                    }
                    catch { }
                }
                var uniq = addrs.Distinct().Take(8).ToArray();
                return (domain: d, addrs: uniq,
                        error: uniq.Length == 0 ? "解析超时或无 IPv4" : null);
            }
            catch (Exception ex)
            {
                return (domain: d, addrs: Array.Empty<IPAddress>(),
                        error: $"解析失败（{ex.Message}）");
            }
        }));

        var frontPending = new List<(string Domain, string Front)>();
        await Task.WhenAll(resolved.Select(async t =>
        {
            var (d, addrs, error) = t;
            if (error is not null || addrs.Length == 0)
            {
                Console.Error.WriteLine($"跳过 {d}：{error ?? "解析不到 IPv4"}");
                return;
            }
            // 第一轮：真实 SNI 直连测速（拿全部通过节点，按延迟升序）
            var list = await NodeProbe.PickAsync(d, addrs);
            lock (_ipMap)
            {
                if (list is not null)
                {
                    _ipMap[d] = list[0].Ip;
                    _candMap[d] = list.Select(r => r.Ip).ToList();
                    Console.WriteLine(
                        $"  {d} -> {list[0].Ip}  ({list[0].Kind}, {list[0].LatencyMs} ms"
                        + $", 共测 {addrs.Length} 个节点, 备 {list.Count - 1} 个)");
                    return;
                }
            }
            // 第二轮：真实 SNI 全败 → 有 FakeServerName 就用伪装 SNI 再测
            remote.TryGetValue(d, out var rr);
            if (rr?.FakeSni is not null)
            {
                var fakeList = await NodeProbe.PickAsync(
                    d, addrs, sniOverride: rr.FakeSni);
                if (fakeList is not null)
                {
                    lock (_ipMap)
                    {
                        frontPending.Add((d, rr.FakeSni));
                        _ipMap[d] = fakeList[0].Ip;
                        _candMap[d] = fakeList.Select(r => r.Ip).ToList();
                    }
                    return;
                }
            }
            // 第三轮：内置前置表（Rules.Fronts）
            if (Rules.Fronts.ContainsKey(d))
            {
                lock (_ipMap) frontPending.Add((d, Rules.Fronts[d]));
                return;
            }
            Console.Error.WriteLine(
                $"跳过 {d}：{addrs.Length} 个节点全部握手失败"
                + (rr?.FakeSni is not null ? "（含 SNI 伪装重试）" : ""));
        }));

        // ---- 前置第二轮：把直连全败的域名挂到可用的前置/伪装名上 ----
        // 并发下"被伪装的域名/前置域名"可能比它后测完，所以放统一收尾处理
        foreach (var (d, front) in frontPending)
        {
            lock (_ipMap)
            {
                // 🔴 别把伪 SNI 测速得到的候选列表覆盖成单节点：那批 IP 是用
                //    伪装名**真实握手成功**的边缘节点（延迟升序），是前置路径
                //    做节点兜底的唯一依据。没有就退回前置域名自己测得的候选
                //    （对前置域名直连 = 对它做伪装 SNI 连接，形状完全相同）。
                List<IPAddress>? cands = null;
                if (_candMap.TryGetValue(d, out var ownC) && ownC.Count > 0)
                    cands = ownC;
                else if (_candMap.TryGetValue(front, out var frC) && frC.Count > 0)
                    cands = frC;

                if (_ipMap.TryGetValue(front, out var frontIp))
                {
                    _frontMap[d] = front;
                    _ipMap[d] = frontIp;
                    _candMap[d] = cands is not null
                        ? new List<IPAddress>(cands)
                        : new List<IPAddress> { frontIp };
                    Console.WriteLine(
                        $"  {d} -> SNI 伪装 via {front} ({frontIp})"
                        + $"（直连全被阻，走域前置, 备 {_candMap[d].Count - 1} 个）");
                }
                else if (_ipMap.TryGetValue(d, out var ownIp))
                {
                    // 伪装 SNI 测速已成功并记了自己的 IP，只是伪装域名
                    // 自己还没进 _ipMap —— 补上即可
                    _frontMap[d] = front;
                    _ipMap[front] = ownIp;
                    _candMap[d] = cands is not null
                        ? new List<IPAddress>(cands)
                        : new List<IPAddress> { ownIp };
                    Console.WriteLine(
                        $"  {d} -> SNI 伪装 via {front} ({ownIp})"
                        + $"（直连全被阻，走域前置, 备 {_candMap[d].Count - 1} 个）");
                }
                else
                    Console.Error.WriteLine(
                        $"跳过 {d}：直连全部被阻，且前置域名 {front} 也不可用");
            }
        }

        if (_ipMap.Count == 0)
        {
            Console.Error.WriteLine("所有域名都解析/测速失败，不启动（检查本机网络）。");
            return 5;
        }

        // ---- ② 写 hosts ----
        if (hostsMode == "hosts")
        {
            // 🔴 hosts 必须把所有加速域名指向 127.0.0.1（本地内核），
            //    而不是 _ipMap 里的真实边缘 IP：
            //    · 写真实 IP ⇒ 浏览器绕过内核直连边缘：测速选优/节点兜底
            //      全部失效，抽风边缘直接暴露给浏览器（商店黑屏转圈）；
            //    · 前置域名写前置 IP ⇒ 浏览器带真实 SNI 直连 ⇒ 正撞 SNI
            //      阻断，社区必死（内核正因为"直连全被阻"才走的前置，
            //      hosts 却把浏览器逼回直连）。
            //    指向 127.0.0.1 让流量全部过内核：叶证书由本机 CA 现签
            //    （CA 已装入受信任根），上游由内核钉 IP / 域前置 ——
            //    GUI 的就绪探测（连 127.0.0.1:443）与文案也是这么描述的。
            var map = _ipMap.Keys.ToDictionary(d => d, _ => "127.0.0.1");
            var (ok, msg) = Hosts.Apply(map);
            Console.WriteLine(msg);
            if (!ok) return 5;
        }

        // ---- ③ 起本地 443 ----
        var builder = WebApplication.CreateSlimBuilder();
        builder.Logging.ClearProviders();               // 别让默认日志刷屏
        builder.WebHost.UseKestrelHttpsConfiguration(); // AOT 下显式声明要 HTTPS

        builder.WebHost.ConfigureKestrel(o =>
        {
            o.Listen(IPAddress.Loopback, port, lo =>
            {
                // 按 SNI 现场签证书：只对规则内域名发证，其它 SNI 直接握手失败
                lo.UseHttps((stream, hello, _state, _ct) =>
                {
                    var cert = SelectCert(ca, hello.ServerName);
                    if (cert is null)
                        throw new System.Security.Authentication.AuthenticationException(
                            $"SNI {hello.ServerName} 不在加速规则里");
                    return ValueTask.FromResult(new SslServerAuthenticationOptions
                    {
                        ServerCertificate = cert,
                    });
                }, null);
            });
            // 单机回环代理不需要海量并发连接
            o.Limits.MaxConcurrentConnections = 512;
        });

        var app = builder.Build();
        app.Run(async ctx =>
        {
            try { await Forward(ctx); }
            catch (Exception ex)
            {
                Console.Error.WriteLine($"转发失败 {ctx.Request.Host.Value}: {ex}");
                if (!ctx.Response.HasStarted)
                {
                    ctx.Response.StatusCode = 502;
                    ctx.Response.ContentType = "text/plain; charset=utf-8";
                    await ctx.Response.WriteAsync("Yuhub accel: upstream error");
                }
            }
            finally { try { Console.Error.Flush(); Console.Out.Flush(); } catch { } }
        });

        // pid 文件给 Yuhub 本体做「内核还在不在」的判据
        var pidPath = Path.Combine(Directory.GetParent(Ca.Dir)!.FullName, "serve.pid");
        File.WriteAllText(pidPath, Environment.ProcessId.ToString());

        Console.WriteLine($"READY 加速内核已启动 https://127.0.0.1:{port} "
                          + $"域名 {(hostsMode == "hosts" ? "(已写 hosts) " : "")}{_ipMap.Count} 个");
        foreach (var kv in _ipMap)
            Console.WriteLine($"  {kv.Key} -> {kv.Value}");

        try
        {
            app.Run();
        }
        catch (Exception ex)
        {
            // 🔴 这里绝不能让异常逃出去：AOT 程序的未捕获异常会以 fail-fast
            //    （0xc0000409）收场，Windows 会弹"基于堆栈的缓冲区溢出"的
            //    恐吓对话框。最常见的就是 443 被占（上一个内核没死透 / 其它
            //    软件占了端口）—— 把真实原因打印出来、干净退出即可。
            var msg = ex.InnerException?.Message ?? ex.Message;
            Console.Error.WriteLine($"内核启动失败：{msg}");
            if (msg.Contains("bind", StringComparison.OrdinalIgnoreCase) ||
                msg.Contains("占用", StringComparison.OrdinalIgnoreCase) ||
                msg.Contains("in use", StringComparison.OrdinalIgnoreCase))
                Console.Error.WriteLine(
                    "端口 443 已被占用：可能是上一次的加速内核还没退出，"
                    + "请先在 Yuhub 里点「停止加速」或重启电脑后再试。");
            if (hostsMode == "hosts")
            {
                Hosts.Clean();
                Console.WriteLine("已清理 hosts");
            }
            try { File.Delete(pidPath); } catch { }
            return 6;
        }
        finally
        {
            if (hostsMode == "hosts")
            {
                Hosts.Clean();
                Console.WriteLine("已清理 hosts");
            }
            try { File.Delete(pidPath); } catch { }
        }
        return 0;
    }

    /// <summary>只对规则内域名发证；其它 SNI 返回 null → TLS 握手失败（安全兜底）。</summary>
    private static X509Certificates.X509Certificate2? SelectCert(
        X509Certificates.X509Certificate2 ca, string? sni)
    {
        if (string.IsNullOrEmpty(sni) || !_ipMap.ContainsKey(sni))
            return null;
        lock (_leafCache)
        {
            if (_leafCache.TryGetValue(sni, out var c) && c.NotAfter > DateTimeOffset.UtcNow)
                return c;
            var leaf = Ca.Leaf(ca, sni);
            _leafCache[sni] = leaf;
            return leaf;
        }
    }

    // ------------------------------------------------------------- 转发
    private static readonly SocketsHttpHandler _handler = CreateHandler();
    private static readonly HttpClient _client = new(_handler)
    {
        Timeout = TimeSpan.FromSeconds(100),
        DefaultRequestVersion = new Version(2, 0),
        DefaultVersionPolicy = HttpVersionPolicy.RequestVersionOrLower,
    };

    private static SocketsHttpHandler CreateHandler() => new()
    {
        UseProxy = false,
        AllowAutoRedirect = false,
        AutomaticDecompression = System.Net.DecompressionMethods.None,
        ConnectTimeout = TimeSpan.FromSeconds(6),
        // 关键：绕过 hosts（此时域名已指向 127.0.0.1），直连钉好的真实 IP。
        // SNI 与上游证书校验仍按原域名走 —— 我们不解上游的 TLS 内容。
        ConnectCallback = async (ctx, ct) =>
        {
            var host = ctx.DnsEndPoint.Host;
            IPAddress ip;
            // 优先用转发层为本请求挑好的备用节点（节点兜底）；没有则回退首选
            if (_nextIp.TryRemove(host, out var next))
                ip = next;
            else
            {
                lock (_ipMap)
                {
                    if (!_ipMap.TryGetValue(host, out ip!))
                        throw new InvalidOperationException($"域名 {host} 不在加速规则里");
                }
            }
            var tcp = new TcpClient();
            await tcp.ConnectAsync(ip, ctx.DnsEndPoint.Port, ct);
            return tcp.GetStream();
        },
    };

    private static async Task Forward(HttpContext ctx)
    {
        var host = ctx.Request.Host.Host;
        lock (_ipMap)
        {
            if (!_ipMap.ContainsKey(host))
            {
                ctx.Response.StatusCode = 404;
                return;                                     // 不加速规则外的域名
            }
        }
        Console.WriteLine($"FWD {ctx.Request.Method} {host}{ctx.Request.Path}");
        // 域前置：上游请求发往「前置域名」（SNI 也随之变成前置域名，
        // 绕开对真实域名的 SNI 阻断），但 Host 头仍写真实域名 ——
        // Akamai 边缘按 Host 路由，会把请求送给真实的站点。
        // ⚠️ 必须是局部变量：并发请求下静态字段会互相串
        string? front;
        lock (_frontMap)
        {
            _frontMap.TryGetValue(host, out front);
        }
        var upHost = front ?? host;
        // 域前置走裸 SslStream + 手写 HTTP/1.1（HttpClient 的 ClientHello
        // 带 ALPN 扩展，SNI 伪装场景下 100% 被重置 —— 实测见 FrontedUpstream.cs）
        if (front is not null)
        {
            // 前置路径同样做「首选失败换下一个候选」兜底：Akamai 边缘对伪装
            // SNI 连接的 RST 是随机的，单发一次就会把整个域名打成间歇 502。
            // 候选 = 伪 SNI 测速存下的列表（首选在前，_ipMap[front] 补首位）；
            // 与直连路径同理，只有幂等的 GET/HEAD 才换节点重发。
            _ipMap.TryGetValue(upHost, out var primary);
            var frontCands = _candMap.TryGetValue(host, out var fc) && fc.Count > 0
                ? fc : null;
            var ordered = new List<IPAddress>();
            if (primary is not null) ordered.Add(primary);
            if (frontCands is not null)
                foreach (var ip in frontCands)
                    if (!ordered.Contains(ip)) ordered.Add(ip);
            var methodF = ctx.Request.Method;
            int maxAttemptF = (methodF == "GET" || methodF == "HEAD")
                ? Math.Max(ordered.Count, 1) : 1;
            for (int attempt = 0; attempt < maxAttemptF; attempt++)
            {
                try
                {
                    await FrontedUpstream.Forward(ctx, host, front, ordered[attempt]);
                    return;
                }
                catch (Exception ex) when (attempt + 1 < maxAttemptF
                                           && !ctx.Response.HasStarted)
                {
                    // 响应还没开始才算"这次没打出去"，可以安全换节点重发；
                    // 最后一次尝试的异常直接向上抛，交给外层统一 502
                    Console.Error.WriteLine(
                        $"前置转发失败（节点 {ordered[attempt]}）{host}：" +
                        $"{ex.GetType().Name}: {ex.Message}，换下一个候选");
                }
            }
        }

        // ---- HTTP Upgrade（WebSocket）----
        var upgrade = ctx.Features.Get<Microsoft.AspNetCore.Http.Features.IHttpUpgradeFeature>();
        if (upgrade is not null && upgrade.IsUpgradableRequest)
        {
            var upstreamReq = new HttpRequestMessage(
                new HttpMethod(ctx.Request.Method),
                new Uri($"https://{upHost}{ctx.Request.Path}{ctx.Request.QueryString}"))
            { Version = new Version(1, 1), VersionPolicy = HttpVersionPolicy.RequestVersionExact };
            if (front is not null) upstreamReq.Headers.Host = host;
            CopyRequestHeaders(ctx, upstreamReq, hopByHop: true);

            var upstreamResp = await _client.SendAsync(
                upstreamReq, HttpCompletionOption.ResponseHeadersRead, ctx.RequestAborted);
            if ((int)upstreamResp.StatusCode != 101)
            {
                using (upstreamResp) await CopyPlainResponse(ctx, upstreamResp);
                return;
            }
            // 下游接受升级（先置 101）→ 得到一条裸流；上游 TLS 已被 handler
            // 终结，同样是裸流 —— 之后就是双向对拷，不解析帧
            ctx.Response.StatusCode = 101;
            var downstream = await upgrade.UpgradeAsync();
            var upstream = await upstreamResp.Content.ReadAsStreamAsync(ctx.RequestAborted);
            await Relay(downstream, upstream, ctx.RequestAborted);
            return;
        }

        // ---- 常规请求（带节点兜底重试）----
        // 直连域名可能解析出多个边缘节点，个别节点会在 TLS 之后把真实
        // HTTP 连接 RST / 挂起 —— 首选节点失败就换下一个候选，避免把整个
        // 域名打挂（GET 这类无 body 的请求重试是安全的）。
        var cands = _candMap.TryGetValue(host, out var cc) && cc.Count > 0 ? cc : null;
        // 只有幂等的 GET/HEAD 才换节点重试：POST 的 body 流读一次就到尾，
        // 重发会变成空 body（页面加载绝大多数是 GET，正是失败高发场景）。
        var method = ctx.Request.Method;
        int maxAttempt = (method == "GET" || method == "HEAD") ? (cands?.Count ?? 1) : 1;
        Exception? lastErr = null;
        HttpResponseMessage? resp = null;
        for (int attempt = 0; attempt < maxAttempt; attempt++)
        {
            if (cands is not null) _nextIp[host] = cands[attempt];
            var req = new HttpRequestMessage(
                new HttpMethod(ctx.Request.Method),
                new Uri($"https://{upHost}{ctx.Request.Path}{ctx.Request.QueryString}"));
            if (front is not null)
            {
                // 🔴 域前置必须锁 HTTP/1.1：Host 头路由是 HTTP/1.1 的语义，
                //    HTTP/2 里路由靠 :authority（= 伪装域名），带一个"额外的
                //    Host 头"会被 Akamai 直接 RST 连接（实测 0.2s 秒断）
                req.Headers.Host = host;
                req.Version = new Version(1, 1);
                req.VersionPolicy = HttpVersionPolicy.RequestVersionExact;
            }
            req.Content = new StreamContent(ctx.Request.Body);
            CopyRequestHeaders(ctx, req, hopByHop: true);
            Console.WriteLine($"  UP[{attempt}] -> https://{upHost}  Host={req.Headers.Host}  ver={req.Version}");
            try
            {
                HttpResponseMessage? r;
                if (attempt == 0)
                {
                    r = await _client.SendAsync(
                        req, HttpCompletionOption.ResponseHeadersRead, ctx.RequestAborted);
                }
                else
                {
                    // 重试尝试（attempt ≥ 1）给 6s 响应头上限：挂起型坏节点
                    // 不该把整个请求拖死。拿到响应头立即解除限时 —— 正文想
                    // 流多久都行。
                    // ⚠️ 这个 cts 不能 using/dispose：响应流还挂在它的 token
                    //    上，提前释放会把流的通知链拆掉。让它随响应被 GC。
                    var attemptCts = System.Threading.CancellationTokenSource
                        .CreateLinkedTokenSource(ctx.RequestAborted);
                    attemptCts.CancelAfter(6000);
                    r = await _client.SendAsync(
                        req, HttpCompletionOption.ResponseHeadersRead, attemptCts.Token);
                    attemptCts.CancelAfter(System.Threading.Timeout.InfiniteTimeSpan);
                }
                resp = r;
                break;
            }
            catch (Exception ex)
            {
                lastErr = ex;
                Console.Error.WriteLine(
                    $"转发失败（第 {attempt + 1} 个节点）{host}：" +
                    $"{ex.GetType().Name}: {ex.Message}");
                // 还有候选节点就继续换，否则退出循环交给 502
            }
        }
        if (resp is null)
        {
            Console.Error.WriteLine($"所有候选节点转发失败 {host}：{lastErr?.Message}");
            if (!ctx.Response.HasStarted)
            {
                ctx.Response.StatusCode = 502;
                ctx.Response.ContentType = "text/plain; charset=utf-8";
                await ctx.Response.WriteAsync("Yuhub accel: upstream error");
            }
            return;
        }
        await CopyPlainResponse(ctx, resp);
    }

    private static async Task CopyPlainResponse(HttpContext ctx, HttpResponseMessage resp)
    {
        ctx.Response.StatusCode = (int)resp.StatusCode;
        foreach (var h in resp.Headers)
            if (!IsHopByHop(h.Key))
                ctx.Response.Headers[h.Key] = h.Value.ToArray();
        foreach (var h in resp.Content.Headers)
            if (!IsHopByHop(h.Key))
                ctx.Response.Headers[h.Key] = h.Value.ToArray();
        // Kestrel 会自己补 Content-Length / chunked
        ctx.Response.Headers.Remove("content-length");
        await resp.Content.CopyToAsync(ctx.Response.Body, ctx.RequestAborted);
    }

    private static void CopyRequestHeaders(
        HttpContext ctx, HttpRequestMessage req, bool hopByHop)
    {
        foreach (var h in ctx.Request.Headers)
        {
            if (IsHopByHop(h.Key)) continue;
            try { if (!req.Headers.TryAddWithoutValidation(h.Key, h.Value.ToArray()))
                    req.Content?.Headers?.TryAddWithoutValidation(h.Key, h.Value.ToArray()); }
            catch { /* 个别受限头加不进去就算了 */ }
        }
    }

    internal static bool IsHopByHop(string name)
    {
        switch (name.ToLowerInvariant())
        {
            case "connection": case "keep-alive": case "proxy-authenticate":
            case "proxy-authorization": case "te": case "trailer":
            case "transfer-encoding": case "upgrade":
                return true;
            default: return false;
        }
    }

    /// <summary>两条裸流双向对拷（WebSocket / 任意升级协议）。</summary>
    private static async Task Relay(Stream a, Stream b, CancellationToken ct)
    {
        using var done = new CancellationTokenSource();
        var t1 = Pump(a, b, ct, done);
        var t2 = Pump(b, a, ct, done);
        await Task.WhenAny(t1, t2);
        done.Cancel();
        try { a.Close(); } catch { }
        try { b.Close(); } catch { }
    }

    private static async Task Pump(Stream from, Stream to, CancellationToken ct,
        CancellationTokenSource done)
    {
        var buf = new byte[16 * 1024];
        try
        {
            while (true)
            {
                int n = await from.ReadAsync(buf, ct);
                if (n <= 0) break;
                await to.WriteAsync(buf.AsMemory(0, n), ct);
                await to.FlushAsync(ct);
            }
        }
        catch { }
        finally { done.Cancel(); }
    }
}
