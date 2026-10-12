// 加速框架 · 域前置专用上游（裸 SslStream + HTTP/1.1）
//
// 为什么不用 HttpClient：实测（2026-10-11）在 SNI 伪装场景下，
// .NET SocketsHttpHandler 的 ClientHello 带 ALPN 扩展 → 100% 被
// 「远程主机强迫关闭」；裸 SslStream（无 ALPN，同 python/浏览器手写）
// 同一 IP 同一 SNI 5/5 稳定 200。所以前置请求绕开 HttpClient，
// 手写 HTTP/1.1（这也顺带解决了 h2 :authority 与 Host 头路由冲突）。
//
// 上游用 Connection: close（每请求一次握手 ~400ms，比连接池慢一点，
// 但正确性优先；Steam 社区页面没有 WebSocket/长连接需求）。

using System.Net;
using System.Net.Security;
using System.Net.Sockets;
using System.Text;
using Microsoft.AspNetCore.Http;

namespace YuhubAccel;

internal static class FrontedUpstream
{
    public static async Task Forward(
        HttpContext ctx, string host, string front, IPAddress ip)
    {
        var ct = ctx.RequestAborted;
        using var tcp = new TcpClient();
        // 连接/握手都要限时：挂起型坏节点不能拖死整个请求 ——
        // 转发层靠异常换下一个候选节点，没有超时就永远轮不到下一个
        Task connect = tcp.ConnectAsync(ip, 443, ct).AsTask();
        if (await Task.WhenAny(connect, Task.Delay(5000, ct)) != connect)
            throw new TimeoutException($"前置连接超时（{ip}）");
        await connect;
        await using var ssl = new SslStream(tcp.GetStream(), false);

        // SNI = 伪装域名，证书按系统正常校验（伪装名是真 CDN 域名，能过）
        var handshake = ssl.AuthenticateAsClientAsync(
            new SslClientAuthenticationOptions { TargetHost = front }, ct);
        if (await Task.WhenAny(handshake, Task.Delay(5000, ct)) != handshake)
            throw new TimeoutException($"前置 TLS 握手超时（{front} via {ip}）");
        await handshake;

        // ---- 上游请求头（HTTP/1.1，Host = 真实域名）----
        var sb = new StringBuilder(512);
        sb.Append(ctx.Request.Method).Append(' ')
          .Append(ctx.Request.Path).Append(ctx.Request.QueryString)
          .Append(" HTTP/1.1\r\nHost: ").Append(host).Append("\r\n");
        foreach (var h in ctx.Request.Headers)
        {
            if (Proxy.IsHopByHop(h.Key) ||
                h.Key.Equals("Host", StringComparison.OrdinalIgnoreCase)) continue;
            foreach (var v in h.Value)
                sb.Append(h.Key).Append(": ").Append(v).Append("\r\n");
        }
        sb.Append("Connection: close\r\n\r\n");
        await ssl.WriteAsync(Encoding.Latin1.GetBytes(sb.ToString()), ct);

        // ---- 请求体 ----
        var te = ctx.Request.Headers.TransferEncoding.ToString();
        if (ctx.Request.ContentLength is > 0)
        {
            await ctx.Request.Body.CopyToAsync(ssl, ct);
        }
        else if (te.Contains("chunked", StringComparison.OrdinalIgnoreCase))
        {
            // 下游 chunked 请求体（浏览器 POST 较少见；小体积直接解块重发）
            var body = await DechunkToMemory(ctx.Request.Body, ct);
            await ssl.WriteAsync(body, ct);
        }

        // ---- 读上游响应头（累积到 \r\n\r\n）----
        var headBuf = new MemoryStream();
        var one = new byte[8192];
        var headEnd = -1;
        while (headEnd < 0)
        {
            var n = await ssl.ReadAsync(one, ct);
            if (n <= 0) break;
            headBuf.Write(one, 0, n);
            headEnd = FindDoubleCrlf(headBuf.GetBuffer(), (int)headBuf.Length);
            if (headBuf.Length > 256 * 1024) break;
        }
        var all = headBuf.GetBuffer();
        var headLen = headEnd < 0 ? (int)headBuf.Length : headEnd;
        var headText = Encoding.Latin1.GetString(all, 0, headLen);

        // ---- 解析状态行与响应头 ----
        var lines = headText.Split("\r\n");
        var status = 502;
        if (lines.Length > 0 && lines[0].StartsWith("HTTP/"))
        {
            var parts = lines[0].Split(' ', 3);
            _ = int.TryParse(parts.Length > 1 ? parts[1] : "", out status);
        }
        ctx.Response.StatusCode = status;

        var upstreamChunked = false;
        object? keepContentLength = null;    // null = 不设置
        foreach (var line in lines.Skip(1))
        {
            var idx = line.IndexOf(':');
            if (idx <= 0) continue;
            var k = line[..idx].Trim();
            var v = line[(idx + 1)..].Trim();
            if (Proxy.IsHopByHop(k)) continue;
            if (k.Equals("Transfer-Encoding", StringComparison.OrdinalIgnoreCase))
            {
                upstreamChunked = v.Contains("chunked", StringComparison.OrdinalIgnoreCase);
                continue;                                  // 我们自己重新分帧
            }
            if (k.Equals("Content-Length", StringComparison.OrdinalIgnoreCase))
            {
                if (!upstreamChunked && long.TryParse(v, out var cl))
                    keepContentLength = cl;
                continue;
            }
            ctx.Response.Headers[k] = v;
        }
        if (keepContentLength is long cl2) ctx.Response.ContentLength = cl2;

        // ---- 响应体：头之后的部分先写，再继续流式 ----
        var bodyStart = headEnd < 0 ? (int)headBuf.Length : headEnd + 4;
        await ctx.Response.Body.WriteAsync(
            all, bodyStart, (int)headBuf.Length - bodyStart, ct);

        if (upstreamChunked)
        {
            // 上游 chunked → 解块后交给 Kestrel 重新分帧
            await DechunkStream(ssl, ctx.Response.Body, ct);
        }
        else
        {
            await ssl.CopyToAsync(ctx.Response.Body, 16384, ct);
        }
        try { ssl.Close(); } catch { }
    }

    // ------------------------------------------------------------ 工具
    private static int FindDoubleCrlf(byte[] buf, int len)
    {
        for (var i = 0; i + 3 < len; i++)
            if (buf[i] == 13 && buf[i + 1] == 10 && buf[i + 2] == 13 && buf[i + 3] == 10)
                return i;
        return -1;
    }

    /// <summary>读 chunked 流并输出原始字节（浏览器 POST 体一般很小）。</summary>
    private static async Task<byte[]> DechunkToMemory(Stream src, CancellationToken ct)
    {
        using var ms = new MemoryStream();
        var lineBuf = new byte[16];
        while (true)
        {
            // 读一行 chunk size
            int n, lineLen = 0;
            while ((n = await src.ReadAsync(lineBuf.AsMemory(lineLen, 1), ct)) > 0)
            {
                if (lineBuf[lineLen] == 10) break;             // \n
                lineLen++;
                if (lineLen >= 15) break;
            }
            if (n <= 0 || lineLen == 0) break;
            var sizeHex = Encoding.ASCII.GetString(lineBuf, 0, lineLen).Trim();
            var semi = sizeHex.IndexOf(';');
            if (semi >= 0) sizeHex = sizeHex[..semi];
            if (!int.TryParse(sizeHex, System.Globalization.NumberStyles.HexNumber,
                    null, out var size)) break;
            if (size == 0) break;
            var chunk = new byte[size];
            await ReadExactly(src, chunk, ct);
            await ms.WriteAsync(chunk, ct);
            await src.ReadAsync(lineBuf.AsMemory(0, 2), ct);   // CRLF
        }
        return ms.ToArray();
    }

    /// <summary>流式解 chunked（上游响应体可能很大，逐块搬运）。</summary>
    private static async Task DechunkStream(Stream src, Stream dst, CancellationToken ct)
    {
        var line = new byte[16];
        var chunk = new byte[64 * 1024];
        while (true)
        {
            int n, lineLen = 0;
            while ((n = await src.ReadAsync(line.AsMemory(lineLen, 1), ct)) > 0)
            {
                if (line[lineLen] == 10) break;
                lineLen++;
                if (lineLen >= 15) break;
            }
            if (n <= 0 || lineLen == 0) break;
            var sizeHex = Encoding.ASCII.GetString(line, 0, lineLen).Trim();
            var semi = sizeHex.IndexOf(';');
            if (semi >= 0) sizeHex = sizeHex[..semi];
            if (!int.TryParse(sizeHex, System.Globalization.NumberStyles.HexNumber,
                    null, out var size)) break;
            if (size == 0) break;                              // last-chunk
            while (size > 0)
            {
                var take = Math.Min(size, chunk.Length);
                await ReadExactly(src, chunk.AsMemory(0, take), ct);
                await dst.WriteAsync(chunk.AsMemory(0, take), ct);
                size -= take;
            }
            await src.ReadAsync(line.AsMemory(0, 2), ct);      // CRLF
        }
    }

    private static async Task ReadExactly(Stream s, Memory<byte> buf, CancellationToken ct)
    {
        var off = 0;
        while (off < buf.Length)
        {
            var n = await s.ReadAsync(buf[off..], ct);
            if (n <= 0) throw new IOException("上游在读取响应体时断开");
            off += n;
        }
    }
}
