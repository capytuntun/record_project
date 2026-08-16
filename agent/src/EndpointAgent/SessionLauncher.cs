using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Runtime.Versioning;

namespace EndpointAgent;

/// <summary>
/// Launches the tray helper in the interactive user's session from the service.
///
/// The service runs as LocalSystem in session 0, which has no visible desktop.
/// A tray icon must live in the logged-in user's session, so the service
/// duplicates that user's token and starts EndpointAgent.exe there. This is
/// what makes the icon appear immediately after install, without waiting for
/// the next logon.
///
/// Requires the SE_ASSIGNPRIMARYTOKEN / SE_INCREASE_QUOTA privileges that
/// LocalSystem has and a normal user process does not -- so this path only
/// works from the actual service, and is exercised there, not from an
/// interactively-run agent.
/// </summary>
[SupportedOSPlatform("windows")]
public static class SessionLauncher
{
    /// <summary>Session id of the user at the physical console, or null if none.</summary>
    public static uint? ActiveConsoleSession()
    {
        var id = WTSGetActiveConsoleSessionId();
        // 0xFFFFFFFF means no session is attached to the console.
        if (id == 0xFFFFFFFF) return null;
        return id;
    }

    /// <summary>
    /// Start "EndpointAgent.exe tray" as the user of the active console session.
    /// Returns true if a process was started. Does nothing and returns false if
    /// no user is logged in.
    /// </summary>
    public static bool LaunchTrayInActiveSession(string exePath)
    {
        var session = ActiveConsoleSession();
        if (session is null) return false;

        if (!WTSQueryUserToken(session.Value, out var userToken))
        {
            // ERROR_NO_TOKEN etc. -- nobody interactive to launch for.
            return false;
        }

        var primary = IntPtr.Zero;
        var environment = IntPtr.Zero;
        try
        {
            if (!DuplicateTokenEx(userToken, MAXIMUM_ALLOWED, IntPtr.Zero,
                    SecurityImpersonation, TokenPrimary, out primary))
            {
                return false;
            }

            CreateEnvironmentBlock(out environment, primary, false);

            var commandLine = $"\"{exePath}\" tray";
            var startupInfo = new StartupInfo
            {
                cb = Marshal.SizeOf<StartupInfo>(),
                lpDesktop = @"winsta0\default",   // the interactive desktop
            };

            var created = CreateProcessAsUser(
                primary, null, commandLine, IntPtr.Zero, IntPtr.Zero,
                inheritHandles: false,
                dwCreationFlags: CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT,
                environment, null, ref startupInfo, out var processInfo);

            if (!created) return false;

            CloseHandle(processInfo.hThread);
            CloseHandle(processInfo.hProcess);
            return true;
        }
        finally
        {
            if (environment != IntPtr.Zero) DestroyEnvironmentBlock(environment);
            if (primary != IntPtr.Zero) CloseHandle(primary);
            CloseHandle(userToken);
        }
    }

    /// <summary>Is a process of this name already running in the given session?</summary>
    public static bool ProcessRunningInSession(string processName, uint session)
    {
        foreach (var p in Process.GetProcessesByName(processName))
        {
            using (p)
            {
                try
                {
                    if ((uint)p.SessionId == session) return true;
                }
                catch (Exception) { /* process exited between enumerate and read */ }
            }
        }
        return false;
    }

    // --- Win32 ------------------------------------------------------------

    private const uint MAXIMUM_ALLOWED = 0x02000000;
    private const uint CREATE_NO_WINDOW = 0x08000000;
    private const uint CREATE_UNICODE_ENVIRONMENT = 0x00000400;
    private const int SecurityImpersonation = 2;
    private const int TokenPrimary = 1;

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct StartupInfo
    {
        public int cb;
        public string? lpReserved;
        public string? lpDesktop;
        public string? lpTitle;
        public int dwX, dwY, dwXSize, dwYSize, dwXCountChars, dwYCountChars;
        public int dwFillAttribute, dwFlags;
        public short wShowWindow, cbReserved2;
        public IntPtr lpReserved2, hStdInput, hStdOutput, hStdError;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct ProcessInformation
    {
        public IntPtr hProcess, hThread;
        public int dwProcessId, dwThreadId;
    }

    [DllImport("kernel32.dll")]
    private static extern uint WTSGetActiveConsoleSessionId();

    [DllImport("wtsapi32.dll", SetLastError = true)]
    private static extern bool WTSQueryUserToken(uint sessionId, out IntPtr token);

    [DllImport("advapi32.dll", SetLastError = true)]
    private static extern bool DuplicateTokenEx(
        IntPtr existingToken, uint desiredAccess, IntPtr attributes,
        int impersonationLevel, int tokenType, out IntPtr newToken);

    [DllImport("userenv.dll", SetLastError = true)]
    private static extern bool CreateEnvironmentBlock(out IntPtr env, IntPtr token, bool inherit);

    [DllImport("userenv.dll", SetLastError = true)]
    private static extern bool DestroyEnvironmentBlock(IntPtr env);

    [DllImport("advapi32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    private static extern bool CreateProcessAsUser(
        IntPtr token, string? applicationName, string? commandLine,
        IntPtr processAttributes, IntPtr threadAttributes, bool inheritHandles,
        uint dwCreationFlags, IntPtr environment, string? currentDirectory,
        ref StartupInfo startupInfo, out ProcessInformation processInformation);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool CloseHandle(IntPtr handle);
}
