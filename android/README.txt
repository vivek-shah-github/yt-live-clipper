YT LIVE CLIPPER 2.0  (Android)
==============================

INSTALL
  1. Copy YT-Live-Clipper-Android.apk to the phone (or download it there).
  2. Tap it. If Android asks, allow "Install unknown apps" for your file manager
     or browser, then tap Install.
  3. Open the app. Allow notifications (shows progress while the screen is off).
     First start takes ~20 seconds while it sets up and updates yt-dlp.
  Needs Android 8 or newer. The APK is for 64-bit phones (almost every phone since 2017).
  Very old 32-bit phones: use YT-Live-Clipper-Android-32bit.apk from the website instead.

USE
  - Paste a link (Paste button) > Load, or in the YouTube app tap
    Share > YT Live Clipper to send the link straight in.
  - Same controls as the computer version: drag on the timeline, "Set to now",
    -1 / +1, pinch the timeline to zoom, Preview, Add clip, Export.
  - Live streams: red button to start recording, tap again to stop and save.
    Recording keeps going with the screen off or the app in the background.
  - Clips are saved to Movies/YT Clips (gallery, Files app). Open / Share from the
    Saved list in the app.

BUILD IT YOURSELF (optional, for changes)
  Android Studio > Open this folder > Build > Build APK(s).
  Or command line:  ./gradlew assembleRelease   (needs JDK 17+ and the Android SDK)
  APKs: app/build/outputs/apk/release/
  The interface is app/src/main/assets/web/index.html (same file as the computer
  version's ui/index.html). The work is done in Engine.kt.
  The signing key is kept out of this public repository. Put it at
  android/keystore/ytliveclipper.jks to sign release builds with it; updates must be
  signed with the same key to install over the already-installed app.
