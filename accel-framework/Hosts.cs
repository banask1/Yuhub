// 加速框架 · hosts 改写
//
// 只碰自己的标记块：
//   # >>> YuhubAccel v1 >>>
//   <ip> <domain>
//   ...
//   # <<< YuhubAccel <<<
// 别的内容一个字节不动。无论块里有多少行、IP 变过几轮，
// 写 = 删旧块再插新块；清 = 只删块。这样崩溃残留也能被下一次覆盖/清理。

using System.Text;

namespace YuhubAccel;

internal static class Hosts
{
    public const string Begin = "# >>> YuhubAccel v1 >>>";
    public const string End = "# <<< YuhubAccel <<<";
    public static string Path =>
        System.IO.Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.System),
            "drivers", "etc", "hosts");

    // ---------------------------------------------------------------- 脏标记
    //
    // 「hosts 已被本框架改写」的落盘标记（与 serve.pid 同目录）。
    //
    // 🔴 为什么必须有它：写完 hosts 之后、内核真正开始监听之前，任何一步**暴死**
    //    都来不及跑 finally 里的还原。而残留状态的后果最严重 —— 所有加速域名被
    //    指向 127.0.0.1，而 443 上没人监听：浏览器 / Steam 直接打不开页面
    //    （用户看到的就是一个纯报错页）。AOT 的 fail-fast（0xc0000409）和任务
    //    管理器强杀都属于这一类，**拦不住**。
    //    有了标记，Yuhub 下次进「网络加速」页就能识别"上次是脏退"，自动跑一次
    //    hosts-clean 并把真相说出来，而不是让用户对着打不开的网页猜。
    //
    // 生命周期：写 hosts 成功 → MarkDirty()；还原 hosts 成功 → ClearDirty()。
    public static string DirtyFlagPath => System.IO.Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
        "Yuhub", "accel", "hosts_dirty");

    public static void MarkDirty()
    {
        try
        {
            var p = DirtyFlagPath;
            var dir = System.IO.Path.GetDirectoryName(p);
            if (!string.IsNullOrEmpty(dir)) System.IO.Directory.CreateDirectory(dir);
            File.WriteAllText(p, Environment.ProcessId.ToString());
        }
        catch
        {
            // 标记写不进去不该拦住加速本身（顶多少一层兜底）
        }
    }

    public static void ClearDirty()
    {
        try { File.Delete(DirtyFlagPath); } catch { }
    }

    /// <summary>当前标记块内容（无块返回 null）。诊断用。</summary>
    public static string? CurrentBlock()
    {
        foreach (var line in ReadAllLines())
            if (line.Trim() == Begin) return Begin;
        return null;
    }

    /// <summary>把域名 → IP 映射写进 hosts（替换旧标记块）。需管理员。</summary>
    public static (bool ok, string msg) Apply(IReadOnlyDictionary<string, string> map)
    {
        try
        {
            var lines = WithoutBlock(ReadAllLines());
            var sb = new StringBuilder();
            sb.AppendLine(Begin);
            sb.AppendLine("# 由 Yuhub 加速框架维护 —— 请勿手工编辑此块，"
                          + "停止加速或卸载时会整体清除");
            foreach (var (domain, ip) in map)
                sb.AppendLine($"{ip} {domain}");
            sb.AppendLine(End);
            var all = lines.ToList();
            if (all.Count > 0 && all[^1].Trim().Length > 0)
                all.Add("");                                    // 与原文件保持空行分隔
            all.AddRange(sb.ToString().ReplaceLineEndings("\n")
                            .Split('\n', StringSplitOptions.RemoveEmptyEntries));
            File.WriteAllLines(Path, all, new UTF8Encoding(false));
            return (true, $"已写入 {map.Count} 条 hosts 记录");
        }
        catch (Exception ex)
        {
            return (false, FailMsg(ex));
        }
    }

    /// <summary>移除标记块。没块也返回成功（幂等）。</summary>
    public static (bool ok, string msg) Clean()
    {
        try
        {
            var lines = WithoutBlock(ReadAllLines()).ToList();
            while (lines.Count > 0 && lines[^1].Trim().Length == 0)
                lines.RemoveAt(lines.Count - 1);
            File.WriteAllLines(Path, lines, new UTF8Encoding(false));
            return (true, "已清除 hosts 中的加速记录");
        }
        catch (Exception ex)
        {
            return (false, FailMsg(ex));
        }
    }

    private static IEnumerable<string> ReadAllLines()
        => File.Exists(Path) ? File.ReadAllLines(Path) : Array.Empty<string>();

    private static IEnumerable<string> WithoutBlock(IEnumerable<string> lines)
    {
        var inside = false;
        foreach (var line in lines)
        {
            var t = line.Trim();
            if (t == Begin) { inside = true; continue; }
            if (t == End) { inside = false; continue; }
            if (!inside) yield return line;
        }
    }

    private static string FailMsg(Exception ex) =>
        ex is UnauthorizedAccessException || ex.Message.Contains("拒绝访问")
            ? "权限不足：写 hosts 需要管理员（请从 Yuhub 主程序里启动加速）"
            : $"hosts 写入失败：{ex.Message}";
}
