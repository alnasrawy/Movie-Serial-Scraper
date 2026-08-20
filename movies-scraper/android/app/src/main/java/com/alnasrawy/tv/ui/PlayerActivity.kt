package com.alnasrawy.tv.ui

import android.content.Context
import android.content.Intent
import android.os.Bundle
import androidx.appcompat.app.AppCompatActivity
import androidx.media3.common.MediaItem
import androidx.media3.common.PlaybackException
import androidx.media3.common.Player
import androidx.media3.exoplayer.ExoPlayer
import com.alnasrawy.tv.R
import com.alnasrawy.tv.databinding.ActivityPlayerBinding

class PlayerActivity : AppCompatActivity() {

    private lateinit var binding: ActivityPlayerBinding
    private var player: ExoPlayer? = null
    private var mediaUrl: String = ""

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityPlayerBinding.inflate(layoutInflater)
        setContentView(binding.root)

        mediaUrl = intent.getStringExtra(EXTRA_URL).orEmpty()
        binding.retryButton.setOnClickListener { play() }

        play()
    }

    private fun play() {
        if (mediaUrl.isEmpty()) {
            showError()
            return
        }
        binding.buffering.visibility = android.view.View.VISIBLE
        binding.errorOverlay.visibility = android.view.View.GONE

        player?.release()
        val p = ExoPlayer.Builder(this).build().also { exo ->
            exo.addListener(object : Player.Listener {
                override fun onPlaybackStateChanged(state: Int) {
                    binding.buffering.visibility =
                        if (state == Player.STATE_BUFFERING) android.view.View.VISIBLE
                        else android.view.View.GONE
                }

                override fun onPlayerError(error: PlaybackException) {
                    binding.buffering.visibility = android.view.View.GONE
                    showError()
                }
            })
        }

        // Media3's default media-source factory auto-detects HLS from .m3u8
        // and rewrites nothing: the /stream proxy already returned absolute URLs.
        p.setMediaItem(MediaItem.fromUri(mediaUrl))
        p.prepare()
        p.playWhenReady = true
        player = p
        binding.playerView.player = p
    }

    private fun showError() {
        binding.errorOverlay.visibility = android.view.View.VISIBLE
    }

    override fun onDestroy() {
        super.onDestroy()
        player?.release()
        player = null
    }

    companion object {
        private const val EXTRA_URL = "url"

        fun start(context: Context, url: String, title: String?) {
            context.startActivity(
                Intent(context, PlayerActivity::class.java)
                    .putExtra(EXTRA_URL, url)
                    .putExtra("title", title ?: "")
            )
        }
    }
}
