/* Source sizes and honest output estimates. No guessed bitrate from resolution. */
(() => {
  "use strict";
  const valid = (value) => typeof value === "number" && Number.isFinite(value) && value >= 0;
  const unknown = (reason = "The source has not provided enough size information.") => ({ bytes: null, approximate: false, reason });
  const known = (bytes, approximate = false) => ({ bytes, approximate });
  const hasVideo = (format) => typeof format.has_video === "boolean" ? format.has_video : (format.mime_type || "").startsWith("video/");
  const hasAudio = (format) => typeof format.has_audio === "boolean" ? format.has_audio : (format.mime_type || "").startsWith("audio/");

  function bytesText(bytes) {
    if (!valid(bytes)) return "Size unknown";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let value = bytes, index = 0;
    while (value >= 1024 && index < units.length - 1) { value /= 1024; index++; }
    return `${value.toFixed(index ? 1 : 0)} ${units[index]}`;
  }

  function label(size) {
    if (size?.pending) return "Calculating…";
    if (!size || !valid(size.bytes) || size.unknownCount) return "Size unavailable";
    const text = `${size.approximate ? "~" : ""}${bytesText(size.bytes)}`;
    return size.target ? `Target ${text}` : text;
  }

  function formatSize(format, duration, live = false) {
    if (!live && valid(format.size_bytes)) return known(format.size_bytes, !!format.size_is_approx);
    const bitrate = (hasVideo(format) ? format.video_bitrate_kbps || 0 : 0) + (hasAudio(format) ? format.audio_bitrate_kbps || 0 : 0);
    if (duration > 0 && bitrate > 0 && Number.isFinite(duration * bitrate)) return known(Math.round(duration * bitrate * 125), true);
    return unknown();
  }

  function sum(sizes, total = sizes.length) {
    const available = sizes.filter((size) => valid(size?.bytes));
    if (!available.length) return { ...unknown(), unknownCount: total };
    return {
      bytes: available.reduce((value, size) => value + size.bytes, 0),
      approximate: available.some((size) => size.approximate),
      unknownCount: Math.max(0, total - available.length),
    };
  }

  function itemSize(item) {
    if (item?.content_type === "livestream") return unknown("Live size depends on recording duration and bitrate.");
    if (item?._sizeProbe) return estimate(item._sizeProbe);
    return valid(item?.size_bytes) ? known(item.size_bytes, !!item.size_is_approx) : unknown();
  }

  function estimate(probe, options = {}) {
    if (!probe) return unknown();
    const type = probe.content_type;
    const mode = options.mode || (type === "audio" ? "audio" : type === "image" ? "image" : "auto");
    const live = probe.is_live || type === "livestream";
    if (options.target_size_mb) return { ...known(options.target_size_mb * 1024 * 1024), target: true };
    if (options.video_output_action || options.webpage_format || options.subtitles_only) return unknown("The output size is known after this conversion finishes.");
    if (options._useThumbnailUrl) return valid(probe.thumbnail?.size_bytes) ? known(probe.thumbnail.size_bytes) : unknown();

    if (["playlist", "gallery"].includes(type)) {
      const items = probe.items || [];
      let selected;
      if (options.playlist_items) {
        try { selected = window.NeonClient.parseIndices(options.playlist_items); } catch (_) { return unknown(); }
      } else if (options.gallery_range_end != null) {
        selected = Array.from({ length: Math.max(0, options.gallery_range_end - (options.gallery_range_start || 1) + 1) }, (_, index) => index + (options.gallery_range_start || 1));
      }
      const expected = selected ? selected.length : Math.max(items.length, probe.metadata?.item_count_total || 0);
      const byIndex = new Map(items.map((item) => [item.index, item]));
      const children = selected ? selected.map((index) => byIndex.get(index)) : items;
      const result = sum(children.map((item) => {
        if (!item) return unknown();
        if (item._sizeProbe) return estimate(item._sizeProbe, { ...options, playlist_items: undefined });
        if (item.content_type === "livestream") return unknown("Live size depends on recording duration and bitrate.");
        if (mode === "audio" && item.duration_seconds > 0) return estimate({ content_type: "audio", duration_seconds: item.duration_seconds }, { ...options, playlist_items: undefined });
        if ((options.video_quality && options.video_quality !== "best") || (options.video_codec && options.video_codec !== "auto") || (options.image_format && options.image_format !== "original") || options.image_max_width || options.strip_metadata) return unknown("Inspect the item to calculate a size for these settings.");
        return itemSize(item);
      }), expected);
      if (mode !== "auto" && valid(result.bytes)) result.approximate = true;
      return result;
    }

    let duration = live ? options.livestream_max_duration : probe.duration_seconds;
    let clipped = false;
    if (options.clip_start_seconds != null || options.clip_end_seconds != null) {
      const end = options.clip_end_seconds ?? duration;
      duration = end != null ? Math.max(0, end - (options.clip_start_seconds || 0)) : null;
      clipped = true;
    }
    const formats = probe.available_formats || [];
    if (mode === "image") {
      if (type !== "image" || (options.image_format && options.image_format !== "original") || options.image_max_width || options.strip_metadata) return unknown("Converted image size is known after encoding.");
      return valid(probe.size_bytes) ? known(probe.size_bytes, !!probe.size_is_approx) : formats.length ? formatSize(formats[0]) : unknown();
    }
    if (mode === "audio") {
      const bitrate = /^([0-9]+)k$/.exec(options.audio_bitrate || "");
      if (["flac", "wav"].includes(options.audio_format)) return unknown("Lossless encoding size depends on the source and is known after encoding.");
      if (bitrate && duration > 0) return known(Math.round(duration * Number(bitrate[1]) * 125), true);
      if (options.audio_format && options.audio_format !== "best") return unknown("Choose a bitrate to estimate the audio size.");
      const audio = formats.filter((format) => hasAudio(format) && !hasVideo(format)).sort((a, b) => (b.audio_bitrate_kbps || 0) - (a.audio_bitrate_kbps || 0));
      if (audio.length) {
        const result = formatSize(audio[0], live ? duration : probe.duration_seconds, live);
        if (clipped) return valid(result.bytes) && probe.duration_seconds > 0 && duration != null ? known(result.bytes * duration / probe.duration_seconds, true) : unknown();
        return result;
      }
      const audioBitrate = formats.find((format) => hasAudio(format) && format.audio_bitrate_kbps > 0)?.audio_bitrate_kbps;
      if (audioBitrate && duration > 0) return known(Math.round(duration * audioBitrate * 125), true);
      if (type === "audio" && valid(probe.size_bytes) && !clipped) return known(probe.size_bytes, !!probe.size_is_approx);
      return unknown(live ? "Live size depends on the recording duration and bitrate." : undefined);
    }

    if (["file", "document", "archive"].includes(type) || mode === "direct") {
      return valid(probe.size_bytes) ? known(probe.size_bytes, !!probe.size_is_approx) : formats.length ? formatSize(formats[0]) : unknown();
    }
    if (type === "webpage") return unknown("The saved article, assets, or snapshot size is known after extraction.");
    if (live && !(duration > 0)) return unknown("Live size depends on the recording duration and bitrate.");

    if (options.format_id) {
      const chosen = formats.find((format) => format.format_id === options.format_id);
      if (chosen && !chosen.requires_muxing && !clipped && !live) return formatSize(chosen, duration);
    }
    const codecPatterns = { h264: /^(avc|h264)/i, hevc: /^(hev|hvc|h265)/i, vp9: /^(vp09|vp9)/i, av1: /^(av01|av1)/i };
    let video = formats.filter(hasVideo).filter((format) => {
      if (options.format_id && format.format_id !== options.format_id) return false;
      if (options.video_quality && !["best", "worst"].includes(options.video_quality) && !(format.height > 0 && format.height <= Number(options.video_quality))) return false;
      if (options.fps_limit && format.fps > options.fps_limit) return false;
      const codec = codecPatterns[options.video_codec];
      if (codec && !codec.test(format.video_codec || "")) return false;
      return !options.video_only || !hasAudio(format);
    });
    video.sort((a, b) => (b.height || 0) - (a.height || 0) || (b.video_bitrate_kbps || 0) - (a.video_bitrate_kbps || 0));
    if (options.video_quality === "worst") video.reverse();
    if (video.length) {
      const chosen = video[0];
      let result = formatSize(chosen, live ? duration : probe.duration_seconds, live);
      if (!hasAudio(chosen) && !options.video_only) {
        const audio = formats.filter((format) => hasAudio(format) && !hasVideo(format)).sort((a, b) => (b.audio_bitrate_kbps || 0) - (a.audio_bitrate_kbps || 0));
        if (!audio.length) return unknown("Audio size is unavailable for the combined output.");
        result = sum([result, formatSize(audio[0], live ? duration : probe.duration_seconds, live)]);
        // An incomplete track sum must never be presented as the whole file.
        if (result.unknownCount) return unknown("One of the video/audio track sizes is unavailable.");
        result.approximate = true;
      }
      if (clipped && valid(result.bytes) && probe.duration_seconds > 0 && duration != null) result = known(result.bytes * duration / probe.duration_seconds, true);
      if (live) result.approximate = true;
      if (options.video_quality || options.video_codec || options.video_container || options.video_only) result.approximate = true;
      return result;
    }
    if (!options.video_quality && !options.video_codec && !options.video_only && valid(probe.size_bytes)) return known(probe.size_bytes, !!probe.size_is_approx);
    return unknown();
  }

  function progress(event = {}, completedBytes = null) {
    if (valid(completedBytes)) return bytesText(completedBytes);
    const downloaded = valid(event.bytes_downloaded) ? bytesText(event.bytes_downloaded) : null;
    const total = valid(event.bytes_total) ? bytesText(event.bytes_total) : null;
    if (downloaded && total) return `${downloaded} / ${total}`;
    if (downloaded) return `${downloaded} downloaded · total unknown`;
    return total ? `${total} total` : "Size unknown";
  }

  window.NeonSize = { estimate, itemSize, sum, label, bytesText, progress };
})();
