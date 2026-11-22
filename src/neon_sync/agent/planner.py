"""Neon Sync AI Agent Planner: Google Gemini 2.5, Cloudflare Workers AI & deterministic fallback."""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field

from neon_sync.cloud import run_cloudflare_workers_ai
from neon_sync.config import Settings

log = logging.getLogger("neon_sync.agent.planner")

_URL_RE = re.compile(r"https?://[^\s<>\"'\)\]]+", re.IGNORECASE)
_CLIP_RANGE_RE = re.compile(
    r"(?:from\s+)?(\d{1,2}:\d{2}(?::\d{2})?|\d+\s*(?:s|sec|secs|seconds)?)\s*(?:to|-|–|until)\s*(\d{1,2}:\d{2}(?::\d{2})?|\d+\s*(?:s|sec|secs|seconds)?)",
    re.IGNORECASE,
)
_QUALITY_RE = re.compile(r"\b(2160p?|4k|1440p?|2k|1080p?|720p?|480p?|360p?)\b", re.IGNORECASE)
_BITRATE_RE = re.compile(r"\b(320|256|192|128|96)\s*k(?:bps)?\b", re.IGNORECASE)
_WIDTH_RE = re.compile(r"\b(\d{3,4})\s*px\b", re.IGNORECASE)
_FIRST_N_RE = re.compile(r"\b(?:first|top)\s+(\d{1,3})\b", re.IGNORECASE)

AgentAction = Literal[
    "download",
    "multi_step",
    "stream",
    "stream_and_summarize",
    "probe",
    "summarize",
    "download_and_summarize",
]


class AgentRequest(BaseModel):
    prompt: str = Field(min_length=2, max_length=2000)
    url: str | None = Field(default=None, max_length=4096)


class AgentPlan(BaseModel):
    action: AgentAction = "download"
    url: str
    explanation: str
    provider_used: str = "neon-sync-core"
    download_params: dict[str, Any] = Field(default_factory=dict)
    steps: list[dict[str, Any]] = Field(default_factory=list)
    stream_mode: Literal["video", "audio"] | None = None
    output_style: Literal["brief", "chapters", "social_kit", "qa"] = "brief"
    summarize_question: str | None = None


def _parse_time_token(tok: str) -> float | None:
    clean = re.sub(r"\s*(?:seconds|secs|sec|s)$", "", tok.strip(), flags=re.IGNORECASE)
    if clean.isdigit():
        return float(clean)
    parts = clean.split(":")
    if all(p.isdigit() for p in parts):
        nums = [int(p) for p in parts]
        if len(nums) == 2:
            return float(nums[0] * 60 + nums[1])
        if len(nums) == 3:
            return float(nums[0] * 3600 + nums[1] * 60 + nums[2])
    return None


def extract_url_from_prompt(prompt: str, explicit_url: str | None = None) -> str | None:
    if explicit_url and explicit_url.strip():
        return explicit_url.strip()
    m = _URL_RE.search(prompt)
    return m.group(0).rstrip(".,;:") if m else None


def _detect_output_style(low: str) -> Literal["brief", "chapters", "social_kit", "qa"]:
    if any(k in low for k in ("reel kit", "social kit", "tiktok", "shorts", "hook", "tweet", "post copy", "viral")):
        return "social_kit"
    if any(k in low for k in ("chapter", "timestamp", "outline", "sections")):
        return "chapters"
    if any(k in low for k in ("?", "what ", "why ", "how ", "who ", "does ")):
        return "qa"
    return "brief"


def parse_intent_deterministic(prompt: str, url: str) -> AgentPlan:
    """Zero-latency, zero-cost multi-step natural-language intent planner for media workflows."""
    low = prompt.lower()
    params: dict[str, Any] = {"mode": "auto"}
    notes: list[str] = []
    output_style = _detect_output_style(low)

    wants_summary = any(
        w in low
        for w in (
            "summarize",
            "summary",
            "tldr",
            "tl;dr",
            "key points",
            "takeaways",
            "bullet",
            "explain",
            "what does",
            "show notes",
            "notes",
            "brief",
            "hooks",
            "caption copy",
            "chapters",
        )
    )
    wants_stream = any(
        w in low
        for w in ("watch live", "play live", "stream live", "watch ", "play ", "vlc", "listen live")
    )
    wants_kit = any(w in low for w in ("reel kit", "creator kit", "podcast kit", "both video and audio", "video and mp3"))
    wants_download = any(
        w in low
        for w in (
            "download",
            "save",
            "clip",
            "trim",
            "cut",
            "extract",
            "convert",
            "mp3",
            "mp4",
            "mkv",
            "flac",
            "wav",
            "webp",
            "jpg",
            "png",
            "audio",
            "video",
            "1080",
            "720",
            "480",
            "4k",
            "kit",
        )
    )

    # Detect clip range (e.g. "clip from 0:05 to 0:15" or "5s to 15s")
    clip_start: float | None = None
    clip_end: float | None = None
    cm = _CLIP_RANGE_RE.search(prompt)
    if cm:
        s_val = _parse_time_token(cm.group(1))
        e_val = _parse_time_token(cm.group(2))
        if s_val is not None:
            clip_start = s_val
            if e_val is not None and e_val > s_val:
                clip_end = e_val

    # Multi-step Creator / Reel / Podcast Kits!
    if wants_kit:
        if "podcast" in low:
            steps = [
                {
                    "label": "Mastered 320k MP3 Audio",
                    "params": {
                        "mode": "audio",
                        "audio_format": "mp3",
                        "audio_bitrate": "320k",
                        "normalize_audio": True,
                        "embed_metadata": True,
                        "extract_subtitles": True,
                        "generate_transcript": True,
                    },
                }
            ]
            return AgentPlan(
                action="multi_step",
                url=url,
                explanation="Planned Podcast Kit: mastered 320k MP3 audio + transcript + AI show notes.",
                provider_used="neon-sync-core",
                steps=steps,
                output_style="brief",
                summarize_question=f"Write structured podcast show notes and key takeaways for: {prompt}",
            )
        video_step: dict[str, Any] = {
            "mode": "video",
            "video_quality": "1080",
            "video_container": "mp4",
            "video_codec": "h264",
            "remux_faststart": True,
        }
        if clip_start is not None:
            video_step["clip_start_seconds"] = clip_start
            if clip_end is not None:
                video_step["clip_end_seconds"] = clip_end
        steps = [
            {"label": "1080p H.264 Video Master", "params": video_step},
            {
                "label": "320k MP3 Audio Track",
                "params": {
                    "mode": "audio",
                    "audio_format": "mp3",
                    "audio_bitrate": "320k",
                    "normalize_audio": True,
                },
            },
        ]
        return AgentPlan(
            action="multi_step",
            url=url,
            explanation="Planned multi-step Creator Kit: 1080p MP4 video + mastered 320k MP3 audio + AI brief.",
            provider_used="neon-sync-core",
            steps=steps,
            output_style=output_style,
            summarize_question=prompt if (wants_summary or "reel" in low) else None,
        )

    # Detect audio-only intent
    audio_fmts = ("mp3", "flac", "wav", "opus", "m4a")
    matched_af = next((f for f in audio_fmts if re.search(rf"\b{f}\b", low)), None)
    if matched_af or any(k in low for k in ("audio only", "just the audio", "extract audio", "as audio", "podcast audio")):
        params["mode"] = "audio"
        params["audio_format"] = matched_af or "mp3"
        notes.append(f"Audio ({params['audio_format'].upper()})")
        bm = _BITRATE_RE.search(low)
        if bm:
            params["audio_bitrate"] = f"{bm.group(1)}k"
            notes.append(params["audio_bitrate"])
        if any(k in low for k in ("normalize", "loudnorm", "even volume", "level volume")):
            params["normalize_audio"] = True
            notes.append("loudness normalized")

    # Detect image/gallery intent
    img_fmts = ("webp", "avif", "png", "jpg", "jpeg")
    matched_if = next((f for f in img_fmts if re.search(rf"\b{f}\b", low)), None)
    if matched_if or any(k in low for k in ("gallery", "album", "photos", "images", "pictures")):
        params["mode"] = "image"
        if matched_if:
            params["image_format"] = "jpg" if matched_if == "jpeg" else matched_if
            notes.append(f"Image -> {params['image_format'].upper()}")
        wm = _WIDTH_RE.search(low)
        if wm:
            params["image_max_width"] = max(64, min(16384, int(wm.group(1))))
            notes.append(f"max {params['image_max_width']}px")
        fn = _FIRST_N_RE.search(low)
        if fn:
            n = max(1, min(200, int(fn.group(1))))
            params["gallery_range_start"] = 1
            params["gallery_range_end"] = n
            notes.append(f"first {n} items")

    # Detect video quality / codec / container
    if params["mode"] in {"auto", "video"}:
        qm = _QUALITY_RE.search(low)
        if qm:
            qraw = qm.group(1).lower().rstrip("p")
            qmap = {"4k": "2160", "2k": "1440"}
            params["mode"] = "video"
            params["video_quality"] = qmap.get(qraw, qraw)
            notes.append(f"{params['video_quality']}p")
        for container in ("mp4", "mkv", "webm", "mov"):
            if re.search(rf"\b{container}\b", low):
                params["mode"] = "video"
                params["video_container"] = container
                notes.append(container.upper())
                break
        for codec, alias in (("h264", r"h\.?264|avc"), ("hevc", r"hevc|h\.?265"), ("av1", r"\bav1\b"), ("vp9", r"\bvp9\b")):
            if re.search(alias, low):
                params["video_codec"] = codec
                notes.append(codec.upper())
                break

    if clip_start is not None:
        params["clip_start_seconds"] = clip_start
        if clip_end is not None:
            params["clip_end_seconds"] = clip_end
        notes.append(f"clip {int(clip_start)}s–{int(clip_end) if clip_end else 'end'}s")

    # Detect subtitles / transcript request
    if any(k in low for k in ("subtitle", "caption", "transcript", "srt", "vtt")):
        params["extract_subtitles"] = True
        params["generate_transcript"] = True
        if "embed" in low:
            params["embed_subtitles"] = True
        notes.append("subtitles + transcript")

    # Decide action
    stream_mode: Literal["video", "audio"] | None = None
    if wants_stream and not any(k in low for k in ("download", "save", "clip", "trim", "convert")):
        stream_mode = "audio" if ("listen" in low or "audio" in low) else "video"
        action: AgentAction = "stream_and_summarize" if wants_summary else "stream"
    elif wants_summary and wants_download:
        action = "download_and_summarize"
        params["extract_subtitles"] = True
        params["generate_transcript"] = True
    elif wants_summary and not wants_download:
        action = "summarize"
    elif any(k in low for k in ("inspect", "probe", "what formats", "check quality", "metadata")):
        action = "probe"
    else:
        action = "download"

    summary_str = ", ".join(notes) if notes else (
        f"live {stream_mode} network stream" if stream_mode else "smart best-quality defaults"
    )
    explanation = f"Planned `{action}` using {summary_str}."
    return AgentPlan(
        action=action,
        url=url,
        explanation=explanation,
        provider_used="neon-sync-core",
        download_params=params,
        stream_mode=stream_mode,
        output_style=output_style,
        summarize_question=prompt if wants_summary else None,
    )


_SYSTEM_PLANNER_PROMPT = """You are Neon Sync AI Agent, an autonomous media workflow planner.
Given a user request and URL, return ONLY valid JSON matching this schema:
{
  "action": "download" | "multi_step" | "stream" | "stream_and_summarize" | "probe" | "summarize" | "download_and_summarize",
  "explanation": "Concise 1-sentence plan description",
  "stream_mode": null | "video" | "audio",
  "output_style": "brief" | "chapters" | "social_kit" | "qa",
  "summarize_question": "Optional question/focus if summarizing",
  "download_params": {
    "mode": "auto" | "video" | "audio" | "image" | "direct",
    "video_quality": "best" | "2160" | "1440" | "1080" | "720" | "480" | "360" | "worst",
    "video_container": "auto" | "mp4" | "mkv" | "webm" | "mov",
    "video_codec": "auto" | "h264" | "hevc" | "vp9" | "av1",
    "audio_format": null | "best" | "mp3" | "m4a" | "opus" | "flac" | "wav",
    "audio_bitrate": "auto" | "320k" | "256k" | "192k" | "128k" | "96k",
    "normalize_audio": false,
    "image_format": "original" | "jpg" | "png" | "webp" | "avif",
    "image_max_width": null | 3840 | 1920 | 1280 | 800,
    "extract_subtitles": false,
    "generate_transcript": false,
    "clip_start_seconds": null | number,
    "clip_end_seconds": null | number
  },
  "steps": [
    { "label": "Short step label", "params": { "...download_params subset..." } }
  ]
}
Use "stream" or "stream_and_summarize" when the user wants to watch/play live without downloading.
Use "multi_step" with 2+ steps when the user asks for a kit or multiple distinct outputs (e.g., video clip AND mp3 audio).
Return raw JSON only with no markdown fences."""


def _parse_llm_plan_json(raw_text: str, *, url: str, provider: str) -> AgentPlan | None:
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.DOTALL).strip()
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if not m:
            return None
        parsed = json.loads(m.group(0))
    return AgentPlan(
        action=parsed.get("action", "download"),
        url=url,
        explanation=parsed.get("explanation", "Planned via Neon Sync AI."),
        provider_used=provider,
        download_params=parsed.get("download_params") or {},
        steps=parsed.get("steps") or [],
        stream_mode=parsed.get("stream_mode"),
        output_style=parsed.get("output_style", "brief"),
        summarize_question=parsed.get("summarize_question"),
    )


async def _plan_with_gemini(prompt: str, url: str, api_key: str, model: str) -> AgentPlan | None:
    endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    body = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": f"{_SYSTEM_PLANNER_PROMPT}\n\nURL: {url}\nUser Request: {prompt}"}],
            }
        ],
        "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"},
    }
    try:
        async with httpx.AsyncClient(timeout=18.0) as client:
            resp = await client.post(endpoint, json=body)
            resp.raise_for_status()
            data = resp.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        return _parse_llm_plan_json(text, url=url, provider=f"gemini:{model}")
    except Exception as exc:  # noqa: BLE001
        log.warning("Gemini planner fallback: %s", exc)
        return None


def _local_extractive_brief(text: str, *, output_style: str) -> str:
    """High-signal local extractive brief when no cloud LLM API key is configured."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    meta_lines = [ln for ln in lines[:6] if ":" in ln and len(ln) < 160]
    body_lines = [ln for ln in lines if ln not in meta_lines and len(ln) > 20]

    # Pick diverse representative sentences across beginning, middle, and end
    picked: list[str] = []
    if body_lines:
        step = max(1, len(body_lines) // 6)
        for idx in range(0, len(body_lines), step):
            sent = body_lines[idx][:220]
            if sent not in picked:
                picked.append(sent)
            if len(picked) >= 6:
                break

    header = {
        "social_kit": "Neon Sync AI Content Kit (add Gemini or Cloudflare AI key for creative copy):",
        "chapters": "Neon Sync Structured Outline:",
        "qa": "Neon Sync Transcript Highlights:",
    }.get(output_style, "Neon Sync Executive Brief:")

    out = [header]
    if meta_lines:
        out.extend(f"• {ln}" for ln in meta_lines[:4])
    if picked:
        out.append("")
        out.append("Key Highlights:")
        out.extend(f"• {ln}" for ln in picked)
    return "\n".join(out)


async def summarize_text_with_llm(
    text: str,
    question: str,
    *,
    settings: Settings,
    byok_gemini_key: str | None = None,
    output_style: str = "brief",
) -> tuple[str, str]:
    """Synthesize media transcript/metadata via Google Gemini 2.5 -> Cloudflare Workers AI -> local brief."""
    clipped = text[:28000]
    style_instructions = {
        "social_kit": (
            "Produce: (1) 3 punchy short-form hooks, (2) a concise caption with 4 relevant hashtags, "
            "and (3) the best 15-30s clip angle based strictly on this content."
        ),
        "chapters": "Produce a clean timestamped/topic chapter breakdown with 1-sentence takeaways per section.",
        "qa": "Answer the user's question directly and accurately using concise bullet points grounded in the transcript.",
    }.get(
        output_style,
        "Produce a crisp executive summary with 4–6 high-signal bullet takeaways grounded strictly in the transcript/metadata.",
    )

    system_msg = f"You are Neon Sync AI. {style_instructions} Never mention underlying extraction tools."
    user_msg = f"User request: {question}\n\nMedia Content & Transcript:\n{clipped}"

    # 1. Google Gemini 2.5 (Primary)
    api_key = byok_gemini_key or settings.gemini_api_key
    if api_key:
        endpoint = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{settings.gemini_model}:generateContent?key={api_key}"
        )
        body = {
            "contents": [{"role": "user", "parts": [{"text": f"{system_msg}\n\n{user_msg}"}]}],
            "generationConfig": {"temperature": 0.25},
        }
        try:
            async with httpx.AsyncClient(timeout=25.0) as client:
                resp = await client.post(endpoint, json=body)
                resp.raise_for_status()
                answer = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
                return answer.strip(), f"gemini:{settings.gemini_model}"
        except Exception as exc:  # noqa: BLE001
            log.warning("Gemini summarize failed, checking Cloudflare Workers AI: %s", exc)

    # 2. Cloudflare Workers AI Edge Fallback
    cf_answer = await run_cloudflare_workers_ai(
        system_prompt=system_msg,
        user_prompt=user_msg,
        settings=settings,
    )
    if cf_answer:
        return cf_answer, "cloudflare-ai"

    # 3. Clean local extractive brief
    return _local_extractive_brief(clipped, output_style=output_style), "neon-sync-core"


async def plan_agent_action(
    request: AgentRequest,
    *,
    settings: Settings,
    byok_gemini_key: str | None = None,
) -> AgentPlan:
    url = extract_url_from_prompt(request.prompt, request.url)
    if not url:
        raise ValueError("Include a valid http(s) URL in your prompt or link field.")

    # 1. Try Google Gemini 2.5 Flash structured planner
    api_key = byok_gemini_key or settings.gemini_api_key
    if api_key:
        llm_plan = await _plan_with_gemini(request.prompt, url, api_key, settings.gemini_model)
        if llm_plan is not None:
            return llm_plan

    # 2. Try Cloudflare Workers AI structured planner
    cf_raw = await run_cloudflare_workers_ai(
        system_prompt=_SYSTEM_PLANNER_PROMPT,
        user_prompt=f"URL: {url}\nUser Request: {request.prompt}",
        settings=settings,
        temperature=0.1,
    )
    if cf_raw:
        cf_plan = _parse_llm_plan_json(cf_raw, url=url, provider="cloudflare-ai")
        if cf_plan is not None:
            return cf_plan

    # 3. Deterministic zero-latency planner
    return parse_intent_deterministic(request.prompt, url)
