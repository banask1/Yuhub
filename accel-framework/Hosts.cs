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
