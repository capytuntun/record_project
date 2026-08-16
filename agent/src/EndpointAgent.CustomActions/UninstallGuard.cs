using System;
using System.IO;
using System.Security.Cryptography;
using System.Text;

namespace EndpointAgent.CustomActions;

/// <summary>
/// The decision logic for password-gated uninstall, separated from the MSI
/// custom-action plumbing so it can be unit-tested without an installer.
///
/// SAFETY BIAS: fail OPEN. The only outcome that blocks an uninstall is
/// "configuration is readable, a password is required, and the supplied one is
/// wrong or missing". Every other case -- no config, no password configured, a
/// malformed hash, any exception -- ALLOWS the uninstall.
///
/// This is deliberate and required by CLAUDE.md section 19: the agent must not
/// become hard to remove. A bug here must never strand a machine; the worst a
/// bug can do is fail to enforce the password, never fail to allow removal.
/// A local administrator can in any case remove the product by other means --
/// this is a speed bump against casual removal, not an unremovable lock.
/// </summary>
public static class UninstallGuard
{
    public enum Decision
    {
        Allow,          // proceed with uninstall
        BlockNoPassword,// a password is required but none was supplied
        BlockWrongPassword,
    }

    public sealed class Result
    {
        public Decision Decision { get; set; }
        public string Message { get; set; } = "";
        public bool Allowed => Decision == Decision.Allow;
    }

    /// <summary>Default location of the agent config the installer wrote.</summary>
    public static string DefaultConfigPath =>
        Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData),
            "EndpointAgent", "agent.config.json");

    public static Result Evaluate(string configPath, string suppliedPassword)
    {
        string? hash;
        try
        {
            hash = ReadAdminPasswordHash(configPath);
        }
        catch (Exception)
        {
            // Cannot read/parse config -> do not stand in the way of removal.
            return Allow();
        }

        if (string.IsNullOrWhiteSpace(hash))
        {
            // No uninstall password was set for this package: nothing to enforce.
            return Allow();
        }

        if (string.IsNullOrEmpty(suppliedPassword))
        {
            return new Result
            {
                Decision = Decision.BlockNoPassword,
                Message = "移除此程式需要 IT 密碼。請洽貴公司 IT 部門，" +
                          "或以：msiexec /x {產品碼} UNINSTALLPWD=你的密碼 移除。",
            };
        }

        bool ok;
        try
        {
            ok = VerifyPassword(hash!, suppliedPassword);
        }
        catch (Exception)
        {
            // A malformed stored hash must not block removal.
            return Allow();
        }

        return ok
            ? Allow()
            : new Result { Decision = Decision.BlockWrongPassword, Message = "密碼錯誤，無法移除。" };
    }

    private static Result Allow() => new() { Decision = Decision.Allow };

    /// <summary>
    /// Extract adminPasswordHash from the config JSON. Deliberately a tiny
    /// hand-rolled scan rather than a JSON dependency, to keep the custom-action
    /// DLL small and free of assembly-load surprises during uninstall.
    /// </summary>
    internal static string? ReadAdminPasswordHash(string configPath)
    {
        if (!File.Exists(configPath)) return null;
        var json = File.ReadAllText(configPath);

        const string key = "\"adminPasswordHash\"";
        var i = json.IndexOf(key, StringComparison.Ordinal);
        if (i < 0) return null;

        i = json.IndexOf(':', i + key.Length);
        if (i < 0) return null;

        var start = json.IndexOf('"', i + 1);
        if (start < 0) return null;
        var end = json.IndexOf('"', start + 1);
        if (end < 0) return null;

        var value = json.Substring(start + 1, end - start - 1);
        return string.IsNullOrWhiteSpace(value) ? null : value;
    }

    /// <summary>
    /// Verify against the same format the server produces:
    /// pbkdf2-sha256$iterations$base64(salt)$base64(hash)
    /// </summary>
    internal static bool VerifyPassword(string stored, string password)
    {
        var parts = stored.Split('$');
        if (parts.Length != 4 || parts[0] != "pbkdf2-sha256") return false;
        if (!int.TryParse(parts[1], out var iterations) || iterations < 1000) return false;

        var salt = Convert.FromBase64String(parts[2]);
        var expected = Convert.FromBase64String(parts[3]);

        // Rfc2898DeriveBytes with an explicit hash algorithm is available on
        // .NET Framework 4.7.2, which is what DTF custom actions target.
        using var kdf = new Rfc2898DeriveBytes(
            Encoding.UTF8.GetBytes(password), salt, iterations, HashAlgorithmName.SHA256);
        var actual = kdf.GetBytes(expected.Length);

        return FixedTimeEquals(actual, expected);
    }

    private static bool FixedTimeEquals(byte[] a, byte[] b)
    {
        if (a.Length != b.Length) return false;
        var diff = 0;
        for (var i = 0; i < a.Length; i++) diff |= a[i] ^ b[i];
        return diff == 0;
    }
}
