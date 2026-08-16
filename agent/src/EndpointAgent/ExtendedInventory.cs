using System.Runtime.InteropServices;
using System.Runtime.Versioning;
using System.Text.Json.Serialization;
using Microsoft.Win32;

namespace EndpointAgent;

public sealed record SoftwareEntry(
    [property: JsonPropertyName("name")] string Name,
    [property: JsonPropertyName("version")] string? Version,
    [property: JsonPropertyName("publisher")] string? Publisher);

/// <summary>
/// Extended asset inventory the console shows for triage: OS build, CPU, RAM,
/// system-drive space, uptime, and the installed-software list.
///
/// Collected in the LocalSystem service, where the registry and drive APIs are
/// all available, and reported only every few hours (not every heartbeat) --
/// enumerating installed software is the one non-trivial cost here. Everything
/// is best-effort: any field that cannot be read comes back null/zero rather
/// than failing the whole collection. This is asset data only -- no document
/// contents, no activity (CLAUDE.md section 13).
/// </summary>
[SupportedOSPlatform("windows")]
public sealed record ExtendedInventory(
    [property: JsonPropertyName("osBuild")] string? OsBuild,
    [property: JsonPropertyName("cpu")] string? Cpu,
    [property: JsonPropertyName("cpuCores")] int CpuCores,
    [property: JsonPropertyName("memoryTotalMb")] long MemoryTotalMb,
    [property: JsonPropertyName("memoryFreeMb")] long MemoryFreeMb,
    [property: JsonPropertyName("diskTotalGb")] long DiskTotalGb,
    [property: JsonPropertyName("diskFreeGb")] long DiskFreeGb,
    [property: JsonPropertyName("diskFreePercent")] int DiskFreePercent,
    [property: JsonPropertyName("uptimeSeconds")] long UptimeSeconds,
    [property: JsonPropertyName("softwareCount")] int SoftwareCount,
    [property: JsonPropertyName("software")] IReadOnlyList<SoftwareEntry> Software)
{
    public static ExtendedInventory Collect()
    {
        var (memTotal, memFree) = MemoryMb();
        var (diskTotal, diskFree, diskPct) = SystemDrive();
        var software = InstalledSoftware();
        return new ExtendedInventory(
            OsBuild: OsBuildString(),
            Cpu: CpuName(),
            CpuCores: Environment.ProcessorCount,
            MemoryTotalMb: memTotal,
            MemoryFreeMb: memFree,
            DiskTotalGb: diskTotal,
            DiskFreeGb: diskFree,
            DiskFreePercent: diskPct,
            UptimeSeconds: Environment.TickCount64 / 1000,
            SoftwareCount: software.Count,
            Software: software);
    }

    private static string? OsBuildString()
    {
        try
        {
            using var key = Registry.LocalMachine.OpenSubKey(
                @"SOFTWARE\Microsoft\Windows NT\CurrentVersion");
            var build = key?.GetValue("CurrentBuildNumber") as string;
            if (string.IsNullOrWhiteSpace(build)) return null;
            var ubr = key?.GetValue("UBR");
            return ubr is int u ? $"{build}.{u}" : build;
        }
        catch (Exception) { return null; }
    }

    private static string? CpuName()
    {
        try
        {
            using var key = Registry.LocalMachine.OpenSubKey(
                @"HARDWARE\DESCRIPTION\System\CentralProcessor\0");
            return (key?.GetValue("ProcessorNameString") as string)?.Trim();
        }
        catch (Exception) { return null; }
    }

    private static (long TotalMb, long FreeMb) MemoryMb()
    {
        try
        {
            var status = new MEMORYSTATUSEX { dwLength = (uint)Marshal.SizeOf<MEMORYSTATUSEX>() };
            if (GlobalMemoryStatusEx(ref status))
            {
                return ((long)(status.ullTotalPhys / (1024 * 1024)),
                        (long)(status.ullAvailPhys / (1024 * 1024)));
            }
        }
        catch (Exception) { /* fall through */ }
        return (0, 0);
    }

    private static (long TotalGb, long FreeGb, int FreePercent) SystemDrive()
    {
        try
        {
            var root = Path.GetPathRoot(Environment.SystemDirectory) ?? "C:\\";
            var drive = new DriveInfo(root);
            if (drive.IsReady && drive.TotalSize > 0)
            {
                const long gb = 1024L * 1024 * 1024;
                var pct = (int)(drive.TotalFreeSpace * 100 / drive.TotalSize);
                return (drive.TotalSize / gb, drive.TotalFreeSpace / gb, pct);
            }
        }
        catch (Exception) { /* fall through */ }
        return (0, 0, 0);
    }

    /// <summary>
    /// Installed programs, from the Uninstall registry keys Windows itself uses
    /// to populate "Apps &amp; Features". Reads the 64- and 32-bit machine hives;
    /// entries without a display name, and Windows update/component rows, are
    /// skipped. Deduplicated by name+version and sorted, capped so a machine with
    /// an enormous list cannot bloat a heartbeat.
    /// </summary>
    private static IReadOnlyList<SoftwareEntry> InstalledSoftware()
    {
        const int cap = 2000;
        var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        var list = new List<SoftwareEntry>();

        void Scan(RegistryKey? root)
        {
            if (root is null) return;
            foreach (var name in root.GetSubKeyNames())
            {
                if (list.Count >= cap) return;
                try
                {
                    using var sub = root.OpenSubKey(name);
                    if (sub is null) continue;
                    var display = (sub.GetValue("DisplayName") as string)?.Trim();
                    if (string.IsNullOrWhiteSpace(display)) continue;
                    // Skip OS components/updates that clutter the list.
                    if (sub.GetValue("SystemComponent") is int sc && sc == 1) continue;
                    if (sub.GetValue("ParentKeyName") is not null) continue;

                    var version = (sub.GetValue("DisplayVersion") as string)?.Trim();
                    var publisher = (sub.GetValue("Publisher") as string)?.Trim();
                    var dedup = display + "\u0000" + version;
                    if (!seen.Add(dedup)) continue;
                    list.Add(new SoftwareEntry(
                        display!,
                        string.IsNullOrWhiteSpace(version) ? null : version,
                        string.IsNullOrWhiteSpace(publisher) ? null : publisher));
                }
                catch (Exception) { /* skip one bad entry, keep going */ }
            }
        }

        try
        {
            using (var hklm64 = RegistryKey.OpenBaseKey(RegistryHive.LocalMachine, RegistryView.Registry64))
            using (var k = hklm64.OpenSubKey(@"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"))
                Scan(k);
            using (var hklm32 = RegistryKey.OpenBaseKey(RegistryHive.LocalMachine, RegistryView.Registry32))
            using (var k = hklm32.OpenSubKey(@"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"))
                Scan(k);
        }
        catch (Exception) { /* return whatever was gathered */ }

        list.Sort((a, b) => string.Compare(a.Name, b.Name, StringComparison.OrdinalIgnoreCase));
        return list;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct MEMORYSTATUSEX
    {
        public uint dwLength;
        public uint dwMemoryLoad;
        public ulong ullTotalPhys;
        public ulong ullAvailPhys;
        public ulong ullTotalPageFile;
        public ulong ullAvailPageFile;
        public ulong ullTotalVirtual;
        public ulong ullAvailVirtual;
        public ulong ullAvailExtendedVirtual;
    }

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool GlobalMemoryStatusEx(ref MEMORYSTATUSEX lpBuffer);
}
