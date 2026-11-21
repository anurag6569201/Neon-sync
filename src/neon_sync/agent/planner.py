"""Natural-language media intent planner with Gemini, OpenAI/Ollama & zero-key deterministic fallback."""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field

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


class AgentRequest(BaseModel):
    prompt: str = Field(min_length=2, max_length=2000)
    url: str | None = Field(default=None, max_length=4096)


class AgentPlan(BaseModel):
    action: Literal["download", "probe", "summarize", "download_and_summarize"] = "download"
    url: str
    explanation: str
    provider_used: str = "local-planner"
    download_params: dict[str, Any] = Field(default_factory=dict)
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


def parse_intent_deterministic(prompt: str, url: str) -> AgentPlan:
    """Zero-latency, zero-cost natural-language intent parser for common media workflows."""
    low = prompt.lower()
    params: dict[str, Any] = {"mode": "auto"}
    notes: list[str] = []

    wants_summary = any(
        w in low
        for w in ("summarize", "summary", "tldr", "tl;dr", "key points", "bullet", "explain", "what does", "notes", "transcript")
    )
    wants_download = any(
        w in low
        for w in ("download", "save", "clip", "trim", "cut", "extract", "convert", "mp3", "mp4", "mkv", "flac", "wav", "webp", "jpg", "png", "audio", "video", "1080", "720", "480", "4k")
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

    # Detect clip range (e.g. "clip from 0:05 to 0:15" or "5s to 15s")
    cm = _CLIP_RANGE_RE.search(prompt)
    if cm:
        s_val = _parse_time_token(cm.group(1))
        e_val = _parse_time_token(cm.group(2))
        if s_val is not None:
            params["clip_start_seconds"] = s_val
            if e_val is not None and e_val > s_val:
                params["clip_end_seconds"] = e_val
            notes.append(f"clip {int(s_val)}s–{int(e_val) if e_val else 'end'}s")

    # Detect subtitles / transcript request
    if any(k in low for k in ("subtitle", "caption", "transcript", "srt", "vtt")):
        params["extract_subtitles"] = True
        params["generate_transcript"] = True
        if "embed" in low:
            params["embed_subtitles"] = True
        notes.append("subtitles + transcript")

    # Decide action
    if wants_summary and wants_download:
        action: Literal["download", "probe", "summarize", "download_and_summarize"] = "download_and_summarize"
        params["extract_subtitles"] = True
        params["generate_transcript"] = True
    elif wants_summary and not wants_download:
        action = "summarize"
    elif any(k in low for k in ("inspect", "probe", "what formats", "check quality", "metadata")):
        action = "probe"
    else:
        action = "download"

    summary_str = ", ".join(notes) if notes else "smart best-quality defaults"
    explanation = f"Planned `{action}` using {summary_str}."
    return AgentPlan(
        action=action,
        url=url,
        explanation=explanation,
        provider_used="local-planner",
        download_params=params,
        summarize_question=prompt if wants_summary else None,
    )


_SYSTEM_PLANNER_PROMPT = """You are Neon Sync Agent, a precision media extraction planner.
Given a user request and URL, return ONLY valid JSON matching this schema:
{
  "action": "download" | "probe" | "summarize" | "download_and_summarize",
  "explanation": "Short 1-sentence plan description",
  "summarize_question": "Optional question if summarizing",
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
  }
}
Return raw JSON only with no markdown fences."""


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
        parsed = json.loads(text)
        return AgentPlan(
            action=parsed.get("action", "download"),
            url=url,
            explanation=parsed.get("explanation", "Planned via Gemini."),
            provider_used=f"gemini:{model}",
            download_params=parsed.get("download_params") or {},
            summarize_question=parsed.get("summarize_question"),
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("Gemini planner fallback to deterministic: %s", exc)
        return None


async def summarize_text_with_llm(
    text: str,
    question: str,
    *,
    settings: Settings,
    byok_gemini_key: str | None = None,
) -> tuple[str, str]:
    """Summarize transcript/metadata via Gemini if key is configured, else clean local extractive brief."""
    api_key = byok_gemini_key or settings.gemini_api_key
    clipped = text[:24000]
    if api_key:
        endpoint = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{settings.gemini_model}:generateContent?key={api_key}"
        )
        body = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {
                            "text": (
                                "You are Neon Sync Agent. Answer the user's request concisely using "
                                "bullet points based strictly on this media transcript/metadata:\n\n"
                                f"User request: {question}\n\n"
                                f"Content:\n{clipped}"
                            )
                        }
                    ],
                }
            ],
            "generationConfig": {"temperature": 0.2},
        }
        try:
            async with httpx.AsyncClient(timeout=25.0) as client:
                resp = await client.post(endpoint, json=body)
                resp.raise_for_status()
                answer = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
                return answer.strip(), f"gemini:{settings.gemini_model}"
        except Exception as exc:  # noqa: BLE001
            log.warning("Gemini summarize failed, falling back to local brief: %s", exc)

    # Clean local extractive summary when no external LLM key is configured
    lines = [ln.strip() for ln in clipped.splitlines() if ln.strip()]
    preview_lines = lines[:8]
    summary = (
        "Local Transcript Brief (add a Gemini API key in Pro/BYOK for deep LLM synthesis):\n"
        + "\n".join(f"• {ln}" for ln in preview_lines)
    )
    return summary, "local-extractive"


async def plan_agent_action(
    request: AgentRequest,
    *,
    settings: Settings,
    byok_gemini_key: str | None = None,
) -> AgentPlan:
    url = extract_url_from_prompt(request.prompt, request.url)
    if not url:
        raise ValueError("Include a valid http(s) URL in your prompt or link field.")

    api_key = byok_gemini_key or settings.gemini_api_key
    if api_key:
        llm_plan = await _plan_with_gemini(request.prompt, url, api_key, settings.gemini_model)
        if llm_plan is not None:
            return llm_plan

    return parse_intent_deterministic(request.prompt, url)
