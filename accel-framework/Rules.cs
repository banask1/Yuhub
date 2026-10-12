// 加速框架 · 平台规则表
//
// 每个平台对应一组「要加速的域名」。加速原理：
//   1. 把这些域名写进 hosts → 127.0.0.1（带标记块，可整体清理）
//   2. 本地反向代理在 443 端口按 SNI 现场签证书、解 TLS
//   3. 解出来的请求转发到**预先解析好的真实 IP**（SNI/Host 仍是原域名）
//
// ⚠️ 域名是**精确匹配**（不做通配符），宁可少加速也不能把无关流量
//    卷进 MITM —— 证书选择器只对表内域名发证，其余 SNI 握手直接失败。
// ⚠️ 大文件下载 CDN（steamcontent 等）刻意没放进来：全量下载流量过本地
//    代理对速度反而有害，Steam++ 也是默认只转 CDN 的部分节点。

namespace YuhubAccel;

internal static class Rules
{
    /// <summary>平台 key → 域名列表（顺序即 hosts 写入顺序）。</summary>
    public static readonly Dictionary<string, string[]> Services = new()
    {
        ["steam"] = new[]
        {
            "store.steampowered.com", "steamcommunity.com", "api.steampowered.com",
            "login.steampowered.com", "help.steampowered.com", "checkout.steampowered.com",
            "steam-chat.com",
            "steamuserimages-a.akamaihd.net", "steamusercontent-a.akamaihd.net",
            "cdn.akamai.steamstatic.com", "cdn.cloudflare.steamstatic.com",
            "community.akamai.steamstatic.com", "store.cloudflare.steamstatic.com",
            "shared.akamai.steamstatic.com", "shared.cloudflare.steamstatic.com",
        },
        ["discord"] = new[]
        {
            "discord.com", "discord.gg", "discordapp.com", "discordapp.net",
            "cdn.discordapp.com", "media.discordapp.net", "gateway.discord.gg",
            "status.discord.com", "discordstatus.com",
        },
        ["twitch"] = new[]
        {
            "twitch.tv", "www.twitch.tv", "api.twitch.tv", "irc.chat.twitch.tv",
            "static.twitchcdn.net", "static-cdn.jtvnw.net",
        },
        ["github"] = new[]
        {
            "github.com", "gist.github.com", "api.github.com", "codeload.github.com",
            "raw.githubusercontent.com", "objects.githubusercontent.com",
            "avatars.githubusercontent.com", "github.githubassets.com",
        },
        ["translate"] = new[]
        {
            "translate.google.com", "translate.googleapis.com", "translate.gstatic.com",
        },
        ["origin"] = new[]
        {
            "ea.com", "www.ea.com", "signin.ea.com", "service.ea.com",
            "origin.com", "www.origin.com", "api.origin.com",
        },
        ["uplay"] = new[]
        {
            "ubisoft.com", "www.ubisoft.com", "connect.ubisoft.com",
            "uplay.ubi.com", "public.ubi.com",
        },
        // 这三类没有稳定可写的域名规则：公共 CDN 与图床的节点太杂，
        // 验证码平台走的是动态第三方域名 —— 写了反而容易误伤。如实留空。
        ["cdn"] = Array.Empty<string>(),
        ["captcha"] = Array.Empty<string>(),
        ["imagehost"] = Array.Empty<string>(),
    };

    /// <summary>汇总选中的平台的全部域名（去重、保序）。</summary>
    public static List<string> DomainsFor(IEnumerable<string> keys)
    {
        var all = new List<string>();
        foreach (var k in keys)
        {
            if (!Services.TryGetValue(k.Trim().ToLowerInvariant(), out var list))
                continue;
            foreach (var d in list)
                if (!all.Contains(d))
                    all.Add(d);
        }
        return all;
    }

    // ---------------------------------------------------------------- 域前置
    //
    // steamcommunity.com 的 SNI 在国内被点名单独阻断（实测：同 CDN 的
    // store.steampowered.com 全节点 TLS 正常，唯独它 4/6 节点全部握手失败）。
    // 解法是域前置（Steam++ 对社区站同款）：TLS 握手用同一 CDN 上没被阻的
    // 「前置域名」做 SNI，HTTP 层 Host 仍写真实域名 —— Akamai 边缘按 Host
    // 路由，会把请求送给真实的 steamcommunity。
    //
    // ⚠️ 只对「直连测速全败」的域名启用（见 Proxy.cs）：能直连的永远直连，
    //    前置只是兜底，不做全局 MITM 扩权。
    public static readonly Dictionary<string, string> Fronts = new()
    {
        ["steamcommunity.com"] = "store.steampowered.com",
    };
}
