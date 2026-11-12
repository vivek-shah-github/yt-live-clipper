package com.madebytwins.ytliveclipper

import android.content.ClipboardManager
import android.content.ContentValues
import android.content.Context
import android.content.Intent
import android.media.MediaScannerConnection
import android.net.Uri
import android.os.Build
import android.os.Environment
import android.os.Handler
import android.os.Looper
import android.provider.MediaStore
import androidx.core.content.FileProvider
import com.yausername.ffmpeg.FFmpeg
import com.yausername.youtubedl_android.YoutubeDL
import com.yausername.youtubedl_android.YoutubeDLRequest
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import kotlin.math.max
import kotlin.math.min
import kotlin.math.roundToInt

/**
 * Everything the app does. Mirrors the desktop clipper.py engine: the web interface calls
 * [call] with the same action names and gets the same JSON back.
 */
object Engine {
    private const val VERSION = "2.0"
    private val QUALITIES = listOf("Best", "2160p", "1440p", "1080p", "720p", "480p", "360p")
    private const val OUT_LABEL = "Movies/YT Clips"
    private const val VERTICAL_VF = "crop=trunc(ih*9/16/2)*2:ih,scale=1080:1920:flags=lanczos,setsar=1"
    private val ENCODE_ARGS = listOf(
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"
    )
    private val HTTP_IN = listOf("-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5")

    private lateinit var app: Context
    private val lock = Any()
    private val main = Handler(Looper.getMainLooper())
    private val ready = CountDownLatch(1)
    @Volatile private var initError: String? = null
    @Volatile private var prepTask: String? = "Getting ready…"

    private val logs = ArrayList<JSONObject>()
    private var logId = 0
    private var info: JSONObject? = null
    private var infoSeq = 0
    private var loadFail = 0
    @Volatile var busy = false; private set
    @Volatile var task: String? = null; private set
    @Volatile var progress: Double? = null; private set
    private var export: JSONObject? = null
    @Volatile var rec: LiveRecorder? = null; private set
    @Volatile private var recTitle: String? = null
    @Volatile var recState: String? = null; private set
    private val outputs = JSONArray()
    @Volatile private var ffProc: Process? = null
    @Volatile private var cancelFlag = false

    private val prefs by lazy { app.getSharedPreferences("settings", Context.MODE_PRIVATE) }

    // ------------------------------------------------------------------ setup
    fun init(context: Context) {
        if (::app.isInitialized) return
        app = context.applicationContext
        Thread {
            try {
                YoutubeDL.init(app)
                FFmpeg.init(app)
            } catch (e: Throwable) {
                initError = "Couldn't start yt-dlp/ffmpeg: ${e.message}"
                log("ERROR: $initError", "error", true)
            }
            ready.countDown()
            // keep yt-dlp fresh, YouTube changes often
            val last = prefs.getLong("ytdlp_checked", 0)
            if (initError == null && System.currentTimeMillis() - last > 2 * 24 * 3600 * 1000L) {
                prepTask = "Updating yt-dlp…"
                updating = true
                try {
                    val st = YoutubeDL.updateYoutubeDL(app, YoutubeDL.UpdateChannel.STABLE)
                    prefs.edit().putLong("ytdlp_checked", System.currentTimeMillis()).apply()
                    if (st == YoutubeDL.UpdateStatus.DONE) log("yt-dlp updated to ${ytdlpVersion()}", "ok")
                } catch (e: Throwable) {
                    log("Couldn't update yt-dlp right now (${e.message?.take(120)}). Using the built-in version.")
                }
            }
            updating = false
            prepTask = null
        }.start()
        Thread { watchRecorder() }.apply { isDaemon = true }.start()
    }

    @Volatile private var updating = false

    private fun awaitReady() {
        ready.await()
        initError?.let { throw RuntimeException(it) }
        // don't run yt-dlp while it is being replaced by the start-up update (max 90 s)
        val until = System.currentTimeMillis() + 90_000
        while (updating && System.currentTimeMillis() < until) Thread.sleep(250)
    }

    private fun ytdlpVersion(): String? = try { YoutubeDL.versionName(app) ?: YoutubeDL.version(app) } catch (e: Throwable) { null }

    private val ffmpegBin: File get() = File(app.applicationInfo.nativeLibraryDir, "libffmpeg.so")

    private fun procEnv(pb: ProcessBuilder) {
        val base = File(app.noBackupFilesDir, "youtubedl-android/packages").absolutePath
        pb.environment().apply {
            this["LD_LIBRARY_PATH"] = "$base/python/usr/lib:$base/ffmpeg/usr/lib:$base/aria2c/usr/lib"
            this["SSL_CERT_FILE"] = "$base/python/usr/etc/tls/cert.pem"
            this["TMPDIR"] = app.cacheDir.absolutePath
            this["HOME"] = app.cacheDir.absolutePath
        }
    }

    // ------------------------------------------------------------------ settings
    private fun settings() = JSONObject().apply {
        put("quality", prefs.getString("quality", "1080p").let { if (it in QUALITIES) it else "1080p" })
        put("vertical", prefs.getBoolean("vertical", false))
        put("cookies", "none")
        put("out_dir", OUT_LABEL)
    }

    private fun maxHeight(q: String?): Int? {
        val v = q ?: prefs.getString("quality", "1080p")
        return if (v == "Best" || v == null) null else v.trimEnd('p').toIntOrNull()
    }

    // ------------------------------------------------------------------ logging / jobs
    fun log(msg: String, level: String = "info", toast: Boolean = false) {
        synchronized(lock) {
            logId++
            logs.add(JSONObject().apply {
                put("id", logId); put("t", SimpleDateFormat("HH:mm:ss", Locale.US).format(Date()))
                put("msg", msg); put("level", level); put("toast", toast)
            })
            while (logs.size > 500) logs.removeAt(0)
        }
    }

    private class Canceled : Exception()

    private fun runBg(name: String, fn: () -> Unit): JSONObject {
        synchronized(lock) {
            if (busy) return err("Please wait, the app is still busy with: ${task ?: "a task"}")
            busy = true; task = name; progress = null; cancelFlag = false
        }
        ClipperService.ensureRunning(app)
        Thread {
            try {
                awaitReady()
                fn()
            } catch (e: Canceled) {
                log("Canceled.", "info", true)
            } catch (e: Throwable) {
                log("ERROR: " + cleanError(e), "error", true)
            } finally {
                synchronized(lock) { busy = false; task = null; progress = null }
            }
        }.start()
        return ok()
    }

    private fun cleanError(e: Throwable): String {
        val m = (e.message ?: e.javaClass.simpleName).trim()
        val lines = m.lines().map { it.trim() }.filter { it.isNotEmpty() }
        val errLine = lines.lastOrNull { it.startsWith("ERROR:") } ?: lines.lastOrNull() ?: m
        return errLine.removePrefix("ERROR:").trim().take(600)
    }

    private fun ok() = JSONObject().put("ok", true)
    private fun err(msg: String) = JSONObject().put("ok", false).put("error", msg)

    // ------------------------------------------------------------------ yt-dlp
    private fun fetchInfo(url: String): JSONObject {
        val req = YoutubeDLRequest(url)
        req.addOption("-J")
        req.addOption("--no-playlist")
        req.addOption("--no-warnings")
        val resp = YoutubeDL.execute(req, null, null)
        val out = resp.out.trim()
        val start = out.indexOf('{')
        if (start < 0) throw RuntimeException("yt-dlp returned nothing for this link.")
        return JSONObject(out.substring(start))
    }

    private fun JSONObject.str(k: String): String? = if (has(k) && !isNull(k)) optString(k) else null
    private fun JSONObject.num(k: String): Double = if (has(k) && !isNull(k)) optDouble(k, 0.0).let { if (it.isNaN()) 0.0 else it } else 0.0

    private fun formats(info: JSONObject): List<JSONObject> {
        val arr = info.optJSONArray("formats") ?: return emptyList()
        return (0 until arr.length()).map { arr.getJSONObject(it) }.filter { it.str("url") != null }
    }

    private fun codecPref(f: JSONObject): Int {
        val vc = (f.str("vcodec") ?: "").lowercase()
        return when {
            vc.startsWith("avc1") || vc.startsWith("h264") -> 3
            vc.startsWith("vp09") || vc.startsWith("vp9") -> 2
            vc.startsWith("av01") -> 1
            else -> 0
        }
    }

    /** same ranking as desktop: fits under max height first, then tallest. */
    private fun heightKey(f: JSONObject, maxH: Int?): Double {
        val h = f.num("height")
        return if (maxH == null || h <= maxH) 100000 + h else -h
    }

    private fun hasV(f: JSONObject) = f.str("vcodec").let { it != null && it != "none" }
    private fun hasA(f: JSONObject) = f.str("acodec").let { it != null && it != "none" }

    private fun pickVodFormats(info: JSONObject, maxH: Int?): Pair<JSONObject, JSONObject?> {
        val fmts = formats(info)
        val direct = fmts.filter { (it.str("protocol") ?: "") in listOf("https", "http") }
        val pool = direct.ifEmpty { fmts }
        val vids = pool.filter { hasV(it) && it.str("acodec") == "none" }
        val auds = pool.filter { hasA(it) && it.str("vcodec") == "none" }
        val muxed = pool.filter { hasA(it) && hasV(it) }
        if (vids.isNotEmpty() && auds.isNotEmpty()) {
            val v = vids.sortedWith(compareBy<JSONObject>({ heightKey(it, maxH) }, { codecPref(it) }, { it.num("tbr") })).last()
            val a = auds.sortedWith(compareBy<JSONObject>({ if (it.str("ext") == "m4a") 1 else 0 }, { max(it.num("abr"), it.num("tbr")) })).last()
            return v to a
        }
        if (muxed.isNotEmpty()) return muxed.sortedWith(compareBy<JSONObject>({ heightKey(it, maxH) }, { it.num("tbr") })).last() to null
        val unknown = pool.filter { it.str("vcodec") != "none" }
        if (unknown.isNotEmpty()) return unknown.sortedWith(compareBy<JSONObject>({ heightKey(it, maxH) }, { it.num("tbr") })).last() to null
        throw RuntimeException("No downloadable formats found for this video.")
    }

    private fun pickLiveFormat(info: JSONObject, maxH: Int?): JSONObject {
        val hls = formats(info).filter { (it.str("protocol") ?: "").contains("m3u8") && it.str("vcodec") != "none" }
        if (hls.isEmpty()) throw RuntimeException("No live stream found (is the stream really live?).")
        return hls.sortedWith(compareBy<JSONObject>({ heightKey(it, maxH) }, { it.num("tbr") })).last()
    }

    private fun headerArgs(f: JSONObject, info: JSONObject): List<String> {
        val h = f.optJSONObject("http_headers") ?: info.optJSONObject("http_headers") ?: return emptyList()
        val sb = StringBuilder()
        h.keys().forEach { k -> sb.append(k).append(": ").append(h.optString(k)).append("\r\n") }
        return if (sb.isEmpty()) emptyList() else listOf("-headers", sb.toString())
    }

    private fun uiInfo(i: JSONObject) = JSONObject().apply {
        for (k in listOf("id", "title", "duration", "is_live", "live_status", "uploader", "thumbnail", "webpage_url", "extractor_key"))
            put(k, if (i.has(k)) i.get(k) else JSONObject.NULL)
        put("extractor", (i.str("extractor_key") ?: "").lowercase())
    }

    // ------------------------------------------------------------------ ffmpeg
    private fun runFfmpeg(args: List<String>, duration: Double?, onProgress: ((Double) -> Unit)?) {
        val cmd = listOf(ffmpegBin.absolutePath, "-hide_banner", "-y", "-loglevel", "error", "-nostats", "-progress", "pipe:1") + args
        val pb = ProcessBuilder(cmd)
        procEnv(pb)
        val p = pb.start()
        ffProc = p
        val errLines = ArrayList<String>()
        val et = Thread { p.errorStream.bufferedReader().forEachLine { synchronized(errLines) { errLines.add(it); if (errLines.size > 40) errLines.removeAt(0) } } }
        et.start()
        p.outputStream.close()
        p.inputStream.bufferedReader().forEachLine { line ->
            if (line.startsWith("out_time_us=") || line.startsWith("out_time_ms=")) {
                val us = line.substringAfter("=").toLongOrNull()
                if (us != null && duration != null && duration > 0 && onProgress != null)
                    onProgress(min(100.0, max(0.0, us / 1_000_000.0 / duration * 100)))
            }
        }
        val code = p.waitFor()
        et.join(2000)
        ffProc = null
        if (code != 0) throw RuntimeException("ffmpeg failed:\n" + synchronized(errLines) { errLines.takeLast(15).joinToString("\n") })
    }

    private fun buildClipArgs(v: JSONObject, a: JSONObject?, info: JSONObject, start: Double, end: Double, vertical: Boolean, out: File): List<String> {
        val dur = end - start
        val args = ArrayList<String>()
        for (f in listOfNotNull(v, a)) {
            args += HTTP_IN + headerArgs(f, info) + listOf("-ss", "%.3f".format(Locale.US, start), "-t", "%.3f".format(Locale.US, dur), "-i", f.getString("url"))
        }
        args += if (a != null) listOf("-map", "0:v:0", "-map", "1:a:0") else listOf("-map", "0:v:0", "-map", "0:a:0?")
        if (vertical) args += listOf("-vf", VERTICAL_VF)
        args += ENCODE_ARGS + out.absolutePath
        return args
    }

    class LiveRecorder(val ts: File, cmd: List<String>, env: (ProcessBuilder) -> Unit) {
        val started = System.currentTimeMillis()
        val proc: Process

        init {
            val pb = ProcessBuilder(cmd).redirectErrorStream(true)
            env(pb)
            proc = pb.start()
            Thread { try { val b = ByteArray(8192); val ins = proc.inputStream; while (ins.read(b) >= 0) { } } catch (_: Throwable) {} }.start()
        }

        fun elapsed() = (System.currentTimeMillis() - started) / 1000.0
        fun size() = if (ts.exists()) ts.length() else 0L
        fun alive() = proc.isAlive

        fun stop(): Double {
            if (alive()) {
                try { proc.outputStream.write('q'.code); proc.outputStream.flush() } catch (_: Throwable) {}
                if (!proc.waitFor(20, TimeUnit.SECONDS)) {
                    proc.destroy()
                    proc.waitFor(10, TimeUnit.SECONDS)
                }
            }
            return elapsed()
        }
    }

    // ------------------------------------------------------------------ files
    private fun workDir(): File = File(app.getExternalFilesDir(null) ?: app.filesDir, "work").apply { mkdirs() }

    private fun safeName(name: String, maxLen: Int = 80): String {
        var n = name.replace(Regex("[\\\\/:*?\"<>|\\r\\n\\t]+"), " ").trim().trim('.')
        n = n.replace(Regex("\\s+"), " ")
        return (n.ifEmpty { "clip" }).take(maxLen).trim()
    }

    private fun fmtTime(sec: Double): String {
        val s = max(0, sec.roundToInt())
        return "%02d:%02d:%02d".format(s / 3600, s % 3600 / 60, s % 60)
    }

    /** Move a finished file into Movies/YT Clips so it shows up in the gallery. */
    private fun publish(tmp: File, displayName: String): JSONObject {
        val size = tmp.length()
        val path: String
        if (Build.VERSION.SDK_INT >= 29) {
            val r = app.contentResolver
            val values = ContentValues().apply {
                put(MediaStore.Video.Media.DISPLAY_NAME, displayName)
                put(MediaStore.Video.Media.MIME_TYPE, "video/mp4")
                put(MediaStore.Video.Media.RELATIVE_PATH, "${Environment.DIRECTORY_MOVIES}/YT Clips")
                put(MediaStore.Video.Media.IS_PENDING, 1)
            }
            val uri = r.insert(MediaStore.Video.Media.EXTERNAL_CONTENT_URI, values)
                ?: throw RuntimeException("Couldn't save to Movies/YT Clips.")
            r.openOutputStream(uri)!!.use { o -> tmp.inputStream().use { it.copyTo(o, 1 shl 20) } }
            values.clear(); values.put(MediaStore.Video.Media.IS_PENDING, 0)
            r.update(uri, values, null, null)
            path = uri.toString()
        } else {
            @Suppress("DEPRECATION")
            val dir = File(Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_MOVIES), "YT Clips").apply { mkdirs() }
            var out = File(dir, displayName)
            var i = 2
            while (out.exists()) { out = File(dir, displayName.removeSuffix(".mp4") + " ($i).mp4"); i++ }
            tmp.copyTo(out)
            MediaScannerConnection.scanFile(app, arrayOf(out.absolutePath), arrayOf("video/mp4"), null)
            path = out.absolutePath
        }
        tmp.delete()
        return JSONObject().put("name", displayName).put("path", path).put("size", size)
    }

    private fun uriFor(path: String): Uri =
        if (path.startsWith("content://")) Uri.parse(path)
        else FileProvider.getUriForFile(app, app.packageName + ".files", File(path))

    // ------------------------------------------------------------------ API
    fun call(name: String, a: JSONObject): JSONObject = try {
        when (name) {
            "init" -> apiInit()
            "state" -> apiState(a)
            "load" -> apiLoad(a)
            "export" -> apiExport(a)
            "cancel" -> apiCancel()
            "record_start" -> apiRecordStart(a)
            "record_stop" -> apiRecordStop(a)
            "update" -> apiUpdate()
            "save_settings" -> apiSaveSettings(a)
            "open_file" -> apiOpen(a, false)
            "share" -> apiOpen(a, true)
            "clipboard" -> apiClipboard()
            "browse", "open_folder" -> err("Clips are saved in Movies/YT Clips. Open them from your gallery or Files app.")
            else -> err("unknown action")
        }
    } catch (e: Throwable) {
        err(e.message ?: e.toString())
    }

    private fun apiInit() = ok().apply {
        put("platform", "android"); put("version", VERSION); put("settings", settings())
        put("ytdlp", ytdlpVersion() ?: "starting…"); put("ffmpeg", true)
        put("canBrowse", false); put("canCookies", false); put("canQuit", false); put("canOpenFolder", false)
        put("setupHint", "The app couldn't start yt-dlp. Reinstall the app.")
    }

    private fun apiState(a: JSONObject): JSONObject {
        val since = a.optInt("since", 0)
        synchronized(lock) {
            val r = ok()
            r.put("busy", busy)
            r.put("task", task ?: prepTask)
            r.put("progress", progress ?: JSONObject.NULL)
            r.put("logs", JSONArray(logs.filter { it.getInt("id") > since }))
            r.put("logNext", logId)
            r.put("lastLog", logs.lastOrNull()?.getString("msg") ?: JSONObject.NULL)
            r.put("infoSeq", infoSeq); r.put("loadFail", loadFail)
            r.put("export", export ?: JSONObject.NULL)
            r.put("outputsCount", outputs.length()); r.put("outputs", outputs)
            r.put("ytdlp", ytdlpVersion() ?: "starting…")
            if (a.optInt("infoSeq", -99) != infoSeq) r.put("info", info?.let { uiInfo(it) } ?: JSONObject.NULL)
            rec?.let {
                r.put("rec", JSONObject().put("state", recState).put("elapsed", it.elapsed()).put("size", it.size()).put("title", recTitle))
            }
            return r
        }
    }

    private fun apiLoad(a: JSONObject): JSONObject {
        val url = a.optString("url").trim()
        if (url.isEmpty()) return err("Paste a link first.")
        if (rec != null) return err("Stop the recording first.")
        return runBg("Loading link…") {
            log("Loading: $url")
            val i = try { fetchInfo(url) } catch (e: Throwable) { synchronized(lock) { loadFail++ }; throw e }
            if (i.optString("_type") == "playlist") {
                synchronized(lock) { loadFail++ }
                throw RuntimeException("That is a playlist or channel link. Open a single video or stream and copy its link.")
            }
            synchronized(lock) { info = i; infoSeq++; export = null }
            log("Loaded: ${i.optString("title")} [${i.optString("live_status", "")}]", "ok")
        }
    }

    private fun apiExport(a: JSONObject): JSONObject {
        val inf = info ?: return err("Load a video first.")
        if (inf.optBoolean("is_live")) return err("This stream is still live. Record it, or wait until it ends to clip it.")
        val clipsArr = a.optJSONArray("clips") ?: JSONArray()
        if (clipsArr.length() == 0) return err("Add at least one clip.")
        val clips = (0 until clipsArr.length()).map { clipsArr.getJSONObject(it) }
        val vertical = a.optBoolean("vertical", prefs.getBoolean("vertical", false))
        val mh = maxHeight(a.optString("quality", null))
        synchronized(lock) {
            val items = JSONObject()
            clips.forEach { items.put(it.getString("uid"), JSONObject().put("status", "queued").put("progress", 0)) }
            export = JSONObject().put("items", items)
        }
        fun setItem(uid: String, status: String, prog: Double? = null) = synchronized(lock) {
            export?.optJSONObject("items")?.optJSONObject(uid)?.apply { put("status", status); if (prog != null) put("progress", prog) }
        }
        return runBg("Exporting clips…") {
            log("Refreshing stream links…")
            val fresh = fetchInfo(inf.str("webpage_url") ?: inf.optString("original_url"))
            val (v, au) = pickVodFormats(fresh, mh)
            log("Using ${v.str("height") ?: "?"}p (${v.str("vcodec")})" + if (au != null) " + audio" else "")
            val base = safeName(fresh.str("title") ?: "clip", 60)
            val total = fresh.num("duration")
            var nOk = 0
            clips.forEachIndexed { idx, c ->
                val n = idx + 1
                val uid = c.getString("uid")
                if (cancelFlag) { clips.drop(idx).forEach { setItem(it.getString("uid"), "canceled") }; throw Canceled() }
                val s = c.getDouble("start")
                var e = c.getDouble("end")
                if (total > 0) e = min(e, total)
                if (e <= s) { setItem(uid, "error"); log("[$n/${clips.size}] skipped: end is not after start", "error"); return@forEachIndexed }
                val nm = c.optString("name").trim()
                val label = if (nm.isNotEmpty()) safeName(nm) else "$base ${fmtTime(s).replace(':', '-')}"
                val display = label + (if (vertical) " (vertical)" else "") + ".mp4"
                val tmp = File(workDir(), "clip_${System.currentTimeMillis()}.mp4")
                task = "Exporting clip $n of ${clips.size}"
                log("[$n/${clips.size}] ${fmtTime(s)} - ${fmtTime(e)} -> $display")
                setItem(uid, "running", 0.0)
                try {
                    runFfmpeg(buildClipArgs(v, au, fresh, s, e, vertical, tmp), e - s) { p -> progress = p; setItem(uid, "running", p) }
                } catch (ex: Throwable) {
                    tmp.delete()
                    if (cancelFlag) { clips.drop(idx).forEach { setItem(it.getString("uid"), "canceled") }; throw Canceled() }
                    setItem(uid, "error"); throw ex
                }
                val saved = publish(tmp, display)
                synchronized(lock) { outputs.put(saved) }
                setItem(uid, "done", 100.0)
                nOk++
                log("    Saved: $OUT_LABEL/${saved.getString("name")}", "ok")
            }
            log("Done. $nOk clip${if (nOk != 1) "s" else ""} saved to $OUT_LABEL", "ok", true)
        }
    }

    private fun apiCancel(): JSONObject {
        cancelFlag = true
        ffProc?.let { if (it.isAlive) it.destroy() }
        return ok()
    }

    private fun apiRecordStart(a: JSONObject): JSONObject {
        val inf = info
        if (inf == null || !inf.optBoolean("is_live")) return err("Load a stream that is live right now.")
        if (rec != null) return err("Already recording.")
        val mh = maxHeight(a.optString("quality", null))
        return runBg("Starting recording…") {
            log("Getting the live stream…")
            val fresh = fetchInfo(inf.optString("webpage_url"))
            if (!fresh.optBoolean("is_live")) throw RuntimeException("The stream isn't live anymore. Load it again and clip it instead.")
            val fmt = pickLiveFormat(fresh, mh)
            val ts = File(workDir(), "live_${System.currentTimeMillis()}.ts")
            val cmd = listOf(ffmpegBin.absolutePath, "-hide_banner", "-y", "-loglevel", "error") + headerArgs(fmt, fresh) +
                listOf("-i", fmt.getString("url"), "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy", "-f", "mpegts", ts.absolutePath)
            synchronized(lock) {
                rec = LiveRecorder(ts, cmd) { procEnv(it) }
                recTitle = fresh.str("title") ?: "live"
                recState = "recording"
            }
            log("Recording at ${fmt.str("height") ?: "?"}p. Tap the button again to stop and save.", "ok", true)
        }
    }

    private fun apiRecordStop(a: JSONObject): JSONObject {
        val r: LiveRecorder
        synchronized(lock) {
            r = rec ?: return err("Not recording.")
            if (recState != "recording") return err("Not recording.")
            recState = "saving"
        }
        val vertical = a.optBoolean("vertical", prefs.getBoolean("vertical", false))
        Thread { saveRecording(r, vertical) }.start()
        return ok()
    }

    private fun saveRecording(r: LiveRecorder, vertical: Boolean) {
        synchronized(lock) { busy = true; task = "Saving recording…"; progress = null }
        try {
            val dur = r.stop()
            if (!r.ts.exists() || r.ts.length() < 1024) throw RuntimeException("Nothing was recorded (the stream may have blocked the download).")
            val stamp = SimpleDateFormat("yyyy-MM-dd HH-mm-ss", Locale.US).format(Date())
            val display = "${safeName(recTitle ?: "live", 60)} LIVE $stamp" + (if (vertical) " (vertical)" else "") + ".mp4"
            val tmp = File(workDir(), "rec_${System.currentTimeMillis()}.mp4")
            log("Saving ${fmtTime(dur)} of recording…")
            val args = if (vertical) listOf("-i", r.ts.absolutePath, "-vf", VERTICAL_VF) + ENCODE_ARGS + tmp.absolutePath
            else listOf("-i", r.ts.absolutePath, "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy", "-bsf:a", "aac_adtstoasc", "-movflags", "+faststart", tmp.absolutePath)
            runFfmpeg(args, dur) { progress = it }
            r.ts.delete()
            val saved = publish(tmp, display)
            synchronized(lock) { outputs.put(saved) }
            log("Recording saved: $OUT_LABEL/${saved.getString("name")}", "ok", true)
        } catch (e: Throwable) {
            log("ERROR: " + cleanError(e), "error", true)
        } finally {
            synchronized(lock) { rec = null; recState = null; recTitle = null; busy = false; task = null; progress = null }
        }
    }

    private fun watchRecorder() {
        while (true) {
            Thread.sleep(1000)
            val r = rec
            if (r != null && recState == "recording" && !r.alive()) {
                log("The live stream ended or the connection dropped. Saving what was recorded.", "info", true)
                apiRecordStop(JSONObject().put("vertical", prefs.getBoolean("vertical", false)))
            }
        }
    }

    private fun apiUpdate(): JSONObject = runBg("Updating yt-dlp…") {
        log("Updating yt-dlp…")
        val st = YoutubeDL.updateYoutubeDL(app, YoutubeDL.UpdateChannel.STABLE)
        prefs.edit().putLong("ytdlp_checked", System.currentTimeMillis()).apply()
        if (st == YoutubeDL.UpdateStatus.ALREADY_UP_TO_DATE) log("yt-dlp is already up to date (${ytdlpVersion()}).", "ok", true)
        else log("yt-dlp updated to ${ytdlpVersion()}.", "ok", true)
    }

    private fun apiSaveSettings(a: JSONObject): JSONObject {
        val s = a.optJSONObject("settings") ?: return ok()
        prefs.edit().apply {
            s.optString("quality").takeIf { it in QUALITIES }?.let { putString("quality", it) }
            if (s.has("vertical")) putBoolean("vertical", s.optBoolean("vertical"))
        }.apply()
        return ok()
    }

    private fun apiOpen(a: JSONObject, share: Boolean): JSONObject {
        val path = a.optString("path")
        val known = synchronized(lock) { (0 until outputs.length()).any { outputs.getJSONObject(it).optString("path") == path } }
        if (!known) return err("That file isn't there anymore.")
        val uri = uriFor(path)
        val intent = if (share) {
            Intent.createChooser(Intent(Intent.ACTION_SEND).apply {
                type = "video/mp4"; putExtra(Intent.EXTRA_STREAM, uri); addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
            }, "Share clip")
        } else Intent(Intent.ACTION_VIEW).apply { setDataAndType(uri, "video/mp4"); addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION) }
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        main.post {
            try { app.startActivity(intent) } catch (e: Throwable) { log("ERROR: No app found to open videos.", "error", true) }
        }
        return ok()
    }

    private fun apiClipboard(): JSONObject {
        var text = ""
        val latch = CountDownLatch(1)
        main.post {
            try {
                val cm = app.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
                text = cm.primaryClip?.takeIf { it.itemCount > 0 }?.getItemAt(0)?.coerceToText(app)?.toString() ?: ""
            } catch (_: Throwable) {}
            latch.countDown()
        }
        latch.await(2, TimeUnit.SECONDS)
        return ok().put("text", text)
    }

    /** For the notification while working in the background. */
    fun statusLine(): String? {
        val r = rec
        if (r != null && recState == "recording") return "Recording ${fmtTime(r.elapsed())}, ${"%.1f".format(Locale.US, r.size() / 1e6)} MB"
        if (busy) return (task ?: "Working…") + (progress?.let { " ${it.roundToInt()}%" } ?: "")
        return null
    }
}
