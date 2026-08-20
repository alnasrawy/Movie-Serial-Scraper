package com.alnasrawy.tv.ui

import android.os.Bundle
import android.view.inputmethod.EditorInfo
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.lifecycleScope
import androidx.recyclerview.widget.LinearLayoutManager
import com.alnasrawy.tv.data.Api
import com.alnasrawy.tv.databinding.ActivityMainBinding
import com.alnasrawy.tv.model.MediaItem
import kotlinx.coroutines.launch

class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

        binding.listPopularMovies.layoutManager =
            LinearLayoutManager(this, LinearLayoutManager.HORIZONTAL, false)
        binding.listPopularTv.layoutManager =
            LinearLayoutManager(this, LinearLayoutManager.HORIZONTAL, false)
        binding.listTrending.layoutManager =
            LinearLayoutManager(this, LinearLayoutManager.HORIZONTAL, false)

        binding.searchInput.setOnEditorActionListener { _, action, _ ->
            if (action == EditorInfo.IME_ACTION_SEARCH) {
                submitSearch()
                true
            } else false
        }
        binding.searchBox.setEndIconOnClickListener { submitSearch() }

        loadAll()
    }

    private fun submitSearch() {
        val q = binding.searchInput.text?.toString()?.trim().orEmpty()
        if (q.isNotEmpty()) {
            SearchActivity.start(this, q)
        }
    }

    private fun loadAll() {
        binding.progress.visibility = android.view.View.VISIBLE
        lifecycleScope.launch {
            try {
                setSection(binding.listPopularMovies, Api.popular("movie"))
                setSection(binding.listPopularTv, Api.popular("tv"))
                setSection(binding.listTrending, Api.trending("week"))
            } catch (e: Exception) {
                // Keep sections empty; the user can search or reopen the app.
            } finally {
                binding.progress.visibility = android.view.View.GONE
            }
        }
    }

    private fun setSection(
        list: androidx.recyclerview.widget.RecyclerView,
        items: List<MediaItem>,
    ) {
        list.adapter = MediaAdapter(items, ::openTitle)
    }

    private fun openTitle(item: MediaItem) {
        WatchActivity.start(this, item.tmdbId, item.mediaType, item.title)
    }
}
