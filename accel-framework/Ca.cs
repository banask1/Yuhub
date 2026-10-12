// 加速框架 · 本机证书体系
//
// 为什么加速必须自建 CA：要把用户对目标站点的 HTTPS 请求接到本机来处理
// （改写响应、注入脚本、按最优节点转发），就必须能对每个域名出示一张
// **客户端愿意接受**的证书 —— 而只有被系统信任的根 CA 签发的证书才满足这个条件。
//
// 🔴 三条不可动摇的红线（与 Python 版 netaccel.py 的结论一致）：
//   1. **私钥只在本机生成、只留在本机**，绝不从服务器下载、绝不上传。
//      私钥一旦外流，拿到它的人就能给任意网站签证书、解密所有信任该 CA 的流量。
//   2. 生成 / 签发 / 安装 / 卸载全程**不联网**。
//   3. 只存证书，**不落盘任何流量内容**。
//
// ⚠️ 私钥明文存放（PFX 无口令）是刻意的：加速器是后台常驻服务，
//    不可能每次启动让用户输口令；SteamTools / 302 同样明文落盘。
//    安全边界靠 %LOCALAPPDATA% 的用户隔离。要升级成"口令保护"，
//    改动点只有 SaveToFile/LoadFromFile 两处。

using System.Diagnostics;
using System.Security.Cryptography;
using System.Security.Cryptography.X509Certificates;

namespace YuhubAccel;

internal static class Ca
{
    private const string SubjectName = "CN=Yuhub Acceleration CA, O=Yuhub";
    private const int CaDays = 3650;      // 根证书 10 年：装了之后尽量别再让用户重装
    private const int LeafDays = 825;     // 叶证书：本地信任的自签叶不受 Chrome 398 天上限约束

    public static string Dir => Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
        "Yuhub", "accel", "ca");

    public static string PfxPath => Path.Combine(Dir, "YuhubAccelCA.pfx");
    public static string CerPath => Path.Combine(Dir, "YuhubAccelCA.cer");

    // ---------------------------------------------------------------- 生成 / 加载
    /// <summary>载入已有 CA；不存在返回 null。</summary>
    public static X509Certificate2? Load()
    {
        if (!File.Exists(PfxPath)) return null;
        try
        {
            return new X509Certificate2(File.ReadAllBytes(PfxPath));
        }
        catch
        {
            return null;
        }
    }

    /// <summary>确保 CA 存在（不存在则生成），返回带私钥的证书对象。</summary>
    public static X509Certificate2 Ensure()
    {
        var existing = Load();
        if (existing is not null) return existing;

        Directory.CreateDirectory(Dir);

        using var key = ECDsa.Create(ECCurve.NamedCurves.nistP256);
        var req = new CertificateRequest(SubjectName, key, HashAlgorithmName.SHA256);
        req.CertificateExtensions.Add(
            new X509BasicConstraintsExtension(true, false, 0, true));       // CA = true
        req.CertificateExtensions.Add(
            new X509KeyUsageExtension(X509KeyUsageFlags.KeyCertSign |
                                      X509KeyUsageFlags.CrlSign, true));
        req.CertificateExtensions.Add(
            new X509SubjectKeyIdentifierExtension(req.PublicKey, false));

        var notBefore = DateTimeOffset.UtcNow.AddDays(-1);
        var notAfter = DateTimeOffset.UtcNow.AddDays(CaDays);
        using var self = req.CreateSelfSigned(notBefore, notAfter);

        // ⚠️ 关键：必须走一次 PFX 往返，让证书**自己持有**可用的私钥。
        //    直接返回 CreateSelfSigned 的对象在跨进程/落盘再读之后常出问题；
        //    （同样的坑在 v1.0.7 的探针里踩过一次：SslStream 会静默拒绝。）
        File.WriteAllBytes(PfxPath, self.Export(X509ContentType.Pfx));
        File.WriteAllBytes(CerPath, self.Export(X509ContentType.Cert));     // DER，给 certutil 用

        return new X509Certificate2(File.ReadAllBytes(PfxPath));
    }

    // ---------------------------------------------------------------- 签发叶证书
    /// <summary>给某个域名现场签发一张叶证书（ECDSA P-256，带 SAN）。</summary>
    public static X509Certificate2 Leaf(X509Certificate2 ca, string host)
    {
        using var key = ECDsa.Create(ECCurve.NamedCurves.nistP256);
        var req = new CertificateRequest($"CN={host}", key, HashAlgorithmName.SHA256);

        var san = new SubjectAlternativeNameBuilder();
        san.AddDnsName(host);
        req.CertificateExtensions.Add(san.Build());
        req.CertificateExtensions.Add(
            new X509BasicConstraintsExtension(false, false, 0, false));
        req.CertificateExtensions.Add(new X509KeyUsageExtension(
            X509KeyUsageFlags.DigitalSignature | X509KeyUsageFlags.KeyEncipherment, false));
        req.CertificateExtensions.Add(new X509EnhancedKeyUsageExtension(
            new OidCollection { new("1.3.6.1.5.5.7.3.1") }, false));       // serverAuth

        var serial = RandomNumberGenerator.GetBytes(16);
        using var cert = req.Create(ca,
            DateTimeOffset.UtcNow.AddDays(-1),
            DateTimeOffset.UtcNow.AddDays(LeafDays),
            serial);
        using var withKey = cert.CopyWithPrivateKey(key);
        // PFX 往返（理由同 CA）
        return new X509Certificate2(withKey.Export(X509ContentType.Pfx));
    }

    // ---------------------------------------------------------------- 信任状态
    public static string Thumbprint(X509Certificate2 ca) =>
        ca.Thumbprint.Replace(" ", "").ToUpperInvariant();

    /// <summary>根证书是否已在本机「受信任的根证书颁发机构」里。</summary>
    public static bool IsTrusted(X509Certificate2 ca)
    {
        foreach (var storeName in new[] { StoreName.Root })
        {
            foreach (var loc in new[] { StoreLocation.LocalMachine, StoreLocation.CurrentUser })
            {
                try
                {
                    using var store = new X509Store(storeName, loc);
                    store.Open(OpenFlags.ReadOnly);
                    var hits = store.Certificates.Find(
                        X509FindType.FindByThumbprint, Thumbprint(ca), false);
                    if (hits.Count > 0) return true;
                }
                catch
                {
                    // 打不开就当这个store没装
                }
            }
        }
        return false;
    }

    // ---------------------------------------------------------------- 安装 / 卸载
    /// <summary>装进本机根信任库（需管理员）。</summary>
    public static (bool ok, string msg) Install(X509Certificate2 ca)
    {
        var (code, outp) = RunCertutil($"-addstore -f Root \"{CerPath}\"");
        if (code == 0) return (true, "已装入「受信任的根证书颁发机构」");
        if (outp.Contains("拒绝访问", StringComparison.OrdinalIgnoreCase) ||
            outp.Contains("Access is denied", StringComparison.OrdinalIgnoreCase))
            return (false, "权限不足：请以管理员身份运行");
        return (false, $"certutil 失败（{code}）：{outp.Trim()}");
    }

    /// <summary>从根信任库移除（需管理员）。</summary>
    public static (bool ok, string msg) Uninstall(X509Certificate2 ca)
    {
        var (code, outp) = RunCertutil($"-delstore Root {Thumbprint(ca)}");
        if (code == 0) return (true, "已从「受信任的根证书颁发机构」移除");
        if (outp.Contains("拒绝访问", StringComparison.OrdinalIgnoreCase) ||
            outp.Contains("Access is denied", StringComparison.OrdinalIgnoreCase))
            return (false, "权限不足：请以管理员身份运行");
        return (false, $"certutil 失败（{code}）：{outp.Trim()}");
    }

    private static (int code, string output) RunCertutil(string args)
    {
        try
        {
            var psi = new ProcessStartInfo("certutil", args)
            {
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                UseShellExecute = false,
                CreateNoWindow = true,
            };
            using var p = Process.Start(psi);
            if (p is null) return (-1, "无法启动 certutil");
            var outp = p.StandardOutput.ReadToEnd() + p.StandardError.ReadToEnd();
            p.WaitForExit(30000);
            return (p.ExitCode, outp);
        }
        catch (Exception ex)
        {
            return (-1, ex.Message);
        }
    }
}
