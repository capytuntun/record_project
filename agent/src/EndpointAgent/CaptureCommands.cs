using System.Diagnostics;
using System.Drawing;
using System.Drawing.Imaging;
using System.Runtime.Versioning;

namespace EndpointAgent;

/// <summary>
/// The capture-side command surface.
///
/// These run in the interactive user's session, where the desktop is actually
/// visible. The service launches <c>capture-stream</c> there via
/// <see cref="SessionLauncher"/>; <c>capture-frame</c> and <c>list-monitors</c>
/// exist so the capture path can be exercised by hand without the whole
/// service + server chain.
/// </summary>
[SupportedOSPlatform("windows")]
public static class CaptureCommands
{
    public static int ListMonitors()
    {
        foreach (var m in ScreenCapture.EnumerateMonitors())
        {
            Console.WriteLine(
                $"[{m.Index}] {m.Width}x{m.Height} @ ({m.X},{m.Y})" + (m.Primary ? "  (primary)" : ""));
        }
        return 0;
    }

    public static int CaptureFrame(string[] args)
    {
        var monitorIndex = IntOption(args, "--monitor", 0);
        var quality = IntOption(args, "--quality", 60);
        var outPath = StringOption(args, "--out", "screenshot.jpg") ?? "screenshot.jpg";

        var monitors = ScreenCapture.EnumerateMonitors();
        var monitor = monitors.FirstOrDefault(m => m.Index == monitorIndex) ?? monitors[0];

        var jpeg = ScreenCapture.CaptureJpeg(monitor, quality);
        File.WriteAllBytes(outPath, jpeg);
        Console.WriteLine($"Captured monitor {monitor.Index} ({monitor.Width}x{monitor.Height}) " +
                          $"-> {outPath} ({jpeg.Length} bytes)");
        return 0;
    }

    /// <summary>
    /// Continuously capture and write length-prefixed JPEG frames to stdout.
    ///
    /// Framing: a 4-byte big-endian length, then that many JPEG bytes, repeated.
    /// The parent (the service) reads this stream and relays each frame over the
    /// WebSocket. Writing to stdout is what carries frames across the session
    /// boundary from the user session back to session 0.
    /// </summary>
    public static int CaptureStream(string[] args)
    {
        var monitorIndex = IntOption(args, "--monitor", 0);
        var quality = IntOption(args, "--quality", 55);
        var fps = Math.Clamp(IntOption(args, "--fps", 5), 1, 15);
        var frameInterval = TimeSpan.FromMilliseconds(1000.0 / fps);

        using var stdout = Console.OpenStandardOutput();
        var monitors = ScreenCapture.EnumerateMonitors();
        var monitor = monitors.FirstOrDefault(m => m.Index == monitorIndex) ?? monitors[0];

        while (true)
        {
            var started = DateTime.UtcNow;
            byte[] jpeg;
            try
            {
                jpeg = ScreenCapture.CaptureJpeg(monitor, quality);
            }
            catch (Exception ex)
            {
                Console.Error.WriteLine($"capture error: {ex.Message}");
                return 1;
            }

            var header = new byte[4];
            header[0] = (byte)(jpeg.Length >> 24);
            header[1] = (byte)(jpeg.Length >> 16);
            header[2] = (byte)(jpeg.Length >> 8);
            header[3] = (byte)jpeg.Length;

            try
            {
                stdout.Write(header);
                stdout.Write(jpeg);
                stdout.Flush();
            }
            catch (IOException)
            {
                // Parent closed the pipe -- viewing stopped. Exit quietly.
                return 0;
            }

            var elapsed = DateTime.UtcNow - started;
            var remaining = frameInterval - elapsed;
            if (remaining > TimeSpan.Zero) Thread.Sleep(remaining);
        }
    }

    /// <summary>
    /// Measure the screen-capture CPU cost on this hardware without ever
    /// touching the real screen: it runs the exact capture-loop work (a BitBlt-
    /// sized copy, the sampled hash, and the JPEG encode) against a synthetic
    /// frame, then projects CPU as a share of one core at the given fps --
    /// separately for a moving screen (encode every frame) and a static one
    /// (re-send the cached JPEG). Also reports where the CPU-budget governor
    /// would settle quality. This is the numbers-on-your-box check for the
    /// recording CPU target.
    /// </summary>
    public static int Bench(string[] args)
    {
        var width = IntOption(args, "--width", 1920);
        var height = IntOption(args, "--height", 1080);
        var quality = Math.Clamp(IntOption(args, "--quality", 55), 10, 95);
        var fps = Math.Clamp(IntOption(args, "--fps", 5), 1, 15);
        var budget = IntOption(args, "--budget", 5);
        var floor = Math.Clamp(IntOption(args, "--floor", 25), 10, quality);
        var frames = Math.Max(10, IntOption(args, "--frames", 60));

        Console.WriteLine(
            $"benchmark: {width}x{height}, fps {fps}, quality {quality}, floor {floor}, budget {budget}% of one core");

        using var source = BuildSyntheticFrame(width, height);
        using var capture = new Bitmap(width, height, PixelFormat.Format24bppRgb);

        double copyMs = 0, hashMs = 0, encodeMs = 0;
        var jpegBytes = 0L;
        var sw = new Stopwatch();

        // Warm up the JIT / GDI+ encoder so the first frame does not skew the run.
        RunOnce(source, capture, quality, sw, out _, out _, out _, out _);

        for (var i = 0; i < frames; i++)
        {
            RunOnce(source, capture, quality, sw, out var c, out var h, out var e, out var bytes);
            copyMs += c; hashMs += h; encodeMs += e; jpegBytes += bytes;
        }

        copyMs /= frames; hashMs /= frames; encodeMs /= frames;
        var avgJpegKb = jpegBytes / (double)frames / 1024.0;

        var movingFrameMs = copyMs + hashMs + encodeMs;   // encode every frame
        var staticFrameMs = copyMs + hashMs;              // re-send cached JPEG

        // CPU as a share of one core: ms of work per second / 10.
        var movingPct = movingFrameMs * fps / 10.0;
        var staticPct = staticFrameMs * fps / 10.0;

        Console.WriteLine();
        Console.WriteLine($"  per-frame work (avg over {frames}):");
        Console.WriteLine($"    copy (~BitBlt) {copyMs,7:F2} ms");
        Console.WriteLine($"    hash           {hashMs,7:F2} ms");
        Console.WriteLine($"    JPEG encode    {encodeMs,7:F2} ms   (~{avgJpegKb:F0} KB/frame)");
        Console.WriteLine();
        Console.WriteLine($"  moving screen (encode every frame): {movingFrameMs,6:F2} ms/frame -> {movingPct,5:F1}% of one core at {fps} fps");
        Console.WriteLine($"  static screen (re-send cached JPEG): {staticFrameMs,6:F2} ms/frame -> {staticPct,5:F1}% of one core at {fps} fps");

        // Where the governor settles quality: highest step (ceiling..floor) whose
        // projected moving CPU fits the budget, using measured encode scaling.
        if (budget > 0)
        {
            var allowedFrameMs = budget * 10.0 / fps;      // per-frame ms allowed
            var settled = quality;
            var settledMoving = movingFrameMs;
            // Encode cost scales roughly with quality; approximate by proportion
            // to the measured point so the estimate needs only one real encode.
            for (var q = quality; q >= floor; q -= 5)
            {
                var estEncode = encodeMs * (q / (double)quality);
                var est = copyMs + hashMs + estEncode;
                settled = q; settledMoving = est;
                if (est <= allowedFrameMs) break;
            }
            var settledPct = settledMoving * fps / 10.0;
            var verdict = settledPct <= budget ? "within budget" :
                "STILL OVER budget at quality floor -- lower the recording fps";
            Console.WriteLine();
            Console.WriteLine($"  governor: settles near quality {settled} -> ~{settledPct:F1}% of one core ({verdict})");
        }

        return 0;
    }

    private static void RunOnce(
        Bitmap source, Bitmap capture, int quality, Stopwatch sw,
        out double copyMs, out double hashMs, out double encodeMs, out long bytes)
    {
        // Model the BitBlt as a raw memory copy of the frame -- the dominant part
        // of what CopyFromScreen costs once the surface is warm. (Real screen
        // BitBlt reads VRAM and can run a little higher; this is a fair lower
        // bound without touching the actual screen.)
        sw.Restart();
        BlitCopy(source, capture);
        sw.Stop(); copyMs = sw.Elapsed.TotalMilliseconds;

        // Change detection hashes the frame every tick, exactly as the agent
        // does -- this is what a re-sent (unchanged) frame costs.
        sw.Restart();
        _ = FrameCapturer.HashForBench(capture);
        sw.Stop(); hashMs = sw.Elapsed.TotalMilliseconds;

        // Encode runs only for a changed frame.
        sw.Restart();
        var jpeg = ScreenCapture.EncodeBitmap(capture, quality);
        sw.Stop(); encodeMs = sw.Elapsed.TotalMilliseconds;
        bytes = jpeg.Length;
    }

    private static unsafe void BlitCopy(Bitmap src, Bitmap dst)
    {
        var rect = new Rectangle(0, 0, src.Width, src.Height);
        var s = src.LockBits(rect, ImageLockMode.ReadOnly, PixelFormat.Format24bppRgb);
        var d = dst.LockBits(rect, ImageLockMode.WriteOnly, PixelFormat.Format24bppRgb);
        try
        {
            var bytes = (long)Math.Abs(s.Stride) * src.Height;
            Buffer.MemoryCopy((void*)s.Scan0, (void*)d.Scan0, bytes, bytes);
        }
        finally
        {
            src.UnlockBits(s);
            dst.UnlockBits(d);
        }
    }

    /// <summary>A desktop-like synthetic frame: a light background, a handful of
    /// solid panels (the large flat regions that make a real screen compress),
    /// and rows of short dark bars standing in for text -- the high-frequency
    /// content JPEG actually spends bits on. This keeps encode time and file size
    /// in a realistic range, unlike a flat gradient (too cheap) or per-pixel noise
    /// (incompressible, far too expensive).</summary>
    private static Bitmap BuildSyntheticFrame(int width, int height)
    {
        var bmp = new Bitmap(width, height, PixelFormat.Format24bppRgb);
        using var g = Graphics.FromImage(bmp);
        g.Clear(Color.FromArgb(240, 240, 245));

        // Deterministic layout: seeded RNG, no dependence on the wall clock.
        var rnd = new Random(12345);
        Color[] panels =
        {
            Color.White, Color.FromArgb(42, 120, 214), Color.FromArgb(228, 228, 233),
            Color.FromArgb(30, 30, 34), Color.FromArgb(200, 70, 70), Color.FromArgb(60, 160, 90),
        };
        for (var i = 0; i < 24; i++)
        {
            var x = rnd.Next(width);
            var y = rnd.Next(height);
            var w = rnd.Next(80, Math.Max(120, width / 3));
            var h = rnd.Next(40, Math.Max(80, height / 4));
            using var b = new SolidBrush(panels[rnd.Next(panels.Length)]);
            g.FillRectangle(b, x, y, w, h);
        }

        using var bar = new SolidBrush(Color.FromArgb(70, 70, 78));
        for (var y = 40; y < height - 40; y += 22)
        {
            var x = 30;
            while (x < width - 220)
            {
                var w = rnd.Next(10, 60);
                g.FillRectangle(bar, x, y, w, 8);
                x += w + rnd.Next(6, 18);
                if (rnd.Next(6) == 0) break;
            }
        }
        return bmp;
    }

    private static int IntOption(string[] args, string name, int fallback)
    {
        var value = StringOption(args, name, null);
        return value is not null && int.TryParse(value, out var parsed) ? parsed : fallback;
    }

    private static string? StringOption(string[] args, string name, string? fallback)
    {
        for (var i = 0; i < args.Length - 1; i++)
        {
            if (args[i] == name) return args[i + 1];
        }
        return fallback;
    }
}
