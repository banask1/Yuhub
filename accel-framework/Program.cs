// 加速框架 · 命令行入口
//
// 这个进程由 Yuhub 主程序按需拉起（框架随本体**内嵌**，运行时解包安装）。
// 分成两条路径：
//   · 一次性管理动作：生成 CA / 查状态 / 装信任 / 卸信任 / 清 hosts
//   · 常驻服务：加速内核（本地 443 反向代理 + hosts 改写，见 Proxy.cs）
//
// 用法：
//   YuhubAccel gen-ca          生成本机 CA（已存在则复用）
//   YuhubAccel status          打印 CA 指纹、有效期、是否已被系统信任
//   YuhubAccel install-ca      装进「受信任的根证书颁发机构」（需管理员）
//   YuhubAccel uninstall-ca    从信任库移除（需管理员）
//   YuhubAccel issue <host>    现场签发一张叶证书（验证用，不进信任库）

using System.Security.Cryptography.X509Certificates;

namespace YuhubAccel;

internal static class Program
{
    private static int Main(string[] args)
    {
        // AOT + InvariantGlobalization 下这里偶尔会抛，别让它带崩整个入口
        try { Console.OutputEncoding = System.Text.Encoding.UTF8; } catch { }
        var cmd = args.Length > 0 ? args[0].TrimStart('-').ToLowerInvariant() : "help";

        return cmd switch
        {
            "gen-ca" => GenCa(),
            "status" => args.Length > 1 && args[1].TrimStart('-').ToLowerInvariant() == "json"
                        ? StatusJson() : Status(),
            "install-ca" => InstallCa(),
            "uninstall-ca" => UninstallCa(),
            "issue" => Issue(args.Length > 1 ? args[1] : ""),
            "serve" => Proxy.RunAsync(args.Skip(1).ToArray()).GetAwaiter().GetResult(),
            "hosts-clean" => HostsClean(),
            "help" or "" => Help(),
            _ => Unknown(cmd),
        };
    }

    // ---------------------------------------------------------------- 命令
    private static int GenCa()
    {
        var ca = Ca.Ensure();
        Console.WriteLine("CA 就绪");
        Console.WriteLine($"  主题     : {ca.Subject}");
        Console.WriteLine($"  指纹     : {Ca.Thumbprint(ca)}");
        Console.WriteLine($"  有效期   : {ca.NotBefore:yyyy-MM-dd} ~ {ca.NotAfter:yyyy-MM-dd}");
        Console.WriteLine($"  私钥     : {(ca.HasPrivateKey ? "有（仅存本机）" : "缺失")}");
        Console.WriteLine($"  存放目录 : {Ca.Dir}");
        return 0;
    }

    private static int Status()
    {
        var ca = Ca.Load();
        if (ca is null)
        {
            Console.WriteLine("尚未生成 CA（可运行：YuhubAccel gen-ca）");
            return 1;
        }
        var trusted = Ca.IsTrusted(ca);
        Console.WriteLine($"CA 指纹   : {Ca.Thumbprint(ca)}");
        Console.WriteLine($"有效期    : {ca.NotBefore:yyyy-MM-dd} ~ {ca.NotAfter:yyyy-MM-dd}");
        Console.WriteLine($"系统信任  : {(trusted ? "已信任" : "未信任")}");
        Console.WriteLine($"余期      : {Math.Max(0, (ca.NotAfter - DateTime.Now).Days)} 天");
        return 0;
    }

    /// <summary>
    /// 给 Yuhub 本体读的**机器可读**状态。单行 JSON，字段名稳定。
    /// 为什么要单独一个分支：本体不该去 parse 上面那份给人看的文本。
    /// </summary>
    private static int StatusJson()
    {
        var ca = Ca.Load();
        if (ca is null)
        {
            Console.WriteLine("{\"ca\":false}");
            return 1;
        }
        var trusted = Ca.IsTrusted(ca);
        var days = Math.Max(0, (ca.NotAfter - DateTime.Now).Days);
        // 手写 JSON（字段全是受控值，无需序列化器，AOT 下也更省）
        Console.WriteLine(
            "{\"ca\":true,\"thumbprint\":\"" + Ca.Thumbprint(ca) + "\"," +
            "\"trusted\":" + (trusted ? "true" : "false") + "," +
            "\"expires_in_days\":" + days + "}");
        return 0;
    }

    /// <summary>移除 hosts 里本框架的标记块（幂等；崩溃残留也清得掉）。需管理员。</summary>
    private static int HostsClean()
    {
        var (ok, msg) = Hosts.Clean();
        Console.WriteLine((ok ? "[OK] " : "[失败] ") + msg);
        return ok ? 0 : 1;
    }

    private static int InstallCa()
    {
        var ca = Ca.Ensure();
        var (ok, msg) = Ca.Install(ca);
        Console.WriteLine((ok ? "[OK] " : "[失败] ") + msg);
        if (ok)
        {
            Console.WriteLine("现在浏览器与多数程序都会接受本 CA 签发的域名证书。");
            Console.WriteLine($"不再需要时请运行：YuhubAccel uninstall-ca");
        }
        return ok ? 0 : 1;
    }

    private static int UninstallCa()
    {
        var ca = Ca.Load();
        if (ca is null)
        {
            Console.WriteLine("没有可卸载的 CA");
            return 0;
        }
        var (ok, msg) = Ca.Uninstall(ca);
        Console.WriteLine((ok ? "[OK] " : "[失败] ") + msg);
        return ok ? 0 : 1;
    }

    private static int Issue(string host)
    {
        if (string.IsNullOrWhiteSpace(host))
        {
            Console.WriteLine("用法：YuhubAccel issue <host>");
            return 2;
        }
        var ca = Ca.Ensure();
        using var leaf = Ca.Leaf(ca, host.Trim());
        Console.WriteLine($"已为 {host} 签发叶证书");
        Console.WriteLine($"  主题   : {leaf.Subject}");
        Console.WriteLine($"  有效期 : {leaf.NotBefore:yyyy-MM-dd} ~ {leaf.NotAfter:yyyy-MM-dd}");
        Console.WriteLine($"  私钥   : {(leaf.HasPrivateKey ? "有" : "缺失")}");
        Console.WriteLine($"  签发者 : {leaf.Issuer}");
        return 0;
    }

    private static int Help()
    {
        Console.WriteLine("Yuhub 加速框架");
        Console.WriteLine();
        Console.WriteLine("  YuhubAccel gen-ca          生成本机 CA（已存在则复用）");
        Console.WriteLine("  YuhubAccel status          打印 CA 指纹、有效期、是否已被系统信任");
        Console.WriteLine("  YuhubAccel install-ca      装进「受信任的根证书颁发机构」（需管理员）");
        Console.WriteLine("  YuhubAccel uninstall-ca    从信任库移除（需管理员）");
        Console.WriteLine("  YuhubAccel issue <host>    现场签发一张叶证书（验证用）");
        Console.WriteLine("  YuhubAccel status --json   给 Yuhub 本体读的单行 JSON 状态");
        Console.WriteLine("  YuhubAccel serve [参数]    启动加速内核（常驻）：");
        Console.WriteLine("      --services steam,discord,…  要加速的平台 key（逗号分隔）");
        Console.WriteLine("      --port N                    监听端口（默认 443）");
        Console.WriteLine("      --no-hosts                  只起代理不改 hosts（自检用）");
        Console.WriteLine("  YuhubAccel hosts-clean     只清理 hosts 里的加速标记块");
        Console.WriteLine();
        Console.WriteLine("私钥只在本机生成、只留在本机，全程不联网。");
        return 0;
    }

    private static int Unknown(string cmd)
    {
        Console.WriteLine($"未知命令：{cmd}");
        Help();
        return 2;
    }
}
