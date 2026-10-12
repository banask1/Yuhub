// 加速框架 · 节点测速选优
//
// 同一个域名（尤其 Akamai 上的 Steam 系）会解析出多个边缘 IP，其中一部分
// 在国内连不通或握手被重置。只取第一个 A 记录就会踩雷 —— 表现为
// "GitHub 能通、Steam 社区不通"。这里对每个候选 IP 做一次真实 TLS 握手
// （SNI = 原域名，证书按系统正常校验），挑**握手最快**的成功节点。
//
// 三级回退：
//   TLS 握手成功（最优） > 仅 TCP 可连（可能 TLS 层有问题，但仍可用）
//   > 全失败（该域名跳过，不写 hosts —— 宁可加速少一个域名也不给坏节点）

using System.Diagnostics;
using System.Net;
using System.Net.Security;
using System.Net.Sockets;

namespace YuhubAccel;

internal static class NodeProbe
{
    public record NodeResult(IPAddress Ip, string Kind, int LatencyMs);

    /// <summary>
    /// 并发测速所有候选节点，返回**按延迟升序排好的全部成功节点**（可能多个）；
    /// 全败返回 null。转发层会拿这个列表做「首选节点挂了就换下一个」的兜底，
    /// 不再因为单个边缘节点抽风就把整个域名打挂。
    /// timeoutMs 是单个节点的握手超时。
    /// sniOverride：TLS 握手用的 SNI（默认用 host 本身；
    /// 域前置/伪装场景由调用方传入 FakeServerName）。
    /// </summary>
    public static async Task<List<NodeResult>?> PickAsync(
        string host, IReadOnlyList<IPAddress> candidates,
        int timeoutMs = 2500, string? sniOverride = null)
    {
        var tasks = candidates.Select(ip => ProbeOneAsync(host, ip, timeoutMs, sniOverride));
        var results = await Task.WhenAll(tasks);
        var ok = results.Where(r => r is not null)
                        .OrderBy(r => r!.LatencyMs)
                        .Select(r => r!)
                        .ToList();
        return ok.Count > 0 ? ok : null;
    }

    private static async Task<NodeResult?> ProbeOneAsync(
        string host, IPAddress ip, int timeoutMs, string? sniOverride)
    {
        var sw = Stopwatch.StartNew();
        TcpClient? tcp = null;
        try
        {
            tcp = new TcpClient();
            var connect = tcp.ConnectAsync(ip, 443);
            if (await Task.WhenAny(connect, Task.Delay(timeoutMs)) != connect)
                return null;                                  // TCP 超时
            await connect;                                    // 冒泡连接异常

            // 真正的判据是 TLS 握手：TCP 通但被重置的节点在这里现形
            using var ssl = new SslStream(tcp.GetStream(), leaveInnerStreamOpen: false);
            var handshake = ssl.AuthenticateAsClientAsync(
                new SslClientAuthenticationOptions
                {
                    TargetHost = sniOverride ?? host,     // SNI：默认原域名，可伪装
                    RemoteCertificateValidationCallback = null, // 系统正常校验
                });
            if (await Task.WhenAny(handshake, Task.Delay(timeoutMs)) != handshake)
                return null;                                  // 握手超时（典型被重置）
            await handshake;                                  // 冒泡握手异常
            sw.Stop();
            return new NodeResult(ip, "tls", (int)sw.ElapsedMilliseconds);
        }
        catch
        {
            // TLS 失败但 TCP 通的节点退一档（有些站点握手慢，只是超时误判）
            if (tcp is not null && tcp.Connected)
                return new NodeResult(ip, "tcp", (int)sw.ElapsedMilliseconds + 10000);
            return null;
        }
        finally
        {
            try { tcp?.Close(); } catch { }
        }
    }
}
