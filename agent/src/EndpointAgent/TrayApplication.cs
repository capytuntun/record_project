using System.Drawing;
using System.Runtime.Versioning;
using System.Windows.Forms;
using Microsoft.Extensions.Logging;

namespace EndpointAgent;

/// <summary>
/// The user-session presence of the agent: a notification-area (system tray)
/// icon plus the screen-capture streamer.
///
/// Two reasons this runs in the user session rather than the service:
///
///  1. Transparency. The whole point of the icon is that the person using the
///     machine can SEE the agent is running -- which is only possible from
///     their own session. A service in session 0 has no desktop and no tray.
///     This is the disclosure side of "authorised, non-covert management".
///
///  2. Screen capture actually works here. A session-0 service captures a blank
///     desktop; this process shares the user's desktop, so it captures what the
///     user sees, with no cross-session bridge needed.
///
/// It reads the device credential the service stored (DPAPI machine scope, so a
/// user-session process can decrypt it) and streams only while a viewer is
/// watching -- the server starts and stops it.
/// </summary>
[SupportedOSPlatform("windows")]
public static class TrayApplication
{
    public static int Run()
    {
        // Single instance per session. Both the logon Run key and the service's
        // session launcher may try to start the tray; the second one to arrive
        // finds the mutex held and exits, so the user only ever sees one icon.
        // The Local\ prefix scopes the mutex to this session, which is what we
        // want -- one icon per interactive desktop.
        using var mutex = new Mutex(initiallyOwned: true, @"Local\EndpointAgentTray", out var isNew);
        if (!isNew)
        {
            return 0;
        }

        // This process's only jobs are the tray icon and screen streaming --
        // neither latency-critical. Run it below normal so the screen capture
        // always yields to whatever the user is actually doing in the
        // foreground; the felt smoothness of their apps matters more than the
        // agent's absolute throughput. The service process (heartbeat) stays at
        // normal priority. Best effort: a policy that forbids the change must
        // not stop the tray from running.
        try
        {
            using var self = System.Diagnostics.Process.GetCurrentProcess();
            self.PriorityClass = System.Diagnostics.ProcessPriorityClass.BelowNormal;
        }
        catch (Exception)
        {
            // Leave priority at default; not worth failing over.
        }

        AgentConfig config;
        try
        {
            config = AgentConfig.Load();
        }
        catch (Exception ex)
        {
            // No config means the service has not been installed/enrolled. The
            // tray helper has nothing to show; exit quietly rather than pop an
            // error at every logon.
            Console.Error.WriteLine($"tray: no agent configuration ({ex.Message}); exiting.");
            return 0;
        }

        ApplicationConfiguration.Initialize();
        using var context = new TrayContext(config);
        Application.Run(context);
        return 0;
    }
}

[SupportedOSPlatform("windows")]
internal sealed class TrayContext : ApplicationContext
{
    private readonly NotifyIcon _icon;
    private readonly AgentConfig _config;
    private readonly CancellationTokenSource _cts = new();
    private Icon? _ownedIcon;

    public TrayContext(AgentConfig config)
    {
        _config = config;

        _icon = new NotifyIcon
        {
            Icon = BuildIcon(),
            Visible = true,
            Text = Truncate($"端點管理 Agent\n本機由企業 IT 管理\n伺服器：{HostOf(config.ServerUrl)}"),
            ContextMenuStrip = BuildMenu(),
        };
        _icon.DoubleClick += (_, _) => ShowStatus();

        StartScreenStreamer();
    }

    private void StartScreenStreamer()
    {
        // Reuse the same streamer the agent already has; in this (interactive)
        // process it captures in-process, which is exactly the tested path.
        using var loggerFactory = LoggerFactory.Create(b => b.AddEventLog(
            new Microsoft.Extensions.Logging.EventLog.EventLogSettings { SourceName = "EndpointAgent" }));
        var logger = loggerFactory.CreateLogger<ScreenStreamer>();
        var streamer = new ScreenStreamer(logger, _config.ServerUrl);
        _ = Task.Run(() => streamer.RunAsync(_cts.Token), _cts.Token);
    }

    private ContextMenuStrip BuildMenu()
    {
        var menu = new ContextMenuStrip();
        menu.Items.Add("此電腦由企業 IT 管理").Enabled = false;
        menu.Items.Add(new ToolStripSeparator());
        menu.Items.Add("狀態…", null, (_, _) => ShowStatus());
        menu.Items.Add("關於與 IT 聯絡方式…", null, (_, _) => ShowAbout());
        return menu;
    }

    private void ShowStatus()
    {
        var enrolled = !string.IsNullOrEmpty(_config.EndpointId);
        MessageBox.Show(
            $"端點管理 Agent\n\n" +
            $"管理伺服器：{_config.ServerUrl}\n" +
            $"組織：{_config.OrganizationId ?? "（未設定）"}\n" +
            $"註冊狀態：{(enrolled ? "已註冊" : "尚未註冊")}\n" +
            $"端點識別碼：{_config.EndpointId ?? "（尚未取得）"}\n\n" +
            "此電腦由貴公司 IT 依企業政策管理。",
            "端點管理 Agent", MessageBoxButtons.OK, MessageBoxIcon.Information);
    }

    private void ShowAbout()
    {
        MessageBox.Show(
            "此電腦已安裝企業端點管理 Agent，由貴公司 IT 部門依內部政策與你已簽署的同意書進行管理。\n\n" +
            "如需協助或有疑問，請聯絡貴公司 IT 部門。\n\n" +
            "此 Agent 不會阻擋 IT 以正常方式維護或移除。",
            "關於", MessageBoxButtons.OK, MessageBoxIcon.Information);
    }

    /// <summary>
    /// A small drawn icon, so no .ico asset needs to ship. A filled shield-ish
    /// badge in the accent blue -- recognisable in the tray, not alarming.
    /// </summary>
    private Icon BuildIcon()
    {
        using var bitmap = new Bitmap(32, 32);
        using (var g = Graphics.FromImage(bitmap))
        {
            g.SmoothingMode = System.Drawing.Drawing2D.SmoothingMode.AntiAlias;
            g.Clear(Color.Transparent);
            using var body = new SolidBrush(Color.FromArgb(42, 120, 214));
            var shield = new[]
            {
                new Point(16, 2), new Point(29, 7), new Point(29, 17),
                new Point(16, 30), new Point(3, 17), new Point(3, 7),
            };
            g.FillPolygon(body, shield);
            using var pen = new Pen(Color.White, 3f)
            {
                StartCap = System.Drawing.Drawing2D.LineCap.Round,
                EndCap = System.Drawing.Drawing2D.LineCap.Round,
            };
            g.DrawLines(pen, new[] { new Point(10, 16), new Point(15, 21), new Point(23, 11) });
        }
        _ownedIcon = Icon.FromHandle(bitmap.GetHicon());
        return _ownedIcon;
    }

    private static string HostOf(string url)
    {
        var i = url.IndexOf("://", StringComparison.Ordinal);
        return i >= 0 ? url[(i + 3)..].TrimEnd('/') : url;
    }

    private static string Truncate(string s) => s.Length <= 127 ? s : s[..127];

    protected override void Dispose(bool disposing)
    {
        if (disposing)
        {
            _cts.Cancel();
            _icon.Visible = false;
            _icon.Dispose();
            if (_ownedIcon is not null)
            {
                DestroyIcon(_ownedIcon.Handle);
                _ownedIcon.Dispose();
            }
            _cts.Dispose();
        }
        base.Dispose(disposing);
    }

    [System.Runtime.InteropServices.DllImport("user32.dll", SetLastError = true)]
    private static extern bool DestroyIcon(IntPtr handle);
}
