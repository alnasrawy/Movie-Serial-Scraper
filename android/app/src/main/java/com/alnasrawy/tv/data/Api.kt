package com.alnasrawy.tv.data

import com.alnasrawy.tv.model.MediaItem
import com.alnasrawy.tv.model.ServerItem
import com.alnasrawy.tv.model.SubtitleLang
import com.alnasrawy.tv.model.WatchResult
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/**
 * Thin HTTP client for the Alnasrawy backend. All parsing uses org.json to
 * keep the build dependency-light (no kapt/ksp serialization plugins).
 */
object Api {

    /**
     * Point this at your deployed backend (Dockerfile.lite on Render).
     * For a local run: http://10.0.2.2:8000  (Android emulator -> host loopback)
     */
    var BASE_URL: String = "https://movie-serial-scraper.onrender.com"

    private val json = "application/json; charset=utf-8".toMediaType()

    private val client: OkHttpClient by lazy {
        OkHttpClient.Builder()
            .connectTimeout(30, TimeUnit.SECONDS)
            .readTimeout(180, TimeUnit.SECONDS)
            .callTimeout(180, TimeUnit.SECONDS)
            .build()
    }

    class ApiError(message: String) : Exception(message)

    private fun getJson(path: String): JSONObject = withContext(Dispatchers.IO) {
        val req = Request.Builder().url(BASE_URL + path).get().build()
        client.newCall(req).execute().use { resp ->
            val body = resp.body?.string().orEmpty()
            if (!resp.isSuccessful) {
                throw ApiError("HTTP ${resp.code}: ${body.take(120)}")
            }
            JSONObject(body)
        }
    }

    private fun postJson(path: String, payload: JSONObject): JSONObject = withContext(Dispatchers.IO) {
        val req = Request.Builder()
            .url(BASE_URL + path)
            .post(payload.toString().toRequestBody(json))
            .build()
        client.newCall(req).execute().use { resp ->
            val body = resp.body?.string().orEmpty()
            if (!resp.isSuccessful) {
                throw ApiError("HTTP ${resp.code}: ${body.take(120)}")
            }
            JSONObject(body)
        }
    }

    private fun parseMedia(item: JSONObject): MediaItem {
        val title = item.optString("title").ifEmpty { item.optString("name") }
        return MediaItem(
            tmdbId = item.optLong("tmdb_id"),
            mediaType = item.optString("media_type", "movie"),
            title = title,
            originalTitle = item.optString("original_title"),
            overview = item.optString("overview"),
            posterUrl = item.optString("poster"),
            voteAverage = item.optDouble("vote_average", 0.0),
        )
    }

    /** Fetch a page of popular titles (type = "movie" | "tv"). */
    suspend fun popular(type: String, page: Int = 1): List<MediaItem> {
        val obj = getJson("/tmdb/popular?type=$type&page=$page")
        return obj.optJSONArray("items")?.let { arr ->
            (0 until arr.length()).map { parseMedia(arr.getJSONObject(it)) }
        } ?: emptyList()
    }

    /** Fetch trending titles for the last week/day. */
    suspend fun trending(time: String = "week", page: Int = 1): List<MediaItem> {
        val obj = getJson("/tmdb/trending?time=$time&page=$page")
        return obj.optJSONArray("items")?.let { arr ->
            (0 until arr.length()).map { parseMedia(arr.getJSONObject(it)) }
        } ?: emptyList()
    }

    /** Multi-search (movies + shows) by title. */
    suspend fun search(query: String, page: Int = 1): List<MediaItem> {
        val obj = getJson("/tmdb/search?q=${android.net.Uri.encode(query)}&page=$page")
        return obj.optJSONArray("items")?.let { arr ->
            (0 until arr.length()).map { parseMedia(arr.getJSONObject(it)) }
        } ?: emptyList()
    }

    /** Resolve a title to its playable server list. */
    suspend fun watch(
        tmdbId: Long,
        type: String,
        season: Int? = null,
        episode: Int? = null,
    ): WatchResult {
        val payload = JSONObject()
            .put("tmdb_id", tmdbId)
            .put("type", type)
        if (season != null) payload.put("season", season)
        if (episode != null) payload.put("episode", episode)

        val obj = postJson("/watch", payload)
        val servers = obj.optJSONArray("servers")?.let { arr ->
            (0 until arr.length()).mapNotNull { i ->
                val s = arr.getJSONObject(i)
                val url = s.optString("proxy_url")
                if (url.isEmpty()) null else ServerItem(
                    site = s.optString("site"),
                    name = s.optString("name"),
                    kind = s.optString("kind"),
                    proxyUrl = url,
                )
            }
        } ?: emptyList()

        val subs = obj.optJSONArray("subtitles")?.let { arr ->
            (0 until arr.length()).map { i ->
                val s = arr.getJSONObject(i)
                SubtitleLang(
                    code = s.optString("code"),
                    name = s.optString("name"),
                    count = s.optInt("count", 0),
                )
            }
        } ?: emptyList()

        return WatchResult(
            tmdbId = if (obj.isNull("tmdb_id")) null else obj.optLong("tmdb_id"),
            imdbId = obj.optString("imdb_id").ifEmpty { null },
            subtitles = subs,
            servers = servers,
        )
    }
}
