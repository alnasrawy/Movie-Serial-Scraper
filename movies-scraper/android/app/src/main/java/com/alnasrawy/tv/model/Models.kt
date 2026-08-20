package com.alnasrawy.tv.model

/**
 * A compact TMDB record as returned by the backend browse endpoints
 * (/tmdb/popular, /tmdb/trending, /tmdb/search).
 */
data class MediaItem(
    val tmdbId: Long,
    val mediaType: String,      // "movie" | "tv"
    val title: String,
    val originalTitle: String,
    val overview: String,
    val posterUrl: String,      // absolute https://image.tmdb.org/... or ""
    val voteAverage: Double,
)

/** One playable server as returned by POST /watch. */
data class ServerItem(
    val site: String,
    val name: String,
    val kind: String,           // "hls" | "mp4" | ...
    val proxyUrl: String,       // absolute {base}/stream?sid=...&url=...
)

/** Subtitle language offered by /watch (may be empty on old backends). */
data class SubtitleLang(
    val code: String,
    val name: String,
    val count: Int,
)

/** Full POST /watch response. */
data class WatchResult(
    val tmdbId: Long?,
    val imdbId: String?,
    val subtitles: List<SubtitleLang>,
    val servers: List<ServerItem>,
)
