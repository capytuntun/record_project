using System;
using System.IO;
using System.Net;
using System.Security.Cryptography;
using System.Text;

namespace EndpointAgent.CustomActions;

/// <summary>
/// Tells the management server, at uninstall time, that someone is removing the
/// agent -- so an alert fires even when there is nothing else to notice.
///
/// The password gate only produces an alert when it BLOCKS a removal (a refused
/// attempt the agent later reports). Two cases produce no alert otherwise: a
/// package built without an uninstall password (removal just proceeds), and a
/// removal that succeeds before the agent's next heartbeat. This reports
/// synchronously from the custom action, reading the same config and device
/// credential the agent uses (DPAPI, machine scope), so the server records the
/// attempt and raises its alert regardless.
///
/// Strictly best-effort: any failure (offline, no credential, cert not trusted)
/// is swallowed. Reporting a tamper attempt must never get in the way of a
/// legitimate removal (CLAUDE.md section 19). TLS validation is left at the
/// platform default -- never bypassed (section 30.9).
/// </summary>
internal static class UninstallReporter
{
    // Must match EndpointAgent.CredentialStore.Entropy.
    private static readonly byte[] Entropy =
        Encoding.UTF8.GetBytes("EndpointAgent.DeviceCredential.v1");

    public static void TryReport(string configPath, string outcome)
    {
        try
        {
            var serverUrl = ReadServerUrl(configPath);
            var credential = ReadCredential();
            if (string.IsNullOrEmpty(serverUrl) || string.IsNullOrEmpty(credential))
            {
                return;
            }
            Post(serverUrl!, credential!, outcome);
        }
        catch (Exception)
        {
            // Best effort only -- never affect the uninstall.
        }
    }

    private static string? ReadServerUrl(string configPath)
    {
        if (!File.Exists(configPath)) return null;
        var json = File.ReadAllText(configPath);
        const string key = "\"serverUrl\"";
        var i = json.IndexOf(key, StringComparison.Ordinal);
        if (i < 0) return null;
        i = json.IndexOf(':', i + key.Length);
        if (i < 0) return null;
        var start = json.IndexOf('"', i + 1);
        if (start < 0) return null;
        var end = json.IndexOf('"', start + 1);
        if (end < 0) return null;
        var url = json.Substring(start + 1, end - start - 1);
        return string.IsNullOrWhiteSpace(url) ? null : url;
    }

    private static string? ReadCredential()
    {
        var path = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData),
            "EndpointAgent", "device.cred");
        if (!File.Exists(path)) return null;
        try
        {
            var plain = ProtectedData.Unprotect(
                File.ReadAllBytes(path), Entropy, DataProtectionScope.LocalMachine);
            return Encoding.UTF8.GetString(plain);
        }
        catch (CryptographicException)
        {
            return null;
        }
    }

    private static void Post(string serverUrl, string credential, string outcome)
    {
        // net472 may default to an older protocol; the server requires TLS 1.2+.
        ServicePointManager.SecurityProtocol |= SecurityProtocolType.Tls12;

        var url = serverUrl.TrimEnd('/') + "/api/agent/uninstall-attempt";
        var body = "{\"attempts\":[{\"outcome\":\"" + JsonEscape(outcome)
            + "\",\"localUser\":\"" + JsonEscape(Environment.UserName)
            + "\",\"at\":\"" + JsonEscape(DateTime.UtcNow.ToString("o")) + "\"}]}";
        var data = Encoding.UTF8.GetBytes(body);

        var request = (HttpWebRequest)WebRequest.Create(url);
        request.Method = "POST";
        request.ContentType = "application/json";
        request.Headers["Authorization"] = "Bearer " + credential;
        request.Timeout = 8000;
        request.ReadWriteTimeout = 8000;

        using (var stream = request.GetRequestStream())
        {
            stream.Write(data, 0, data.Length);
        }
        using var response = (HttpWebResponse)request.GetResponse();
        using var _ = response.GetResponseStream();
    }

    private static string JsonEscape(string s)
    {
        var sb = new StringBuilder(s.Length + 8);
        foreach (var c in s)
        {
            switch (c)
            {
                case '"': sb.Append("\\\""); break;
                case '\\': sb.Append("\\\\"); break;
                case '\n': sb.Append("\\n"); break;
                case '\r': sb.Append("\\r"); break;
                case '\t': sb.Append("\\t"); break;
                default:
                    if (c < 0x20) sb.Append("\\u").Append(((int)c).ToString("x4"));
                    else sb.Append(c);
                    break;
            }
        }
        return sb.ToString();
    }
}
