using System;
using System.IO;
using System.Security.Cryptography;
using System.Text;
using Xunit;

namespace EndpointAgent.CustomActions.Tests;

/// <summary>
/// The guard must only ever let an uninstall through when the password is right
/// or no password was configured. The regression that matters most here: the
/// agent rewrites agent.config.json after enrolling, and System.Text.Json's
/// default encoder writes '+' as +. The guard's hand-rolled reader used to
/// take that literally, base64 decoding threw, and the fail-open policy let ANY
/// password -- or a wrong one -- remove the agent.
/// </summary>
public class UninstallGuardTests : IDisposable
{
    private const string Password = "Test-Pw-123";
    private readonly string _dir = Path.Combine(Path.GetTempPath(), "eem-guard-" + Guid.NewGuid().ToString("N"));

    public UninstallGuardTests() => Directory.CreateDirectory(_dir);
    public void Dispose() { try { Directory.Delete(_dir, true); } catch { } }

    // Same scheme the server and New-AgentMsi.ps1 produce.
    private static string MakeHash(string password, bool withPlus)
    {
        for (var attempt = 0; attempt < 200; attempt++)
        {
            var salt = new byte[16];
            using (var rng = RandomNumberGenerator.Create()) rng.GetBytes(salt);
            using var kdf = new Rfc2898DeriveBytes(Encoding.UTF8.GetBytes(password), salt, 210000, HashAlgorithmName.SHA256);
            var hash = $"pbkdf2-sha256$210000${Convert.ToBase64String(salt)}${Convert.ToBase64String(kdf.GetBytes(32))}";
            if (hash.Contains("+") == withPlus) return hash;
        }
        throw new InvalidOperationException("could not produce a hash with the wanted shape");
    }

    private string WriteConfig(string? hashLiteral)
    {
        var path = Path.Combine(_dir, Guid.NewGuid().ToString("N") + ".json");
        var body = hashLiteral is null
            ? "{\n  \"serverUrl\": \"https://localhost\"\n}"
            : "{\n  \"serverUrl\": \"https://localhost\",\n  \"adminPasswordHash\": \"" + hashLiteral + "\"\n}";
        File.WriteAllText(path, body);
        return path;
    }

    [Fact]
    public void Hash_the_agent_escaped_as_u002B_still_enforces_the_password()
    {
        var real = MakeHash(Password, withPlus: true);
        var onDisk = real.Replace("+", "\\u002B");          // what the default encoder wrote
        Assert.DoesNotContain("+", onDisk);
        var path = WriteConfig(onDisk);

        Assert.Equal(real, UninstallGuard.ReadAdminPasswordHash(path));
        Assert.Equal(UninstallGuard.Decision.Allow, UninstallGuard.Evaluate(path, Password).Decision);
        Assert.Equal(UninstallGuard.Decision.BlockWrongPassword, UninstallGuard.Evaluate(path, "not-the-password").Decision);
        Assert.Equal(UninstallGuard.Decision.BlockWrongPassword, UninstallGuard.Evaluate(path, "亂打").Decision);
    }

    [Fact]
    public void Verbatim_hash_with_plus_enforces_the_password()
    {
        var real = MakeHash(Password, withPlus: true);
        var path = WriteConfig(real);

        Assert.Equal(real, UninstallGuard.ReadAdminPasswordHash(path));
        Assert.Equal(UninstallGuard.Decision.Allow, UninstallGuard.Evaluate(path, Password).Decision);
        Assert.Equal(UninstallGuard.Decision.BlockWrongPassword, UninstallGuard.Evaluate(path, "wrong").Decision);
    }

    [Fact]
    public void Hash_without_plus_enforces_the_password()
    {
        var real = MakeHash(Password, withPlus: false);
        var path = WriteConfig(real);

        Assert.Equal(UninstallGuard.Decision.Allow, UninstallGuard.Evaluate(path, Password).Decision);
        Assert.Equal(UninstallGuard.Decision.BlockWrongPassword, UninstallGuard.Evaluate(path, "wrong").Decision);
    }

    [Fact]
    public void Missing_password_is_blocked_when_one_is_configured()
    {
        var path = WriteConfig(MakeHash(Password, withPlus: false));
        Assert.Equal(UninstallGuard.Decision.BlockNoPassword, UninstallGuard.Evaluate(path, "").Decision);
    }

    [Fact]
    public void No_password_configured_allows_removal()
    {
        Assert.Null(UninstallGuard.ReadAdminPasswordHash(WriteConfig(null)));
        Assert.Equal(UninstallGuard.Decision.Allow, UninstallGuard.Evaluate(WriteConfig(null), "anything").Decision);
        Assert.Equal(UninstallGuard.Decision.Allow, UninstallGuard.Evaluate(Path.Combine(_dir, "missing.json"), "anything").Decision);
    }

    [Theory]
    [InlineData("\"a\\u002Bb\"", "a+b")]
    [InlineData("\"x\\/y\"", "x/y")]
    [InlineData("\"q\\\"q\"", "q\"q")]
    [InlineData("\"back\\\\slash\"", "back\\slash")]
    [InlineData("\"tab\\there\"", "tab\there")]
    [InlineData("\"plain\"", "plain")]
    public void ReadJsonString_decodes_standard_escapes(string literal, string expected)
    {
        Assert.Equal(expected, UninstallGuard.ReadJsonString(literal, 0));
    }

    [Theory]
    [InlineData("\"unterminated")]
    [InlineData("\"bad\\qescape\"")]
    [InlineData("\"short\\u12\"")]
    public void ReadJsonString_rejects_malformed_literals(string literal)
    {
        Assert.Null(UninstallGuard.ReadJsonString(literal, 0));
    }

    [Fact]
    public void Weak_or_foreign_hash_formats_do_not_verify()
    {
        Assert.False(UninstallGuard.VerifyPassword("pbkdf2-sha256$500$AAAA$AAAA", Password));   // too few iterations
        Assert.False(UninstallGuard.VerifyPassword("md5$1$AAAA$AAAA", Password));
        Assert.False(UninstallGuard.VerifyPassword("not-a-hash", Password));
    }
}
