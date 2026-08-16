using System.Diagnostics;
using System.Net.WebSockets;
using System.Runtime.Versioning;
using System.Text;
using System.Text.Json;

namespace EndpointAgent;

/// <summary>
/// Maintains the screen-viewing WebSocket to the server and streams frames
/// while a viewer is watching (spec sections 14, 29).
///
/// Capture happens only between a "start" and a "stop" from the server, which
/// the server sends based on whether any authorised viewer is connected. So the
/// screen is captured only while someone is actually looking -- never idly.
///
/// The credential is re-read from <see cref="CredentialStore"/> on each connect,
/// so credential rotation performed by the heartbeat loop is picked up here for
/// free.
/// </summary>
[SupportedOSPlatform("windows")]
public sealed class ScreenStreamer(ILogger<ScreenStreamer> logger, string serverUrl)
{
    private readonly string _wsUrl = ToWs(serverUrl) + "/api/agent/screen/ws";

    private CancellationTokenSource? _captureCts;
    private int _monitor;

    public async Task RunAsync(CancellationToken stoppingToken)
    {
        while (!stoppingToken.IsCancellationRequested)
        {
            var credential = CredentialStore.Load();
            if (credential is null)
            {
                // Not enrolled yet; the worker's enroll path will populate this.
                await SafeDelay(TimeSpan.FromSeconds(10), stoppingToken);
                continue;
            }

            try
            {
                await ConnectAndServeAsync(credential, stoppingToken);
            }
            catch (OperationCanceledException) when (stoppingToken.IsCancellationRequested)
            {
                break;
            }
            catch (Exception ex)
            {
                logger.LogDebug(ex, "螢幕串流連線中斷，稍後重連。");
            }

            StopCapture();
            await SafeDelay(TimeSpan.FromSeconds(15), stoppingToken);
        }
    }

    private async Task ConnectAndServeAsync(string credential, CancellationToken ct)
    {
        using var ws = new ClientWebSocket();
        ws.Options.SetRequestHeader("Authorization", "Bearer " + credential);

        await ws.ConnectAsync(new Uri(_wsUrl), ct);
        logger.LogInformation("螢幕串流已連線。");

        await SendMonitorsAsync(ws, ct);

        var buffer = new byte[8192];
        while (ws.State == WebSocketState.Open && !ct.IsCancellationRequested)
        {
            var message = await ReceiveTextAsync(ws, buffer, ct);
            if (message is null) break;
            HandleControl(ws, message, ct);
        }
    }

    private void HandleControl(ClientWebSocket ws, string message, CancellationToken ct)
    {
        JsonDocument doc;
        try { doc = JsonDocument.Parse(message); }
        catch (JsonException) { return; }

        using (doc)
        {
            if (!doc.RootElement.TryGetProperty("type", out var typeProp)) return;
            var type = typeProp.GetString();

            switch (type)
            {
                case "start":
                    var fps = GetInt(doc.RootElement, "targetFps", 5);
                    var quality = GetInt(doc.RootElement, "jpegQuality", 55);
                    // Additive flag: absent (older server) -> false -> send every
                    // frame, exactly as before. Only ever true for live-view-only
                    // capture; while recording, the server keeps it false so the
                    // recorder gets a steady frame rate.
                    var allowSkip = GetBool(doc.RootElement, "allowSkipUnchanged", false);
                    // Recording CPU controls. These default ON even when an older
                    // server omits them, so the endpoint self-caps regardless:
                    //  * cpuBudgetPercent -- ceiling on capture CPU as a share of
                    //    one core (0 disables the governor).
                    //  * jpegQualityFloor -- how far quality may drop to hold that
                    //    budget before we log that we cannot meet it.
                    var cpuBudget = GetInt(doc.RootElement, "cpuBudgetPercent", 5);
                    var qualityFloor = GetInt(doc.RootElement, "jpegQualityFloor", 25);
                    StartCapture(ws, fps, quality, allowSkip, cpuBudget, qualityFloor, ct);
                    break;
                case "stop":
                    StopCapture();
                    break;
                case "set_monitor":
                    _monitor = GetInt(doc.RootElement, "index", 0);
                    logger.LogInformation("切換擷取螢幕 -> {Index}", _monitor);
                    // The running capture loop reads _monitor each frame, so the
                    // switch takes effect on the next frame without a restart.
                    break;
            }
        }
    }

    private void StartCapture(
        ClientWebSocket ws, int fps, int quality, bool allowSkip,
        int cpuBudgetPercent, int qualityFloor, CancellationToken ct)
    {
        StopCapture();
        var cts = CancellationTokenSource.CreateLinkedTokenSource(ct);
        _captureCts = cts;
        _ = Task.Run(() => CaptureLoopAsync(
            ws, fps, quality, allowSkip, cpuBudgetPercent, qualityFloor, cts.Token), cts.Token);
    }

    private void StopCapture()
    {
        _captureCts?.Cancel();
        _captureCts?.Dispose();
        _captureCts = null;
    }

    private async Task CaptureLoopAsync(
        ClientWebSocket ws, int fps, int quality, bool allowSkip,
        int cpuBudgetPercent, int qualityFloor, CancellationToken ct)
    {
        var monitors = ScreenCapture.EnumerateMonitors();

        // The streamer only runs inside the tray helper, which lives in the
        // user's session and can see the desktop -- so capture is always
        // in-process. (The service no longer runs the streamer, which is what
        // made the old session-0 bridge necessary.)
        try
        {
            await CaptureInProcessAsync(
                ws, monitors, fps, quality, allowSkip,
                cpuBudgetPercent, qualityFloor, ct);
        }
        catch (OperationCanceledException) { /* stop requested */ }
        catch (Exception ex)
        {
            logger.LogDebug(ex, "擷取迴圈結束。");
        }
    }

    // Even when nothing on screen changes, send one frame at least this often:
    // it refreshes the hub's "most recent frame" for viewers that join mid-
    // stream, and bounds how long any missed change could stay unshown.
    private static readonly TimeSpan KeepaliveInterval = TimeSpan.FromSeconds(2);

    // How many unchanged frames before easing off the capture cadence.
    private const int IdleAfterUnchangedFrames = 3;

    private async Task CaptureInProcessAsync(
        ClientWebSocket ws, IReadOnlyList<MonitorInfo> monitors, int fps, int requestedQuality,
        bool allowSkip, int cpuBudgetPercent, int qualityFloor,
        CancellationToken ct)
    {
        var clampedFps = Math.Clamp(fps, 1, 15);
        var fullInterval = TimeSpan.FromMilliseconds(1000.0 / clampedFps);
        // While the screen sits still we only need to capture often enough to
        // notice when it starts moving again; 2 fps is plenty and costs little.
        var idleInterval = TimeSpan.FromMilliseconds(500);
        if (idleInterval < fullInterval) idleInterval = fullInterval;

        // CPU-budget governor. 100% of one core == 1000 ms of CPU per wall-clock
        // second; the budget is that share, and against a moving screen we may
        // encode up to clampedFps frames a second, so each frame's capture+encode
        // work is allowed budgetMsPerSecond / clampedFps. <=0 disables the cap.
        var governed = cpuBudgetPercent > 0;
        var budgetMsPerSecond = 10.0 * cpuBudgetPercent;
        var allowedPerFrameMs = budgetMsPerSecond / clampedFps;
        var ceilingQuality = Math.Clamp(requestedQuality, 10, 95);
        var floorQuality = Math.Clamp(qualityFloor, 10, ceilingQuality);
        var quality = ceilingQuality;

        using var capturer = new FrameCapturer();
        var haveLast = false;
        ulong lastHash = 0;
        var lastSent = DateTime.MinValue;
        var unchangedStreak = 0;

        // Exponential moving average of per-frame CPU work (capture + hash + any
        // downscale + encode; a re-send costs only capture + hash). The send
        // itself is excluded: it is network I/O, not CPU, and would distort the
        // budget. Seeded negative to mean "no measurement yet".
        double cpuEmaMs = -1;
        const double emaAlpha = 0.3;
        var overBudgetLoggedAt = DateTime.MinValue;
        var work = new Stopwatch();

        while (!ct.IsCancellationRequested && ws.State == WebSocketState.Open)
        {
            var started = DateTime.UtcNow;
            var monitor = monitors.FirstOrDefault(m => m.Index == _monitor) ?? monitors[0];

            work.Restart();
            var hash = capturer.Capture(monitor);
            var changed = !haveLast || hash != lastHash;
            var keepaliveDue = DateTime.UtcNow - lastSent >= KeepaliveInterval;

            byte[]? frame = null;
            if (changed)
            {
                // A genuinely new frame: pick the quality the budget allows, then
                // encode. The governor reads last frame's EMA -- the cost it is
                // actually reacting to -- and steps quality toward the floor under
                // sustained motion / back up toward the ceiling when there is slack.
                if (governed)
                {
                    quality = AdjustQuality(quality, cpuEmaMs, allowedPerFrameMs, floorQuality, ceilingQuality);
                }
                frame = capturer.Encode(quality);
            }
            else if (!allowSkip || keepaliveDue)
            {
                // Unchanged, but recording (allowSkip false) or a live-view
                // keepalive still needs a frame this tick. Re-send the cached
                // JPEG rather than re-encode: steady cadence for the recorder's
                // timeline, none of the encode cost. This is the main saving.
                frame = capturer.LastEncoded ?? capturer.Encode(quality);
            }
            work.Stop();

            var workMs = work.Elapsed.TotalMilliseconds;
            cpuEmaMs = cpuEmaMs < 0 ? workMs : cpuEmaMs * (1 - emaAlpha) + workMs * emaAlpha;

            if (frame is not null)
            {
                await ws.SendAsync(frame, WebSocketMessageType.Binary, endOfMessage: true, ct);
                lastSent = DateTime.UtcNow;
            }

            // No silent cap: if even the quality floor cannot hold the budget --
            // a very large screen or a high fps against continuous motion -- say
            // so where IT will see it, rate-limited so it cannot flood the log.
            if (governed && changed && quality <= floorQuality
                && cpuEmaMs > allowedPerFrameMs * 1.5
                && DateTime.UtcNow - overBudgetLoggedAt > TimeSpan.FromMinutes(5))
            {
                overBudgetLoggedAt = DateTime.UtcNow;
                logger.LogWarning(
                    "螢幕擷取已達品質下限仍超出 CPU 預算（每幀約 {Work:F0} ms，預算 {Budget:F0} ms/幀）。" +
                    "此螢幕解析度偏高，建議調降錄影 fps。", cpuEmaMs, allowedPerFrameMs);
            }

            haveLast = true;
            lastHash = hash;
            unchangedStreak = changed ? 0 : unchangedStreak + 1;

            // Adaptive cadence, live-view only: after a few still frames, capture
            // less often to shed even the BitBlt+hash cost; snap back to full
            // rate the instant something moves. Recording always stays full rate
            // so the recorder receives a steady stream.
            var interval = allowSkip && unchangedStreak >= IdleAfterUnchangedFrames
                ? idleInterval
                : fullInterval;

            var remaining = interval - (DateTime.UtcNow - started);
            if (remaining > TimeSpan.Zero) await Task.Delay(remaining, ct);
        }
    }

    /// <summary>
    /// Nudge JPEG quality one step to keep per-frame CPU near the budget: down
    /// under sustained load, back up (toward the requested quality) when there is
    /// headroom. Resolution and frame rate are never touched here -- the recorder
    /// depends on both being constant -- so quality is the only lever.
    /// </summary>
    private static int AdjustQuality(int quality, double cpuEmaMs, double allowedMs, int floor, int ceiling)
    {
        const int step = 5;
        if (cpuEmaMs < 0) return quality;                 // nothing measured yet
        if (cpuEmaMs > allowedMs && quality > floor)
            return Math.Max(floor, quality - step);
        // Hysteresis: only climb back when comfortably under budget, so quality
        // does not oscillate frame to frame around the threshold.
        if (cpuEmaMs < allowedMs * 0.7 && quality < ceiling)
            return Math.Min(ceiling, quality + step);
        return quality;
    }

    private async Task SendMonitorsAsync(ClientWebSocket ws, CancellationToken ct)
    {
        var monitors = ScreenCapture.EnumerateMonitors()
            .Select(m => new { index = m.Index, width = m.Width, height = m.Height, primary = m.Primary });
        var payload = JsonSerializer.Serialize(new { type = "monitors", monitors });
        await ws.SendAsync(Encoding.UTF8.GetBytes(payload), WebSocketMessageType.Text, true, ct);
    }

    private static async Task<string?> ReceiveTextAsync(
        ClientWebSocket ws, byte[] buffer, CancellationToken ct)
    {
        using var stream = new MemoryStream();
        WebSocketReceiveResult result;
        do
        {
            result = await ws.ReceiveAsync(buffer, ct);
            if (result.MessageType == WebSocketMessageType.Close) return null;
            stream.Write(buffer, 0, result.Count);
        }
        while (!result.EndOfMessage);
        return Encoding.UTF8.GetString(stream.ToArray());
    }

    private static int GetInt(JsonElement element, string name, int fallback) =>
        element.TryGetProperty(name, out var value) && value.TryGetInt32(out var n) ? n : fallback;

    private static bool GetBool(JsonElement element, string name, bool fallback) =>
        element.TryGetProperty(name, out var value) && value.ValueKind is JsonValueKind.True or JsonValueKind.False
            ? value.GetBoolean()
            : fallback;

    private static string ToWs(string httpUrl) =>
        httpUrl.TrimEnd('/').Replace("https://", "wss://").Replace("http://", "ws://");

    private static async Task SafeDelay(TimeSpan delay, CancellationToken ct)
    {
        try { await Task.Delay(delay, ct); } catch (OperationCanceledException) { }
    }
}
