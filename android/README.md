# Alnasrawy TV — Android app

Native Android client for the Alnasrawy streaming backend. Arabic RTL UI, dark
theme, HLS playback through the backend's `/stream` proxy.

## Stack

- Kotlin + XML, minSdk 26 / target & compile SDK 34
- AGP 8.5.2, Gradle 8.7 (wrapper properties committed; Android Studio will
  generate the `gradle-wrapper.jar` on first open)
- ExoPlayer via Media3 (`media3-exoplayer`, `media3-exoplayer-hls`, `media3-ui`)
- OkHttp (networking), Glide (posters), Material Components, coroutines
- JSON parsed with `org.json` — no serialization/kapt plugins

## Screens

| Screen        | What it does                                                        |
|---------------|----------------------------------------------------------------------|
| `MainActivity`| Home: popular movies, popular shows, weekly trending — horizontal rows|
| `SearchActivity`| TMDB multi-search (movies + shows), grid of poster cards           |
| `WatchActivity`| `POST /watch` → server list (سيرفر N + site + kind); season/episode pickers for TV (debounced reload) |
| `PlayerActivity`| ExoPlayer playing the server's `proxy_url`; error overlay + retry  |

The app never holds the TMDB API key — it browses through the backend's
`/tmdb/*` endpoints and resolves playback through `POST /watch`, per the
roadmap contract (resolve-per-play, tokens expire).

## Pointing at a backend

`BASE_URL` lives in `app/src/main/java/com/alnasrawy/tv/data/Api.kt`:

- Default: `https://movie-serial-scraper.onrender.com`
- Emulator → local server: `http://10.0.2.2:8000`
- Physical device on LAN: `http://<your-pc-ip>:8000`

## Build

1. Open the `android/` folder in Android Studio (Ladybug+).
2. Let it generate the Gradle wrapper, then `Build > Build APK`.
3. Install on a device/emulator. Minimum Android 8.0 (API 26).

```powershell
# If you have a local SDK, you can also build from the CLI:
& "$env:LOCALAPPDATA\Android\Sdk\cmdline-tools\latest\bin\sdkmanager.bat" --install "platforms;android-34"
# then in android/
.\gradlew.bat assembleDebug
```

## Notes

- Playback is per-server `proxy_url` fetched at play time (short-lived CDN
  tokens, so never cache it long).
- `POST /watch` also returns `subtitles` (language list). Subtitle UI is
  intentionally deferred.
- Release builds are not minified yet (no keystore committed).
