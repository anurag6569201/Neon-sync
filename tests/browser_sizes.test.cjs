const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const context = vm.createContext({ window: {} });
for (const file of ["client.js", "sizes.js"]) {
  vm.runInContext(fs.readFileSync(path.join(__dirname, "../src/neon_sync/api/static", file), "utf8"), context);
}
const size = context.window.NeonSize;
const MB = 1024 * 1024;
const video = (bytes, height = 1080, muxed = false) => ({ format_id: `v${height}`, size_bytes: bytes, height, has_video: true, has_audio: muxed, video_codec: "avc1", container: "mp4" });
const audio = (bytes) => ({ format_id: "audio", size_bytes: bytes, has_video: false, has_audio: true, audio_bitrate_kbps: 128, container: "m4a" });

test("zero-byte files are known; missing sizes are not invented", () => {
  assert.equal(size.label(size.estimate({ content_type: "file", size_bytes: 0 })), "0 B");
  assert.equal(size.label(size.estimate({ content_type: "file" })), "Size unavailable");
  assert.equal(size.label(size.estimate({ content_type: "file", size_bytes: MB, size_is_approx: true })), "~1.0 MB");
});

test("muxed video includes audio once; separate tracks are added", () => {
  let result = size.estimate({ content_type: "video", available_formats: [video(20 * MB, 1080, true), audio(3 * MB)] });
  assert.equal(result.bytes, 20 * MB);
  assert.equal(result.approximate, false);
  result = size.estimate({ content_type: "video", available_formats: [video(20 * MB), audio(3 * MB)] });
  assert.equal(result.bytes, 23 * MB);
  assert.equal(result.approximate, true);
  assert.equal(size.estimate({ content_type: "video", available_formats: [video(20 * MB), audio(null)] }).bytes, null);
});

test("quality and codec estimates respect the chosen constraints", () => {
  const probe = { content_type: "video", size_bytes: 100 * MB, available_formats: [video(100 * MB, 1080, true), video(30 * MB, 720, true)] };
  assert.equal(size.estimate(probe, { mode: "video", video_quality: "720" }).bytes, 30 * MB);
  assert.equal(size.estimate(probe, { mode: "video", video_quality: "720", video_codec: "av1" }).bytes, null);
  assert.equal(size.estimate(probe, { mode: "video", video_quality: "480" }).bytes, null);
});

test("audio encoding estimates use duration and the selected bitrate", () => {
  const probe = { content_type: "video", duration_seconds: 60, size_bytes: 99 * MB };
  assert.equal(size.estimate(probe, { mode: "audio", audio_format: "mp3", audio_bitrate: "128k" }).bytes, 960000);
  assert.equal(size.estimate(probe, { mode: "audio", audio_format: "opus", audio_bitrate: "192k", clip_start_seconds: 10, clip_end_seconds: 20 }).bytes, 240000);
  assert.equal(size.estimate(probe, { mode: "audio", audio_format: "flac", audio_bitrate: "320k" }).bytes, null);
  assert.equal(size.estimate(probe, { mode: "audio", audio_format: "mp3" }).bytes, null);
});

test("audio extraction never treats the entire muxed video as audio bytes", () => {
  const probe = { content_type: "video", duration_seconds: 60, size_bytes: 99 * MB, available_formats: [{ ...video(99 * MB, 1080, true), audio_bitrate_kbps: 128 }] };
  assert.equal(size.estimate(probe, { mode: "audio", audio_format: "best" }).bytes, 960000);
});

test("clip estimates scale native video and audio sizes", () => {
  let probe = { content_type: "audio", duration_seconds: 100, available_formats: [audio(10 * MB)] };
  assert.equal(size.estimate(probe, { mode: "audio", clip_start_seconds: 20, clip_end_seconds: 40 }).bytes, 2 * MB);
  probe = { content_type: "video", duration_seconds: 100, available_formats: [video(100 * MB, 1080, true)] };
  assert.equal(size.estimate(probe, { mode: "video", clip_start_seconds: 20, clip_end_seconds: 40 }).bytes, 20 * MB);
});

test("partial collection subtotals are never displayed as the complete size", () => {
  const probe = { content_type: "playlist", metadata: { item_count_total: 4 }, items: [{ index: 1, size_bytes: MB }, { index: 2, size_bytes: null }, { index: 3, size_bytes: 2 * MB, size_is_approx: true }] };
  const total = size.estimate(probe);
  assert.equal(size.label(total), "Size unavailable");
  assert.equal(total.bytes, 3 * MB);
  assert.equal(size.label({ ...total, pending: true }), "Calculating…");
  const selected = size.estimate(probe, { playlist_items: "1,3" });
  assert.equal(selected.bytes, 3 * MB);
  assert.equal(selected.unknownCount, 0);
  assert.equal(size.estimate(probe, { playlist_items: "68" }).bytes, null);
});

test("playlist audio estimates sum the selected durations", () => {
  const probe = { content_type: "playlist", items: [{ index: 1, duration_seconds: 60 }, { index: 68, duration_seconds: 120 }] };
  const result = size.estimate(probe, { playlist_items: "1,68", mode: "audio", audio_format: "mp3", audio_bitrate: "128k" });
  assert.equal(result.bytes, 2880000);
  assert.equal(result.approximate, true);
});

test("inspected playlist metadata supplies quality-specific sizes", () => {
  const probe = { content_type: "playlist", items: [{ index: 68, _sizeProbe: { content_type: "video", available_formats: [video(30 * MB, 720, true), video(100 * MB, 1080, true)] } }] };
  assert.equal(size.estimate(probe, { playlist_items: "68", mode: "video", video_quality: "720" }).bytes, 30 * MB);
});

test("live sizes require a recording duration and usable bitrate", () => {
  const probe = { content_type: "livestream", is_live: true, available_formats: [{ ...video(999 * MB, 1080, true), video_bitrate_kbps: 1000, audio_bitrate_kbps: 128 }] };
  assert.equal(size.estimate(probe).bytes, null);
  assert.equal(size.estimate(probe, { livestream_max_duration: 60 }).bytes, 8460000);
});

test("generated outputs remain unknown and compression targets are labeled", () => {
  const probe = { content_type: "image", size_bytes: MB };
  assert.equal(size.estimate(probe, { mode: "image", image_format: "original" }).bytes, MB);
  assert.equal(size.estimate(probe, { mode: "image", image_format: "webp" }).bytes, null);
  assert.equal(size.estimate(probe, { video_output_action: "gif" }).bytes, null);
  assert.equal(size.label(size.estimate(probe, { target_size_mb: 8 })), "Target 8.0 MB");
});

test("progress shows transferred bytes and the completed file size", () => {
  assert.equal(size.progress({ bytes_downloaded: MB, bytes_total: 4 * MB }), "1.0 MB / 4.0 MB");
  assert.equal(size.progress({ bytes_downloaded: MB }), "1.0 MB downloaded · total unknown");
  assert.equal(size.progress({}, 0), "0 B");
  assert.equal(size.progress({}, 4 * MB), "4.0 MB");
});
