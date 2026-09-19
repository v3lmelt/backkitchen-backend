from datetime import datetime
import math
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


SampleFormat = Literal["pcm_s16", "pcm_s24", "pcm_s32", "pcm_f32"]
AudioAnalysisStatus = Literal["pending", "processing", "ready", "failed", "not_applicable"]

ALLOWED_SAMPLE_RATES = {44100, 48000, 88200, 96000, 176400, 192000}


class AudioSpec(BaseModel):
    enabled: bool = True
    allowed_sample_rates_hz: list[int] = Field(default_factory=list)
    allowed_sample_formats: list[SampleFormat] = Field(default_factory=list)
    allowed_containers: list[Literal["wav"]] = Field(default_factory=lambda: ["wav"])

    @field_validator("allowed_sample_rates_hz")
    @classmethod
    def validate_sample_rates(cls, values: list[int]) -> list[int]:
        values = list(dict.fromkeys(values))
        unsupported = sorted(set(values) - ALLOWED_SAMPLE_RATES)
        if unsupported:
            raise ValueError(f"Unsupported sample rates: {unsupported}")
        return values

    @field_validator("allowed_sample_formats")
    @classmethod
    def dedupe_sample_formats(cls, values: list[SampleFormat]) -> list[SampleFormat]:
        return list(dict.fromkeys(values))

    @model_validator(mode="after")
    def validate_enabled_requirements(self) -> "AudioSpec":
        if self.allowed_containers != ["wav"]:
            raise ValueError("Enabled audio specifications require the WAV container.")
        if self.enabled and not self.allowed_sample_rates_hz:
            raise ValueError("At least one sample rate is required when the specification is enabled.")
        if self.enabled and not self.allowed_sample_formats:
            raise ValueError("At least one sample format is required when the specification is enabled.")
        return self


# Historical migration/clients may still import this name.
PremasterSpec = AudioSpec


class AudioSpecs(BaseModel):
    source: AudioSpec | None = None
    master: AudioSpec | None = None


class AudioSpecDifference(BaseModel):
    field: Literal["container", "sample_rate_hz", "sample_format"]
    actual: str | int | None
    allowed: list[str | int]


class AudioSpecCheck(BaseModel):
    status: Literal["match", "mismatch", "unknown", "disabled", "not_applicable"]
    differences: list[AudioSpecDifference] = Field(default_factory=list)


class AudioAnalysis(BaseModel):
    analyzer_version: int = 2
    ffmpeg_version: str | None = None
    integrated_status: Literal["valid", "silence", "below_gate", "too_short", "unavailable"] = "unavailable"
    lra_status: Literal["valid", "short_programme", "silence", "below_gate", "too_short", "unavailable"] = "unavailable"
    peak_status: Literal["valid", "silence", "unavailable"] = "unavailable"
    container: str | None = None
    codec: str | None = None
    sample_format: str | None = None
    bit_depth: int | None = None
    sample_rate_hz: int | None = None
    channels: int | None = None
    channel_layout: str | None = None
    duration_seconds: float | None = None
    bitrate_bps: int | None = None
    sample_peak_dbfs: float | None = None
    sample_peak_dbfs_by_channel: list[float | None] = Field(default_factory=list)
    true_peak_dbtp: float | None = None
    true_peak_dbtp_by_channel: list[float | None] = Field(default_factory=list)
    dc_offset: float | None = None
    dc_offset_by_channel: list[float | None] = Field(default_factory=list)
    integrated_lufs: float | None = None
    loudness_range_lu: float | None = None

    @field_validator("duration_seconds", "sample_peak_dbfs", "true_peak_dbtp", "dc_offset", "integrated_lufs", "loudness_range_lu", mode="before")
    @classmethod
    def finite_metric(cls, value):
        return value if value is None or math.isfinite(float(value)) else None

    @field_validator("sample_peak_dbfs_by_channel", "true_peak_dbtp_by_channel", "dc_offset_by_channel", mode="before")
    @classmethod
    def finite_channels(cls, values):
        return [cls.finite_metric(value) for value in values]


class AudioAnalysisRead(BaseModel):
    status: AudioAnalysisStatus
    result: AudioAnalysis | None = None
    error: str | None = None
    attempts: int = 0
    analyzed_at: datetime | None = None
