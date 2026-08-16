using System.Security.Cryptography;
using System.Text;

namespace EndpointAgent;

/// <summary>
/// Stores the device credential encrypted with DPAPI at machine scope.
///
/// Machine scope rather than user scope because the agent runs as LocalSystem
/// and must survive user logoff. DPAPI ties the ciphertext to this machine, so
/// copying the file to another computer yields nothing usable -- a stolen
/// credential cannot be replayed from elsewhere.
///
/// This is not a substitute for the server-side controls. The credential is
/// revocable and rotates; DPAPI only raises the cost of lifting it off disk.
/// </summary>
public static class CredentialStore
{
    private static string FilePath => Path.Combine(AgentConfig.DirectoryPath, "device.cred");

    // Ties the ciphertext to this application, so another process on the same
    // machine cannot decrypt it by calling Unprotect blindly.
    private static readonly byte[] Entropy =
        Encoding.UTF8.GetBytes("EndpointAgent.DeviceCredential.v1");

    public static void Save(string credential)
    {
        Directory.CreateDirectory(AgentConfig.DirectoryPath);
        var cipher = ProtectedData.Protect(
            Encoding.UTF8.GetBytes(credential), Entropy, DataProtectionScope.LocalMachine);

        var temp = FilePath + ".tmp";
        File.WriteAllBytes(temp, cipher);
        File.Move(temp, FilePath, overwrite: true);
    }

    public static string? Load()
    {
        if (!File.Exists(FilePath)) return null;
        try
        {
            var plain = ProtectedData.Unprotect(
                File.ReadAllBytes(FilePath), Entropy, DataProtectionScope.LocalMachine);
            return Encoding.UTF8.GetString(plain);
        }
        catch (CryptographicException)
        {
            // Machine changed, profile rebuilt, or the file was tampered with.
            // Treat as "no credential" so the agent re-enrolls rather than
            // crash-looping on an unreadable blob.
            return null;
        }
    }

    public static void Clear()
    {
        if (File.Exists(FilePath)) File.Delete(FilePath);
    }
}

/// <summary>
/// Verifies the administrator password that gates local settings and the
/// agent's uninstall helper.
///
/// The package generator sends a PBKDF2 hash, never the password itself, so the
/// installed machine never holds a recoverable copy.
/// </summary>
public static class AdminPassword
{
    private const int Iterations = 210_000;
    private const int SaltBytes = 16;
    private const int HashBytes = 32;

    /// <summary>Format: pbkdf2-sha256$iterations$base64(salt)$base64(hash)</summary>
    public static string Hash(string password)
    {
        var salt = RandomNumberGenerator.GetBytes(SaltBytes);
        var hash = Rfc2898DeriveBytes.Pbkdf2(
            Encoding.UTF8.GetBytes(password), salt, Iterations, HashAlgorithmName.SHA256, HashBytes);
        return $"pbkdf2-sha256${Iterations}${Convert.ToBase64String(salt)}${Convert.ToBase64String(hash)}";
    }

    public static bool Verify(string? stored, string password)
    {
        if (string.IsNullOrWhiteSpace(stored)) return false;

        var parts = stored.Split('$');
        if (parts.Length != 4 || parts[0] != "pbkdf2-sha256") return false;
        if (!int.TryParse(parts[1], out var iterations) || iterations < 1000) return false;

        byte[] salt, expected;
        try
        {
            salt = Convert.FromBase64String(parts[2]);
            expected = Convert.FromBase64String(parts[3]);
        }
        catch (FormatException)
        {
            return false;
        }

        var actual = Rfc2898DeriveBytes.Pbkdf2(
            Encoding.UTF8.GetBytes(password), salt, iterations,
            HashAlgorithmName.SHA256, expected.Length);

        return CryptographicOperations.FixedTimeEquals(actual, expected);
    }
}
