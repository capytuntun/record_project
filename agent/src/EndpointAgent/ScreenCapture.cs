using System.Drawing;
using System.Drawing.Imaging;
using System.Runtime.InteropServices;
using System.Runtime.Versioning;

namespace EndpointAgent;

public sealed record MonitorInfo(int Index, int X, int Y, int Width, int Height, bool Primary);

/// <summary>
/// Captures the desktop to JPEG frames (spec section 14).
///
/// Uses GDI BitBlt via Graphics.CopyFromScreen -- dependency-light and adequate
/// for a screenshot stream at a few frames per second. It captures whatever
/// desktop the *calling process* is attached to, which is the crux of the
/// service problem: a LocalSystem service lives in session 0 and would capture
/// a blank desktop, so the capture must run in the interactive user's session
/// (see <see cref="SessionLauncher"/>).
/// </summary>
[SupportedOSPlatform("windows")]
public static class ScreenCapture
{
    public static IReadOnlyList<MonitorInfo> EnumerateMonitors()
    {
        var monitors = new List<MonitorInfo>();
        var index = 0;

        bool Callback(IntPtr hMonitor, IntPtr hdc, ref Rect rect, IntPtr data)
        {
            var info = new MonitorInfoEx { cbSize = Marshal.SizeOf<MonitorInfoEx>() };
            if (GetMonitorInfo(hMonitor, ref info))
            {
                var r = info.rcMonitor;
                monitors.Add(new MonitorInfo(
                    index, r.Left, r.Top, r.Right - r.Left, r.Bottom - r.Top,
                    (info.dwFlags & MonitorPrimary) != 0));
                index++;
            }
            return true;
        }

        EnumDisplayMonitors(IntPtr.Zero, IntPtr.Zero, Callback, IntPtr.Zero);

        if (monitors.Count == 0)
        {
            // Fall back to the primary screen bounds if enumeration returned
            // nothing (rare, but never leave the caller with zero monitors).
            monitors.Add(new MonitorInfo(0, 0, 0,
                GetSystemMetrics(SM_CXSCREEN), GetSystemMetrics(SM_CYSCREEN), true));
        }
        return monitors;
    }

    /// <summary>Capture one monitor to a JPEG byte array at the given quality.</summary>
    public static byte[] CaptureJpeg(MonitorInfo monitor, int quality)
    {
        using var bitmap = new Bitmap(monitor.Width, monitor.Height, PixelFormat.Format24bppRgb);
        using (var graphics = Graphics.FromImage(bitmap))
        {
            graphics.CopyFromScreen(monitor.X, monitor.Y, 0, 0,
                new Size(monitor.Width, monitor.Height), CopyPixelOperation.SourceCopy);
        }
        return EncodeBitmap(bitmap, quality);
    }

    /// <summary>JPEG-encode an in-memory bitmap. Shared by the one-shot capture
    /// and the benchmark so both exercise the same encoder path.</summary>
    internal static byte[] EncodeBitmap(Bitmap bitmap, int quality)
    {
        using var stream = new MemoryStream();
        var encoder = GetJpegEncoder();
        using var parameters = new EncoderParameters(1);
        parameters.Param[0] = new EncoderParameter(
            Encoder.Quality, Math.Clamp((long)quality, 10, 95));
        bitmap.Save(stream, encoder, parameters);
        return stream.ToArray();
    }

    private static ImageCodecInfo GetJpegEncoder()
    {
        foreach (var codec in ImageCodecInfo.GetImageEncoders())
        {
            if (codec.FormatID == ImageFormat.Jpeg.Guid) return codec;
        }
        throw new InvalidOperationException("No JPEG encoder is available.");
    }

    // --- Win32 ------------------------------------------------------------

    private const int MonitorPrimary = 0x1;
    private const int SM_CXSCREEN = 0;
    private const int SM_CYSCREEN = 1;

    [StructLayout(LayoutKind.Sequential)]
    private struct Rect { public int Left, Top, Right, Bottom; }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct MonitorInfoEx
    {
        public int cbSize;
        public Rect rcMonitor;
        public Rect rcWork;
        public uint dwFlags;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)]
        public string szDevice;
    }

    private delegate bool MonitorEnumProc(IntPtr hMonitor, IntPtr hdc, ref Rect rect, IntPtr data);

    [DllImport("user32.dll")]
    private static extern bool EnumDisplayMonitors(
        IntPtr hdc, IntPtr clip, MonitorEnumProc callback, IntPtr data);

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern bool GetMonitorInfo(IntPtr hMonitor, ref MonitorInfoEx info);

    [DllImport("user32.dll")]
    private static extern int GetSystemMetrics(int index);
}

/// <summary>
/// A reusable capturer for the streaming loop (performance work).
///
/// Two costs the per-frame <see cref="ScreenCapture.CaptureJpeg"/> pays that a
/// long-running stream should not:
///
///  1. Allocation. That method news up a Bitmap, a Graphics and a MemoryStream
///     every frame; at 5-15 fps that is constant GC churn. This holds one of
///     each and reuses them, recreating the bitmap only when the monitor's size
///     actually changes.
///
///  2. Encoding an unchanged screen. A viewed desktop is static most of the
///     time. <see cref="Capture"/> returns a cheap content hash so the caller
///     can compare against the previous frame and skip <see cref="Encode"/>
///     -- the expensive JPEG pass -- entirely when nothing moved.
///
/// Single-threaded by contract: one instance is owned by one capture loop.
/// </summary>
[SupportedOSPlatform("windows")]
public sealed class FrameCapturer : IDisposable
{
    // Native-resolution surface the screen is BitBlt'd into.
    private Bitmap? _capture;
    private Graphics? _captureGraphics;
    private int _captureWidth;
    private int _captureHeight;

    private readonly MemoryStream _stream = new();
    private readonly ImageCodecInfo _encoder = JpegEncoder();
    private EncoderParameters? _params;
    private long _paramsQuality = -1;

    // The most recent encoded frame, kept so the caller can re-send it for an
    // unchanged screen without paying the JPEG encode again. This is the core
    // of the recording CPU fix: a recorded desktop is static most of the time,
    // yet the recorder still needs a frame every tick to keep its timeline
    // honest -- so we send the same bytes, not a fresh encode.
    private byte[]? _lastEncoded;

    /// <summary>The last JPEG produced by <see cref="Encode"/>, or null if none
    /// has been encoded yet. The array is owned by the capturer; do not mutate.</summary>
    public byte[]? LastEncoded => _lastEncoded;

    private void EnsureCaptureSurface(MonitorInfo monitor)
    {
        if (_capture is not null && _captureWidth == monitor.Width && _captureHeight == monitor.Height)
        {
            return;
        }
        _captureGraphics?.Dispose();
        _capture?.Dispose();
        _captureWidth = monitor.Width;
        _captureHeight = monitor.Height;
        _capture = new Bitmap(_captureWidth, _captureHeight, PixelFormat.Format24bppRgb);
        _captureGraphics = Graphics.FromImage(_capture);
    }

    /// <summary>
    /// Copy the monitor into the reused surface and return a fast content hash.
    /// The hash is a sampled FNV-1a over the raw pixels: whole-frame coverage at
    /// a fraction of the cost of the JPEG encode it lets the caller skip when the
    /// screen has not changed.
    /// </summary>
    public ulong Capture(MonitorInfo monitor)
    {
        EnsureCaptureSurface(monitor);
        _captureGraphics!.CopyFromScreen(
            monitor.X, monitor.Y, 0, 0,
            new Size(_captureWidth, _captureHeight), CopyPixelOperation.SourceCopy);
        return HashBits(_capture!);
    }

    /// <summary>JPEG-encode the frame captured by the last <see cref="Capture"/>
    /// and cache it as <see cref="LastEncoded"/>.</summary>
    public byte[] Encode(int quality)
    {
        var clamped = Math.Clamp((long)quality, 10, 95);
        if (_params is null || _paramsQuality != clamped)
        {
            _params?.Dispose();
            _params = new EncoderParameters(1)
            {
                Param = { [0] = new EncoderParameter(Encoder.Quality, clamped) },
            };
            _paramsQuality = clamped;
        }

        _stream.SetLength(0);
        _capture!.Save(_stream, _encoder, _params);
        _lastEncoded = _stream.ToArray();
        return _lastEncoded;
    }

    /// <summary>Test/benchmark hook: the same sampled hash the capture loop uses,
    /// over an arbitrary bitmap.</summary>
    internal static ulong HashForBench(Bitmap bitmap) => HashBits(bitmap);

    private static unsafe ulong HashBits(Bitmap bitmap)
    {
        var rect = new Rectangle(0, 0, bitmap.Width, bitmap.Height);
        var data = bitmap.LockBits(rect, ImageLockMode.ReadOnly, PixelFormat.Format24bppRgb);
        try
        {
            const ulong offset = 14695981039346656037UL;
            const ulong prime = 1099511628211UL;
            // Prime stride so sampling does not lock onto a pixel boundary and
            // miss a whole colour channel. Reads ~1% of the buffer, spread over
            // the entire frame, so any visible change registers.
            const int step = 97;

            var hash = offset;
            var scan0 = (byte*)data.Scan0;
            var stride = data.Stride;
            var rowBytes = bitmap.Width * 3;   // exclude row padding
            for (var y = 0; y < data.Height; y++)
            {
                var row = scan0 + (long)y * stride;
                for (var x = 0; x < rowBytes; x += step)
                {
                    hash ^= row[x];
                    hash *= prime;
                }
            }
            // Fold in dimensions so a resolution change is always a change.
            hash ^= (ulong)(((long)data.Height << 20) ^ rowBytes);
            return hash;
        }
        finally
        {
            bitmap.UnlockBits(data);
        }
    }

    private static ImageCodecInfo JpegEncoder()
    {
        foreach (var codec in ImageCodecInfo.GetImageEncoders())
        {
            if (codec.FormatID == ImageFormat.Jpeg.Guid) return codec;
        }
        throw new InvalidOperationException("No JPEG encoder is available.");
    }

    public void Dispose()
    {
        _captureGraphics?.Dispose();
        _capture?.Dispose();
        _params?.Dispose();
        _stream.Dispose();
    }
}
