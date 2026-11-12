package com.madebytwins.ytliveclipper

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.PowerManager
import androidx.core.app.NotificationCompat
import androidx.core.app.ServiceCompat
import androidx.core.content.ContextCompat

/** Keeps the app alive (screen off / app in background) while exporting or recording. */
class ClipperService : Service() {
    private val handler = Handler(Looper.getMainLooper())
    private var idleTicks = 0
    private var wake: PowerManager.WakeLock? = null

    companion object {
        private const val CHANNEL = "work"
        private const val ID = 7
        @Volatile private var running = false

        fun ensureRunning(ctx: Context) {
            if (running) return
            try {
                ContextCompat.startForegroundService(ctx, Intent(ctx, ClipperService::class.java))
            } catch (_: Throwable) { /* app not in foreground: work still runs while the app is open */ }
        }
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        val nm = getSystemService(NotificationManager::class.java)
        if (Build.VERSION.SDK_INT >= 26 && nm.getNotificationChannel(CHANNEL) == null)
            nm.createNotificationChannel(NotificationChannel(CHANNEL, "Exports and recordings", NotificationManager.IMPORTANCE_LOW))
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        running = true
        val type = if (Build.VERSION.SDK_INT >= 29) ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC else 0
        ServiceCompat.startForeground(this, ID, build(Engine.statusLine() ?: "Working…"), type)
        if (wake == null) {
            wake = (getSystemService(POWER_SERVICE) as PowerManager)
                .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "ytlc:work").apply { setReferenceCounted(false); acquire(12 * 3600 * 1000L) }
        }
        handler.removeCallbacksAndMessages(null)
        handler.post(tick)
        return START_NOT_STICKY
    }

    private val tick = object : Runnable {
        override fun run() {
            val line = Engine.statusLine()
            if (line == null) {
                if (++idleTicks >= 3) { stopSelf(); return }
            } else {
                idleTicks = 0
                getSystemService(NotificationManager::class.java).notify(ID, build(line))
            }
            handler.postDelayed(this, 1000)
        }
    }

    private fun build(text: String): Notification {
        val open = PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java)
            .addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP), PendingIntent.FLAG_IMMUTABLE)
        return NotificationCompat.Builder(this, CHANNEL)
            .setSmallIcon(R.drawable.ic_stat)
            .setContentTitle("YT Live Clipper")
            .setContentText(text)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setContentIntent(open)
            .build()
    }

    override fun onTimeout(startId: Int, fgsType: Int) { stopSelf() }

    override fun onDestroy() {
        running = false
        handler.removeCallbacksAndMessages(null)
        try { wake?.release() } catch (_: Throwable) {}
        wake = null
        super.onDestroy()
    }
}
