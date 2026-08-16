using EndpointAgent;

// Command-line surface, in addition to running as a Windows service.
//
// CLAUDE.md section 19 is explicit: the agent must not use hidden tricks to
// resist legitimate enterprise IT maintenance. So the password gates *changing*
// things, never *seeing* them -- `status` and `--help` are open to anyone, and
// the documented removal path is printed by `uninstall-info` without a password.
if (args.Length > 0)
{
    return args[0].ToLowerInvariant() switch
    {
        "status" => Commands.Status(),
        "set-server" => Commands.SetServer(args),
        "reset-enrollment" => Commands.ResetEnrollment(args),
        "uninstall-info" => Commands.UninstallInfo(),
        // The user-session presence: tray icon + screen capture. Autostarted at
        // logon by the installer's Run key.
        "tray" => TrayApplication.Run(),
        // Capture helpers. capture-frame is a single shot for testing.
        "capture-frame" => CaptureCommands.CaptureFrame(args),
        "capture-stream" => CaptureCommands.CaptureStream(args),
        "capture-bench" => CaptureCommands.Bench(args),
        "list-monitors" => CaptureCommands.ListMonitors(),
        "--help" or "-h" or "help" => Commands.Help(),
        _ => Commands.Unknown(args[0]),
    };
}

var builder = Host.CreateApplicationBuilder(args);

builder.Services.AddWindowsService(options => options.ServiceName = "EndpointAgent");
builder.Services.AddSingleton<LocalNotifier>();
builder.Services.AddHostedService<AgentWorker>();

builder.Logging.AddEventLog(settings => settings.SourceName = "EndpointAgent");

var host = builder.Build();
host.Run();
return 0;
