// 加速框架 · 远程规则源（Steam++ 公开规则接口）
//
// 用户选择的方案 A：启动时从 Steam++（Watt Toolkit，AGPL-3.0 开源项目）
// 的公开规则接口拉取最新规则，取三样东西：
//   · ForwardDomainNames —— 官方维护的「转发域名」（他们养的活 IP）
//   · FakeServerName     —— SNI 伪装名（对抗 SNI 点名阻断）
//   · MatchDomainNames   —— 规则适用哪些域名
//
// 失败兜底链：接口失败 → 读本地缓存（rules-cache.json）→ 都没有就退回
// 纯 DNS 模式（原行为）。拉取超时 6 秒，任何失败都不阻塞启动。
//
// 安全边界：拿到的 IP 只用于**连上游**，上游证书照常按系统校验 ——
// 即使有人投毒给我们恶意 IP，也无法伪装成 Steam（证书过不了），
// 最坏结果是"加速失败"，不会被劫持。

using System.Text;
using System.Text.Json;

namespace YuhubAccel;

internal record RemoteRule(string Domain, string? Forward, string? FakeSni);

internal static class RemoteRules
{
    private const string ApiUrl = "https://api.steampp.net/accelerator/projectgroups";

    public static string CachePath => Path.Combine(
        Directory.GetParent(Ca.Dir)!.FullName, "rules-cache.json");

    /// <summary>domain → 远程规则（可能为空字典 = 没拿到）。</summary>
    public static Dictionary<string, RemoteRule> Fetch(out string source)
    {
        source = "";
        var dict = new Dictionary<string, RemoteRule>();
        string? json = null;

        // ① 在线拉取（6 秒超时；失败静默走缓存）
        try
        {
            using var http = new HttpClient();
            http.Timeout = TimeSpan.FromSeconds(6);
            http.DefaultRequestHeaders.UserAgent.ParseAdd("Steam++/3.0.807.0");
            using var content = new StringContent("{}", Encoding.UTF8, "application/json");
            using var resp = http.PostAsync(ApiUrl, content).GetAwaiter().GetResult();
            if (resp.IsSuccessStatusCode)
            {
                json = resp.Content.ReadAsStringAsync().GetAwaiter().GetResult();
                if (Parse(json, dict))
                {
                    source = "Steam++ 远程规则";
                    try { File.WriteAllText(CachePath, json); } catch { }
                }
            }
        }
        catch { }

        // ② 缓存兜底
        if (dict.Count == 0)
        {
            try
            {
                if (File.Exists(CachePath))
                {
                    json = File.ReadAllText(CachePath);
                    if (Parse(json, dict))
                        source = $"Steam++ 规则（本地缓存，{File.GetLastWriteTime(CachePath):MM-dd HH:mm}）";
                }
            }
            catch { }

            if (dict.Count > 0 && source == "")
                source = "Steam++ 规则（本地缓存）";
        }
        return dict;
    }

    /// <summary>
    /// 解析规则 JSON。结构：{ 任意key: [ { Name, Items: [ { MatchDomainNames,
    /// ForwardDomainNames, FakeServerName, ... }, … ] }, … ] }。
    /// 只处理顶层 Items（嵌套 Items 是 URL 路径级规则，用不上）。
    /// 返回是否解析出至少一条。
    /// </summary>
    private static bool Parse(string? json, Dictionary<string, RemoteRule> dict)
    {
        if (string.IsNullOrEmpty(json)) return false;
        try
        {
            using var doc = JsonDocument.Parse(json);
            if (doc.RootElement.ValueKind != JsonValueKind.Object) return false;
            foreach ( var groupList in doc.RootElement.EnumerateObject())
            {
                if (groupList.Value.ValueKind != JsonValueKind.Array) continue;
                foreach (var group in groupList.Value.EnumerateArray())
                {
                    if (!group.TryGetProperty("Items", out var items) ||
                        items.ValueKind != JsonValueKind.Array) continue;
                    foreach (var item in items.EnumerateArray())
                    {
                        var mdn = item.TryGetProperty("MatchDomainNames", out var m)
                                  ? m.GetString() : null;
                        if (string.IsNullOrEmpty(mdn)) continue;
                        var forward = item.TryGetProperty("ForwardDomainNames", out var f)
                                      ? NullIfEmpty(f.GetString()) : null;
                        var fake = item.TryGetProperty("FakeServerName", out var s)
                                   ? NullIfEmpty(s.GetString()) : null;
                        foreach (var raw in mdn.Split(';',
                                     StringSplitOptions.TrimEntries |
                                     StringSplitOptions.RemoveEmptyEntries))
                        {
                            // 只要纯域名；URL 路径模式（https://…）、通配符不要
                            var d = raw.Trim();
                            if (d.Length == 0 || d.Contains('/') || d.Contains('*') ||
                                d.Contains(' ')) continue;
                            if (!dict.ContainsKey(d))
                                dict[d] = new RemoteRule(d, forward, fake);
                        }
                    }
                }
            }
        }
        catch
        {
            return false;
        }
        return dict.Count > 0;
    }

    private static string? NullIfEmpty(string? s) =>
        string.IsNullOrWhiteSpace(s) ? null : s.Trim();
}
