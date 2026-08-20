package com.alnasrawy.tv.ui

import android.view.LayoutInflater
import android.view.ViewGroup
import androidx.recyclerview.widget.RecyclerView
import com.alnasrawy.tv.databinding.ItemMediaBinding
import com.alnasrawy.tv.model.MediaItem
import com.bumptech.glide.Glide
import com.bumptech.glide.load.resource.drawable.DrawableTransitionOptions

class MediaAdapter(
    private val items: List<MediaItem>,
    private val onClick: (MediaItem) -> Unit,
) : RecyclerView.Adapter<MediaAdapter.Holder>() {

    override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): Holder {
        val binding = ItemMediaBinding.inflate(LayoutInflater.from(parent.context), parent, false)
        return Holder(binding)
    }

    override fun getItemCount(): Int = items.size

    override fun onBindViewHolder(holder: Holder, position: Int) {
        holder.bind(items[position])
    }

    inner class Holder(private val binding: ItemMediaBinding) : RecyclerView.ViewHolder(binding.root) {
        fun bind(item: MediaItem) {
            binding.title.text = item.title.ifBlank { item.originalTitle }
            if (item.posterUrl.isNotEmpty()) {
                Glide.with(binding.poster)
                    .load(item.posterUrl)
                    .transition(DrawableTransitionOptions.withCrossFade())
                    .into(binding.poster)
            }
            binding.root.setOnClickListener { onClick(item) }
        }
    }
}
