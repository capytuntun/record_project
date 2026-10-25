# Bundled FFmpeg

`ffmpeg.zip` holds a single file, `ffmpeg.exe`, taken unmodified from Gyan
Doshi's static Windows build. `server\install.ps1` extracts it to
`<InstallDir>\tools\ffmpeg\ffmpeg.exe`, so installs don't have to download
FFmpeg. (The exe alone is ~100.5 MB, over GitHub's 100 MB per-file limit,
hence the zip.)

| | |
| --- | --- |
| Version | 9.0.2-essentials_build-www.gyan.dev |
| Upstream archive | https://github.com/GyanD/codexffmpeg/releases/download/9.0.2/ffmpeg-9.0.2-essentials_build.zip (mirror of https://www.gyan.dev/ffmpeg/builds/) |
| Archive SHA-256 | `60f467265b1e312373dbcd92200c2618a74850f98d3d078e94296bb3fa2047ba` |
| `ffmpeg.exe` SHA-256 | `3256173f3f8bffd7df12227c68adf68025edb1832273a9530688a7bb1ed8edec` |
| License | GPL v3 (see `LICENSE.txt`, copied from the upstream archive) |
| Source code | https://github.com/FFmpeg/FFmpeg/commit/946fcce07b (build configuration: the upstream archive's `README.txt`) |

## Updating

1. Download the newer `*-essentials_build.zip` from gyan.dev or the GyanD
   GitHub mirror and check its SHA-256 against the published one.
2. Zip **only** `bin\ffmpeg.exe` into `ffmpeg.zip` (entry name `ffmpeg.exe`)
   and replace `LICENSE.txt` with the archive's `LICENSE`.
3. Update the table above. Never commit the bare `ffmpeg.exe` — it is
   ignored on purpose, and a push containing it will be rejected.
