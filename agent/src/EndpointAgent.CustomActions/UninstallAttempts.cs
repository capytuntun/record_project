using System;
using System.Globalization;
using System.IO;
using System.Text;

namespace EndpointAgent.CustomActions;

/// <summary>
/// Records a refused uninstall so the agent can report it to the management
/// server on its next cycle (CLAUDE.md sections 16 and 17: a tamper attempt on
/// a managed endpoint is exactly the kind of event that belongs in the audit
/// trail).
///
/// Why a file rather than an HTTPS call from here: the custom action runs
/// inside msiexec for a few seconds, has no device credential (that lives
/// DPAPI-protected in the agent's own store) and no business holding one. The
/// agent service is already authenticated to the server, so it does the
/// reporting; this only has to leave a note where the agent will find it.
///
/// Everything here is BEST EFFORT. Failing to record must never fail or delay
/// an uninstall, so every path swallows its exceptions. The password refusal
/// itself does not depend on this succeeding.
/// </summary>
internal static class UninstallAttempts
{
    /// <summary>One JSON object per line, appended. The agent truncates it once reported.</summary>
    internal static string DefaultPath => Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData),
        "EndpointAgent", "uninstall-attempts.log");

    /// <summary>
    /// Refuse to grow without bound. There is no limit on password attempts, so
    /// this is what stops an endless guessing session from filling the disk.
    ///
    /// The agent drains the file every heartbeat, so in normal operation it
    /// holds seconds' worth of attempts and never approaches this. It only
    /// bites when nobody is collecting (agent stopped, server unreachable) --
    /// and in that case the attempts past the cap go unrecorded, which is worth
    /// knowing: the log is a best-effort notification channel, not evidence.
    /// </summary>
    private const long MaxBytes = 64 * 1024;

    internal static void Record(string path, string outcome)
    {
        try
        {
            var directory = Path.GetDirectoryName(path);
            if (string.IsNullOrEmpty(directory)) return;
            if (!Directory.Exists(directory)) return;   // agent not installed here

            var existing = new FileInfo(path);
            if (existing.Exists && existing.Length > MaxBytes) return;

            File.AppendAllText(path, BuildLine(outcome) + Environment.NewLine, Encoding.UTF8);
        }
        catch (Exception)
        {
            // Never let bookkeeping interfere with removal.
        }
    }

    /// <summary>
    /// Hand-rolled JSON, for the same reason the config reader is: this DLL is
    /// loaded by the installer's custom-action host, where an extra assembly
    /// reference is a failure mode rather than a convenience.
    /// </summary>
    internal static string BuildLine(string outcome)
    {
        var when = DateTime.UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ", CultureInfo.InvariantCulture);
        var user = SafeUser();
        return "{\"at\":\"" + when + "\""
             + ",\"outcome\":\"" + Escape(outcome) + "\""
             + ",\"localUser\":\"" + Escape(user) + "\"}";
    }

    private static string SafeUser()
    {
        try
        {
            var domain = Environment.UserDomainName;
            var name = Environment.UserName;
            return string.IsNullOrEmpty(domain) ? name : domain + "\\" + name;
        }
        catch (Exception)
        {
            return "";
        }
    }

    internal static string Escape(string value)
    {
        if (string.IsNullOrEmpty(value)) return "";
        var builder = new StringBuilder(value.Length + 8);
        foreach (var c in value)
        {
            switch (c)
            {
                case '"': builder.Append("\\\""); break;
                case '\\': builder.Append("\\\\"); break;
                case '\n': builder.Append("\\n"); break;
                case '\r': builder.Append("\\r"); break;
                case '\t': builder.Append("\\t"); break;
                default:
                    if (c < 0x20)
                    {
                        builder.Append("\\u").Append(((int)c).ToString("x4", CultureInfo.InvariantCulture));
                    }
                    else
                    {
                        builder.Append(c);
                    }
                    break;
            }
        }
        return builder.ToString();
    }
}
