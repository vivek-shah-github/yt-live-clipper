plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.madebytwins.ytliveclipper"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.madebytwins.ytliveclipper"
        minSdk = 26
        targetSdk = 35
        versionCode = 1
        versionName = "2.0"
    }

    // The signing key is NOT in this repository. Put your keystore at android/keystore/ytliveclipper.jks
    // (or set YTLC_KEYSTORE + YTLC_KEY_PASSWORD); without it the release build is signed with the debug key.
    val ks = file(System.getenv("YTLC_KEYSTORE") ?: "../keystore/ytliveclipper.jks")
    val pw = System.getenv("YTLC_KEY_PASSWORD") ?: "ytliveclipper"
    signingConfigs {
        create("release") {
            storeFile = ks
            storePassword = pw
            keyAlias = "ytliveclipper"
            keyPassword = pw
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            signingConfig = if (ks.exists()) signingConfigs.getByName("release") else signingConfigs.getByName("debug")
        }
    }

    // One APK per phone type keeps the download smaller (arm64 = almost every phone from the last 7 years).
    splits {
        abi {
            isEnable = true
            reset()
            include("arm64-v8a", "armeabi-v7a", "x86_64")
            isUniversalApk = false
        }
    }

    // yt-dlp's Python and ffmpeg are run as programs, so they must be unpacked on install.
    packaging {
        jniLibs { useLegacyPackaging = true }
        resources { excludes += setOf("META-INF/DEPENDENCIES", "META-INF/LICENSE*", "META-INF/NOTICE*") }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
}

dependencies {
    implementation("io.github.junkfood02.youtubedl-android:library:0.18.1")
    implementation("io.github.junkfood02.youtubedl-android:ffmpeg:0.18.1")
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("androidx.webkit:webkit:1.11.0")
}
