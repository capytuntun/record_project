using System.Diagnostics;

namespace EndpointAgent;

/// <summary>
/// The agent's main loop: enroll once, then heartbeat until stopped.
///
/// Every state the agent can get stuck in surfaces somewhere an administrator
/// will look -- the Windows event log and the agent log file -- rather than
/// failing silently. A monitoring agent nobody can diagnose is worse than none.
/// </summary>
public sealed class AgentWorker(
    ILogger<AgentWorker> logger,
    LocalNotifier notifier) : BackgroundService
{
    private AgentConfig _config = null!;

    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        try
        {
            _config = AgentConfig.Load();
        }
        catch (Exception ex)
        {
            logger.LogCritical(ex, "無法載入設定檔，Agent 停止。");
            notifier.Error("設定錯誤", $"無法讀取 {AgentConfig.FilePath}：{ex.Message}");
            return;
        }

        logger.LogInformation("Agent 啟動，管理伺服器 {Server}", _config.ServerUrl);

        using var client = new ManagementClient(_config.ServerUrl);

        var credential = CredentialStore.Load();
        if (credential is null)
        {
            credential = await EnrollAsync(client, stoppingToken);
            if (credential is null) return;   // reason already reported
        }

        // Screen capture does NOT run here. As a session-0 service this process
        // cannot see the user's desktop; the tray helper (EndpointAgent.exe
        // tray), which runs in the user session, owns screen streaming and the
        // notification-area icon. The service keeps that helper alive in the
        // active session so it appears right after install, without waiting for
        // the next logon.
        var trayTask = OperatingSystem.IsWindows()
            ? EnsureTrayRunningAsync(stoppingToken)
            : Task.CompletedTask;

        await HeartbeatLoopAsync(client, credential, stoppingToken);
        await trayTask;
    }

    private async Task<string?> EnrollAsync(ManagementClient client, CancellationToken ct)
    {
        if (string.IsNullOrWhiteSpace(_config.EnrollmentToken))
        {
            const string message =
                "找不到裝置憑證，安裝包中也沒有註冊憑證。請向 IT 索取新的安裝包重新安裝。";
            logger.LogCritical("{Message}", message);
            notifier.Error("需要重新註冊", message);
            return null;
        }

        var inventory = DeviceInventory.Collect();

        // Retry only transport failures. A rejection is a decision, not a
        // glitch: retrying it just buries the reason in log noise.
        for (var attempt = 1; ; attempt++)
        {
            try
            {
                var result = await client.EnrollAsync(_config.EnrollmentToken!, inventory, ct);

                CredentialStore.Save(result.DeviceCredential);
                _config.EndpointId = result.EndpointId;
                _config.HeartbeatIntervalSeconds = result.HeartbeatIntervalSeconds;
                // The one-time token has been spent; do not leave it on disk.
                _config.EnrollmentToken = null;
                _config.Save();

                logger.LogInformation("註冊成功，Endpoint ID {EndpointId}", result.EndpointId);
                notifier.Info("註冊成功", $"此電腦已納入企業端點管理，識別碼 {result.EndpointId}。");
                return result.DeviceCredential;
            }
            catch (EnrollmentRejectedException ex)
            {
                logger.LogCritical("註冊遭拒（{Reason}）：{Message}", ex.Reason ?? "unknown", ex.Message);
                notifier.Error("註冊失敗", ex.Message);
                return null;
            }
            catch (OperationCanceledException) when (ct.IsCancellationRequested)
            {
                return null;
            }
            catch (Exception ex)
            {
                var delay = BackoffFor(attempt);
                logger.LogWarning(ex, "無法連線至管理伺服器，{Seconds} 秒後重試（第 {Attempt} 次）",
                    delay.TotalSeconds, attempt);
                if (attempt == 5)
                {
                    notifier.Warning("尚未完成註冊",
                        $"無法連線至 {_config.ServerUrl}，將持續重試。若持續失敗請聯絡 IT。");
                }
                await SafeDelayAsync(delay, ct);
            }
        }
    }

    /// <summary>
    /// Hand the server anything the uninstaller refused, then clear the log.
    ///
    /// Deliberately swallows its own errors: failing to report a tamper attempt
    /// must not stop the heartbeat, which is what keeps the endpoint visible at
    /// all. An unreported batch stays on disk and goes out next cycle.
    /// </summary>
    private async Task ReportUninstallAttemptsAsync(
        ManagementClient client, string credential, CancellationToken ct)
    {
        var path = UninstallAttemptLog.FilePath;
        var pending = UninstallAttemptLog.TakePending(path);
        if (pending.Count == 0) return;

        try
        {
            await client.ReportUninstallAttemptsAsync(credential, pending, ct);
            UninstallAttemptLog.Commit(path);
            logger.LogWarning("已回報 {Count} 次被拒絕的移除嘗試。", pending.Count);
        }
        catch (CredentialRejectedException)
        {
            throw;      // the caller handles a dead credential
        }
        catch (Exception ex)
        {
            // Left claimed on disk; TakePending folds it back in next time.
            logger.LogWarning("回報移除嘗試失敗，下次再試：{Message}", ex.Message);
        }
    }

    private async Task HeartbeatLoopAsync(
        ManagementClient client, string credential, CancellationToken ct)
    {
        var interval = TimeSpan.FromSeconds(Math.Max(10, _config.HeartbeatIntervalSeconds));
        var failures = 0;

        // Extended asset inventory (software list, disk, RAM, ...) is expensive
        // to gather, so it rides only an occasional heartbeat: once at startup,
        // then every few hours. Every other heartbeat sends null for it.
        var inventoryInterval = TimeSpan.FromHours(6);
        var lastInventoryAt = DateTime.MinValue;

        while (!ct.IsCancellationRequested)
        {
            try
            {
                var inventory = DeviceInventory.Collect();

                ExtendedInventory? extended = null;
                if (OperatingSystem.IsWindows() &&
                    DateTime.UtcNow - lastInventoryAt >= inventoryInterval)
                {
                    try
                    {
                        extended = ExtendedInventory.Collect();
                        lastInventoryAt = DateTime.UtcNow;
                    }
                    catch (Exception ex)
                    {
                        logger.LogDebug(ex, "擷取延伸資產清單失敗，稍後重試。");
                    }
                }

                var result = await client.HeartbeatAsync(credential, inventory, extended, ct);

                failures = 0;
                interval = TimeSpan.FromSeconds(Math.Max(10, result.HeartbeatIntervalSeconds));

                await ReportUninstallAttemptsAsync(client, credential, ct);

                foreach (var warning in result.Warnings ?? [])
                {
                    // This is the "電腦端跳告警" path: the server told us the
                    // credential is running out while it still authenticates.
                    logger.LogWarning("伺服器告警 {Code}：{Message}", warning.Code, warning.Message);
                    notifier.Warning("端點管理 Agent", warning.Message);

                    if (warning.Action == "rotate")
                    {
                        credential = await RotateAsync(client, credential, ct) ?? credential;
                    }
                }
            }
            catch (CredentialRejectedException ex)
            {
                // Revoked, expired, or the endpoint was disabled. Nothing the
                // agent can do on its own; say so where IT will see it.
                logger.LogCritical("裝置憑證已失效：{Message}", ex.Message);
                notifier.Error("端點管理 Agent 已停止回報", ex.Message);
                CredentialStore.Clear();
                return;
            }
            catch (OperationCanceledException) when (ct.IsCancellationRequested)
            {
                break;
            }
            catch (Exception ex)
            {
                failures++;
                logger.LogWarning(ex, "心跳失敗（連續 {Failures} 次）", failures);
                if (failures == 10)
                {
                    notifier.Warning("端點管理 Agent",
                        $"已連續 {failures} 次無法連線至管理伺服器，請確認網路或聯絡 IT。");
                }
            }

            await SafeDelayAsync(interval, ct);
        }

        logger.LogInformation("Agent 停止。");
    }

    /// <summary>
    /// Keep the tray helper running in whatever session is at the console.
    ///
    /// Only meaningful when this process is the LocalSystem service (that is the
    /// only context with the privilege to start a process in another session).
    /// Run interactively, CreateProcessAsUser will simply fail and this loop is
    /// a no-op -- which is fine, because an interactive agent would be in the
    /// user session already.
    /// </summary>
    [System.Runtime.Versioning.SupportedOSPlatform("windows")]
    private async Task EnsureTrayRunningAsync(CancellationToken ct)
    {
        var exePath = Environment.ProcessPath;
        if (exePath is null) return;

        var quickCheck = TimeSpan.FromSeconds(20);
        var steadyCheck = TimeSpan.FromSeconds(60);

        while (!ct.IsCancellationRequested)
        {
            var trayUp = false;
            try
            {
                var session = SessionLauncher.ActiveConsoleSession();
                // The service itself is "EndpointAgent" too, but in session 0; a
                // match in the *console* session means the tray is already up.
                trayUp = session is not null &&
                    SessionLauncher.ProcessRunningInSession("EndpointAgent", session.Value);

                if (session is not null && !trayUp &&
                    SessionLauncher.LaunchTrayInActiveSession(exePath))
                {
                    logger.LogInformation("已於使用者工作階段 {Session} 啟動常駐程式。", session);
                    trayUp = true;
                }
            }
            catch (Exception ex)
            {
                logger.LogDebug(ex, "啟動常駐程式時發生問題。");
            }

            // Poll briskly until the tray is confirmed up (so it appears quickly
            // after a logon or a crash), then back off: while it is running there
            // is nothing to do, and the process enumeration each pass is not free.
            try { await Task.Delay(trayUp ? steadyCheck : quickCheck, ct); }
            catch (OperationCanceledException) { break; }
        }
    }

    private async Task<string?> RotateAsync(
        ManagementClient client, string credential, CancellationToken ct)
    {
        try
        {
            var rotated = await client.RotateCredentialAsync(credential, ct);
            CredentialStore.Save(rotated.DeviceCredential);
            logger.LogInformation("裝置憑證已更新，下次到期 {ExpiresAt}", rotated.CredentialExpiresAt);
            return rotated.DeviceCredential;
        }
        catch (Exception ex)
        {
            // Keep using the current credential: it still works until it does
            // not, and the warning will fire again on the next heartbeat.
            logger.LogWarning(ex, "憑證更新失敗，將於下次心跳重試。");
            return null;
        }
    }

    /// <summary>Exponential backoff, capped so a long outage does not stretch
    /// the retry interval into hours.</summary>
    private static TimeSpan BackoffFor(int attempt) =>
        TimeSpan.FromSeconds(Math.Min(300, Math.Pow(2, Math.Min(attempt, 8))));

    private static async Task SafeDelayAsync(TimeSpan delay, CancellationToken ct)
    {
        try { await Task.Delay(delay, ct); }
        catch (OperationCanceledException) { /* shutting down */ }
    }
}

/// <summary>
/// Surfaces agent state where an administrator will actually find it: the
/// Windows event log, which is what enterprise IT already collects.
/// </summary>
public sealed class LocalNotifier(ILogger<LocalNotifier> logger)
{
    private const string Source = "EndpointAgent";

    public void Info(string title, string message) => Write(title, message, EventLogEntryType.Information, 1000);
    public void Warning(string title, string message) => Write(title, message, EventLogEntryType.Warning, 2000);
    public void Error(string title, string message) => Write(title, message, EventLogEntryType.Error, 3000);

    private void Write(string title, string message, EventLogEntryType type, int id)
    {
        try
        {
            if (!OperatingSystem.IsWindows()) return;
            // The installer registers the source; creating it here would need
            // administrator rights the service may not want to rely on.
            if (!EventLog.SourceExists(Source)) return;
            EventLog.WriteEntry(Source, $"{title}\n\n{message}", type, id);
        }
        catch (Exception ex)
        {
            logger.LogDebug(ex, "無法寫入事件記錄檔。");
        }
    }
}
