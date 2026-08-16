using System.Text.Json;
using System.Text.Json.Serialization;

namespace EndpointAgent;

/// <summary>
/// One refused uninstall attempt, as the MSI custom action recorded it.
/// There is a line per wrong password, not one summary line per uninstall.
/// </summary>
public sealed record UninstallAttempt(
    [property: JsonPropertyName("at")] string? At,
    [property: JsonPropertyName("outcome")] string? Outcome,
    [property: JsonPropertyName("localUser")] string? LocalUser);

/// <summary>
/// The handover point between the installer and the agent.
///
/// When someone tries to remove the agent without the administrator password,
/// the MSI custom action refuses and appends a line here. The agent forwards
/// those lines to the management server on its next heartbeat, so an
/// administrator learns that somebody tried (CLAUDE.md sections 16 and 17).
///
/// The installer cannot report this itself: it holds no device credential, and
/// giving it one would put a long-lived secret in a process that runs for a few
/// seconds under whoever launched the uninstall.
/// </summary>
public static class UninstallAttemptLog
{
    public static string FilePath => Path.Combine(AgentConfig.DirectoryPath, "uninstall-attempts.log");

    /// <summary>
    /// Take everything recorded so far, leaving the log empty.
    ///
    /// Claims the file by renaming it first, so an uninstall attempt happening
    /// while this runs appends to a fresh file and is reported next round rather
    /// than being deleted unread.
    /// </summary>
    public static IReadOnlyList<UninstallAttempt> TakePending(string path)
    {
        var claimed = path + ".sending";
        try
        {
            if (!File.Exists(path)) return [];

            // A leftover .sending means a previous run died mid-report; its
            // contents are still unreported, so fold them back in.
            if (File.Exists(claimed))
            {
                File.AppendAllText(claimed, File.ReadAllText(path));
                File.Delete(path);
            }
            else
            {
                File.Move(path, claimed);
            }
        }
        catch (IOException)
        {
            return [];      // locked by the installer right now; try next cycle
        }
        catch (UnauthorizedAccessException)
        {
            return [];
        }

        var attempts = new List<UninstallAttempt>();
        try
        {
            foreach (var line in File.ReadAllLines(claimed))
            {
                if (string.IsNullOrWhiteSpace(line)) continue;
                try
                {
                    var parsed = JsonSerializer.Deserialize<UninstallAttempt>(line);
                    if (parsed is not null) attempts.Add(parsed);
                }
                catch (JsonException)
                {
                    // A truncated or corrupted line is not worth failing over.
                }
            }
        }
        catch (IOException)
        {
            return [];
        }

        return attempts;
    }

    /// <summary>Drop the claimed batch once the server has it.</summary>
    public static void Commit(string path)
    {
        try { File.Delete(path + ".sending"); }
        catch (IOException) { }
        catch (UnauthorizedAccessException) { }
    }
}
