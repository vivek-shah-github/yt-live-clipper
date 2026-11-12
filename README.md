# YT Live Clipper

Clip any moment from a YouTube live stream or video and save it as MP4, for Windows, Mac and Android.

- **Cut a stream that has ended:** mark as many clips as you like on a timeline, then export them together.
- **Record while it's live:** start and stop recording with one button. If the stream ends, the recording still saves.
- **Vertical 9:16:** centre-cropped 1080×1920 for Shorts and Reels.

**Website and downloads:** https://vivek-shah-github.github.io/yt-live-clipper/

## What's in this repository

| Folder | What it is |
|---|---|
| `desktop/` | Windows and Mac app (Python). Start with `run.bat` or `run.command`. |
| `android/` | Android app (Kotlin + WebView). Build with `./gradlew assembleRelease`. |
| `web/` | Web version: turns your own computer into a password-protected clipper website (Cloudflare Tunnel link), usable from any browser with nothing to install. Start with `web/start-web.command` (Mac) or `web/start-web.bat` (Windows). Needs the `desktop/` folder next to it. |
| `docs/` | The website (GitHub Pages) and the downloadable files. |

The interface is one HTML file shared by all three apps: `desktop/ui/index.html` and `android/app/src/main/assets/web/index.html` are the same file.

Built on [yt-dlp](https://github.com/yt-dlp/yt-dlp) and [ffmpeg](https://ffmpeg.org). On Android it uses [youtubedl-android](https://github.com/JunkFood02/youtubedl-android).

Use it on your own streams or content you have permission to reuse.
