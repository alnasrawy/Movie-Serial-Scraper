package com.alnasrawy.tv.ui

import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.view.inputmethod.EditorInfo
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.lifecycleScope
import androidx.recyclerview.widget.GridLayoutManager
import com.alnasrawy.tv.data.Api
import com.alnasrawy.tv.databinding.ActivitySearchBinding
import com.alnasrawy.tv.model.MediaItem
import kotlinx.coroutines.launch

class SearchActivity : AppCompatActivity() {

    private lateinit var binding: ActivitySearchBinding
    private var lastQuery: String = ""

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivitySearchBinding.inflate(layoutInflater)
        setContentView(binding.root)

        binding.results.layoutManager = GridLayoutManager(this, 3)

        lastQuery = intent.getStringExtra(EXTRA_QUERY).orEmpty()
        binding.toolbar.title = lastQuery

        binding.searchInput.setText(lastQuery)
        binding.searchInput.setOnEditorActionListener { _, action, _ ->
            if (action == EditorInfo.IME_ACTION_SEARCH) {
                search()
                true
            } else false
        }
        binding.searchBox.setEndIconOnClickListener { search() }

        if (lastQuery.isNotEmpty()) search()
    }

    private fun search() {
        val q = binding.searchInput.text?.toString()?.trim().orEmpty()
        if (q.isEmpty()) {
            Toast.makeText(this, R.string.search_hint, Toast.LENGTH_SHORT).show()
            return
        }
        lastQuery = q
        binding.progress.visibility = android.view.View.VISIBLE
        binding.emptyText.visibility = android.view.View.GONE
        lifecycleScope.launch {
            try {
                val results = Api.search(q)
                binding.results.adapter = MediaAdapter(results) { item ->
                    WatchActivity.start(this@SearchActivity, item.tmdbId, item.mediaType, item.title)
                }
                if (results.isEmpty()) {
                    binding.emptyText.visibility = android.view.View.VISIBLE
                }
            } catch (e: Exception) {
                binding.emptyText.visibility = android.view.View.VISIBLE
            } finally {
                binding.progress.visibility = android.view.View.GONE
            }
        }
    }

    companion object {
        private const val EXTRA_QUERY = "query"

        fun start(context: Context, query: String) {
            context.startActivity(
                Intent(context, SearchActivity::class.java).putExtra(EXTRA_QUERY, query)
            )
        }
    }
}
