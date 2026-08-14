package com.alnasrawy.tv.ui

import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.text.Editable
import android.text.TextWatcher
import android.view.LayoutInflater
import android.view.ViewGroup
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.lifecycleScope
import androidx.recyclerview.widget.LinearLayoutManager
import androidx.recyclerview.widget.RecyclerView
import com.alnasrawy.tv.R
import com.alnasrawy.tv.data.Api
import com.alnasrawy.tv.databinding.ActivityWatchBinding
import com.alnasrawy.tv.databinding.ItemServerBinding
import com.alnasrawy.tv.model.ServerItem
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

class WatchActivity : AppCompatActivity() {

    private lateinit var binding: ActivityWatchBinding
    private var tmdbId: Long = 0L
    private var mediaType: String = "movie"
    private var title: String = ""
    private var reloadJob: Job? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityWatchBinding.inflate(layoutInflater)
        setContentView(binding.root)

        tmdbId = intent.getLongExtra(EXTRA_TMDB_ID, 0L)
        mediaType = intent.getStringExtra(EXTRA_TYPE) ?: "movie"
        title = intent.getStringExtra(EXTRA_TITLE) ?: ""

        binding.toolbar.title = title.ifBlank { getString(R.string.watch_title) }

        if (mediaType == "tv") {
            binding.episodeRow.visibility = android.view.View.VISIBLE
            val watcher = object : TextWatcher {
                override fun beforeTextChanged(s: CharSequence?, a: Int, b: Int, c: Int) {}
                override fun onTextChanged(s: CharSequence?, a: Int, b: Int, c: Int) {
                    reloadJob?.cancel()
                    reloadJob = lifecycleScope.launch {
                        delay(600)
                        load()
                    }
                }
                override fun afterTextChanged(s: Editable?) {}
            }
            binding.seasonInput.addTextChangedListener(watcher)
            binding.episodeInput.addTextChangedListener(watcher)
        }

        binding.serverList.layoutManager = LinearLayoutManager(this)
        binding.retryButton.setOnClickListener { load() }

        load()
    }

    private fun season(): Int? = binding.seasonInput.text?.toString()?.toIntOrNull()
    private fun episode(): Int? = binding.episodeInput.text?.toString()?.toIntOrNull()

    private fun load() {
        binding.progress.visibility = android.view.View.VISIBLE
        binding.statusText.visibility = android.view.View.GONE
        binding.retryButton.visibility = android.view.View.GONE
        binding.serverList.adapter = null

        lifecycleScope.launch {
            try {
                val season = if (mediaType == "tv") season() else null
                val episode = if (mediaType == "tv") episode() else null
                val result = Api.watch(tmdbId, mediaType, season, episode)
                val servers = result.servers
                if (servers.isEmpty()) {
                    binding.statusText.text = getString(R.string.watch_empty)
                    binding.statusText.visibility = android.view.View.VISIBLE
                    binding.retryButton.visibility = android.view.View.VISIBLE
                } else {
                    binding.serverList.adapter = ServerAdapter(servers) { server ->
                        PlayerActivity.start(this@WatchActivity, server.proxyUrl, title)
                    }
                }
            } catch (e: Exception) {
                binding.statusText.text = getString(R.string.watch_error)
                binding.statusText.visibility = android.view.View.VISIBLE
                binding.retryButton.visibility = android.view.View.VISIBLE
            } finally {
                binding.progress.visibility = android.view.View.GONE
            }
        }
    }

    private class ServerAdapter(
        private val servers: List<ServerItem>,
        private val onClick: (ServerItem) -> Unit,
    ) : RecyclerView.Adapter<ServerAdapter.Holder>() {

        override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): Holder {
            val binding = ItemServerBinding.inflate(LayoutInflater.from(parent.context), parent, false)
            return Holder(binding)
        }

        override fun getItemCount(): Int = servers.size

        override fun onBindViewHolder(holder: Holder, position: Int) {
            holder.bind(servers[position])
        }

        inner class Holder(private val binding: ItemServerBinding) : RecyclerView.ViewHolder(binding.root) {
            fun bind(server: ServerItem) {
                binding.serverName.text = server.name
                binding.kind.text = if (server.kind.contains("hls", ignoreCase = true)) {
                    binding.kind.context.getString(R.string.server_kind_hls)
                } else {
                    binding.kind.context.getString(R.string.server_kind_mp4)
                }
                binding.siteLabel.text = binding.root.context.getString(R.string.server_site_from, server.site)
                binding.root.setOnClickListener { onClick(server) }
            }
        }
    }

    companion object {
        private const val EXTRA_TMDB_ID = "tmdb_id"
        private const val EXTRA_TYPE = "type"
        private const val EXTRA_TITLE = "title"

        fun start(context: Context, tmdbId: Long, type: String, title: String) {
            context.startActivity(
                Intent(context, WatchActivity::class.java)
                    .putExtra(EXTRA_TMDB_ID, tmdbId)
                    .putExtra(EXTRA_TYPE, type)
                    .putExtra(EXTRA_TITLE, title)
            )
        }
    }
}
