using System.Text.Json;
using System.Text.Json.Serialization;

namespace EndpointAgent;

/// <summary>
/// Machine-scoped agent configuration, written by the installer into
/// ProgramData and readable by anyone, writable only by administrators.
///
/// Deliberately contains no secret. The enrollment token is consumed on first
/// start and then removed from this file; the device credential it is exchanged
/// for lives in <see cref="CredentialStore"/>, DPAPI-protected, never here.
/// </summary>
public sealed class AgentConfig
{
    /// <summary>Management server base URL, e.g. https://management.example.com</summary>
    [JsonPropertyName("serverUrl")]
    public string ServerUrl { get; set; } = "";

    /// <summary>One-time installer token. Cleared once enrollment succeeds.</summary>
    [JsonPropertyName("enrollmentToken")]
    public string? EnrollmentToken { get; set; }

    [JsonPropertyName("organizationId")]
    public string? OrganizationId { get; set; }

    /// <summary>
    /// Argon2-style hash is overkill here and would drag in a dependency; this
    /// is a salted PBKDF2 hash of the administrator password chosen when the
    /// package was generated. It gates local settings changes and the agent's
    /// own uninstall helper (CLAUDE.md sections 12 and 19).
    ///
    /// It is a hash, so possessing this file does not yield the password.
    /// </summary>
    [JsonPropertyName("adminPasswordHash")]
    public string? AdminPasswordHash { get; set; }

    [JsonPropertyName("logLevel")]
    public string LogLevel { get; set; } = "Information";

    /// <summary>Server-assigned identity, written after enrollment.</summary>
    [JsonPropertyName("endpointId")]
    public string? EndpointId { get; set; }

    [JsonPropertyName("heartbeatIntervalSeconds")]
    public int HeartbeatIntervalSeconds { get; set; } = 60;

    private static readonly JsonSerializerOptions Options = new()
    {
        WriteIndented = true,
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
    };

    /// <summary>
    /// %ProgramData%\EndpointAgent — machine scope, so the configuration is
    /// shared by every user and protected by the directory's ACL rather than
    /// hidden somewhere obscure.
    /// </summary>
    public static string DirectoryPath => Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData),
        "EndpointAgent");

    public static string FilePath => Path.Combine(DirectoryPath, "agent.config.json");

    public static AgentConfig Load()
    {
        if (!File.Exists(FilePath))
        {
            throw new FileNotFoundException(
                $"Agent configuration not found at {FilePath}. " +
                "The installer writes this file; reinstall the package.", FilePath);
        }

        var json = File.ReadAllText(FilePath);
        var config = JsonSerializer.Deserialize<AgentConfig>(json, Options)
            ?? throw new InvalidDataException($"{FilePath} is not valid agent configuration.");

        if (string.IsNullOrWhiteSpace(config.ServerUrl))
        {
            throw new InvalidDataException("serverUrl is missing from the agent configuration.");
        }
        return config;
    }

    public void Save()
    {
        Directory.CreateDirectory(DirectoryPath);
        // Write then move, so a crash mid-write cannot leave a truncated config
        // that would strand the agent on next start.
        var temp = FilePath + ".tmp";
        File.WriteAllText(temp, JsonSerializer.Serialize(this, Options));
        File.Move(temp, FilePath, overwrite: true);
    }
}
