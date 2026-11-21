"""Contract tests verifying Pydantic models against JSON Schemas in research/schemas/."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema

from neon_sync.models import (
    ContentType,
    DownloadInfo,
    DownloadStatus,
    EngineCapability,
    EngineManifest,
    EngineProvenance,
    FormatInfo,
    InvocationMode,
    NormalizedMediaOutput,
    ProtocolType,
    SourceInfo,
    SupportedDomains,
)

SCHEMAS_DIR = Path(__file__).resolve().parent.parent / "research" / "schemas"


def test_example_output_validates_against_schema_and_model():
    schema = json.loads((SCHEMAS_DIR / "normalized-media-output.schema.json").read_text())
    example = json.loads((SCHEMAS_DIR / "example-normalized-output.json").read_text())

    jsonschema.validate(example, schema)
    model = NormalizedMediaOutput.model_validate(example)
    roundtrip = json.loads(model.model_dump_json())
    jsonschema.validate(roundtrip, schema)
    assert model.source.domain == "youtube.com"
    assert model.content_type == ContentType.VIDEO
    assert len(model.available_formats) == 2


def test_minimal_normalized_output_validates_against_schema():
    schema = json.loads((SCHEMAS_DIR / "normalized-media-output.schema.json").read_text())

    out = NormalizedMediaOutput(
        job_id="job_test_001",
        original_url="https://example.com/file.pdf",
        url="https://example.com/file.pdf",
        source=SourceInfo(domain="example.com"),
        content_type=ContentType.DOCUMENT,
        title="Sample Report",
        available_formats=[
            FormatInfo(
                format_id="direct",
                container="pdf",
                mime_type="application/pdf",
                protocol=ProtocolType.HTTPS,
                size_bytes=12345,
                direct_media_url="https://example.com/file.pdf",
            )
        ],
        download=DownloadInfo(status=DownloadStatus.NOT_REQUESTED),
        engine=EngineProvenance(
            engine_used="direct-http",
            engine_version="0.1.0",
            fallback_chain=["direct-http"],
        ),
    )

    payload = json.loads(out.model_dump_json())
    jsonschema.validate(payload, schema)


def test_engine_manifest_validates_against_schema():
    schema = json.loads((SCHEMAS_DIR / "engine-manifest.schema.json").read_text())

    manifest = EngineManifest(
        id="yt-dlp",
        name="yt-dlp",
        version="2026.10.26",
        tier=2,
        supported_content_types=["video", "audio", "playlist", "hls", "dash"],
        supported_domains=SupportedDomains(mode="wildcard", site_count=1800),
        protocols=["http", "https", "hls", "dash"],
        capabilities=[
            EngineCapability.EXTRACT_METADATA,
            EngineCapability.ENUMERATE_FORMATS,
            EngineCapability.DOWNLOAD_MEDIA,
        ],
        priority=900,
        license="Unlicense",
        invocation_mode=InvocationMode.SUBPROCESS,
    )

    payload = json.loads(manifest.model_dump_json())
    jsonschema.validate(payload, schema)
