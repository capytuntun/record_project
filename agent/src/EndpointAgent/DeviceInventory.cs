using Microsoft.Win32;

namespace EndpointAgent;

/// <summary>
/// The device facts the agent reports.
///
/// Deliberately limited to what the console needs to identify and triage a
/// machine: name, OS, agent version, and the logged-on account. No document
/// contents, no keystrokes, no browsing data -- data minimisation, CLAUDE.md
/// section 13. Anything added here must clear the section 31 checklist first.
/// </summary>
public sealed record DeviceInventory(
    string DeviceName,
    string OsName,
    string OsVersion,
    string AgentVersion,
    string? LocalUser)
{
    public static DeviceInventory Collect()
    {
        return new DeviceInventory(
            DeviceName: Environment.MachineName,
            OsName: ReadProductName(),
            OsVersion: Environment.OSVersion.Version.ToString(),
            AgentVersion: typeof(DeviceInventory).Assembly.GetName().Version?.ToString(3) ?? "0.1.0",
            LocalUser: ReadConsoleUser());
    }

    private static string ReadProductName()
    {
        try
        {
            using var key = Registry.LocalMachine.OpenSubKey(
                @"SOFTWARE\Microsoft\Windows NT\CurrentVersion");
            var product = key?.GetValue("ProductName") as string;
            var display = key?.GetValue("DisplayVersion") as string;
            if (string.IsNullOrWhiteSpace(product)) return "Windows";
            return string.IsNullOrWhiteSpace(display) ? product : $"{product} {display}";
        }
        catch (Exception)
        {
            return "Windows";
        }
    }

    /// <summary>
    /// Who the machine is assigned to, for the console's "User" column.
    ///
    /// Running as LocalSystem, Environment.UserName is "SYSTEM", which tells an
    /// administrator nothing. Windows records the last interactive sign-in here,
    /// which is the answer they actually want -- and reading one registry value
    /// is far cheaper than a WMI query per heartbeat.
    /// </summary>
    private static string? ReadConsoleUser()
    {
        try
        {
            using var key = Registry.LocalMachine.OpenSubKey(
                @"SOFTWARE\Microsoft\Windows\CurrentVersion\Authentication\LogonUI");
            var user = key?.GetValue("LastLoggedOnSAMUser") as string
                       ?? key?.GetValue("LastLoggedOnUser") as string;
            return string.IsNullOrWhiteSpace(user) ? null : user;
        }
        catch (Exception)
        {
            // Best effort only; an unknown user must never stop a heartbeat.
            return null;
        }
    }
}
