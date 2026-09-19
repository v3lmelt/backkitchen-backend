"""FFprobe format verification and queued FFmpeg technical analysis."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from threading import Lock

from sqlalchemy import select

from app.config import settings
from app.database import SessionLocal
from app.models.master_delivery import MasterDelivery
from app.models.track_source_version import TrackSourceVersion
from app.schemas.audio_analysis import AudioAnalysis, AudioAnalysisRead
from app.ws_manager import manager as track_manager

logger = logging.getLogger(__name__)
ANALYZER_VERSION = 2
MAX_ANALYSIS_ATTEMPTS = 3
STALE_PROCESSING_MINUTES = 30

_executor = ThreadPoolExecutor(max_workers=max(1, settings.AUDIO_ANALYSIS_CONCURRENCY))
_queued: set[tuple[str, int]] = set()
_prepared_paths: dict[tuple[str, int], Path] = {}
_queue_lock = Lock()
_event_loop: asyncio.AbstractEventLoop | None = None


def set_audio_analysis_event_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _event_loop
    _event_loop = loop


def _run(command: list[str], *, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout or settings.AUDIO_ANALYSIS_TIMEOUT_SECONDS,
        check=True,
    )


def _float(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _int(value: object) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _normalise_sample_format(codec: str | None, sample_fmt: str | None, bit_depth: int | None) -> str | None:
    codec = codec or ""
    if not codec.startswith("pcm_"):
        return None
    if codec.startswith("pcm_f32") or sample_fmt in {"flt", "fltp"}:
        return "pcm_f32"
    if codec.startswith("pcm_s16") or bit_depth == 16:
        return "pcm_s16"
    if codec.startswith("pcm_s24") or bit_depth == 24:
        return "pcm_s24"
    if codec.startswith("pcm_s32") or bit_depth == 32:
        return "pcm_s32"
    return sample_fmt or codec or None


def probe_audio(file_path: str | Path) -> AudioAnalysis:
    completed = _run(
        [
            settings.FFPROBE_PATH,
            "-v", "error",
            "-select_streams", "a:0",
            "-show_entries",
            "format=format_name,duration,bit_rate:stream=codec_name,sample_fmt,sample_rate,channels,channel_layout,bits_per_sample,bits_per_raw_sample,duration",
            "-of", "json",
            str(file_path),
        ],
        timeout=min(settings.AUDIO_ANALYSIS_TIMEOUT_SECONDS, 60),
    )
    payload = json.loads(completed.stdout)
    streams = payload.get("streams") or []
    if not streams:
        raise ValueError("No audio stream found.")
    stream = streams[0]
    fmt = payload.get("format") or {}
    bit_depth = _int(stream.get("bits_per_raw_sample")) or _int(stream.get("bits_per_sample"))
    codec = stream.get("codec_name")
    sample_fmt = stream.get("sample_fmt")
    format_names = str(fmt.get("format_name") or "").split(",")
    container = "wav" if "wav" in format_names else (format_names[0] or None)
    return AudioAnalysis(
        analyzer_version=ANALYZER_VERSION,
        container=container,
        codec=codec,
        sample_format=_normalise_sample_format(codec, sample_fmt, bit_depth),
        bit_depth=bit_depth,
        sample_rate_hz=_int(stream.get("sample_rate")),
        channels=_int(stream.get("channels")),
        channel_layout=stream.get("channel_layout"),
        duration_seconds=_float(stream.get("duration")) or _float(fmt.get("duration")),
        bitrate_bps=_int(fmt.get("bit_rate")),
    )


_ASTATS_LINE = re.compile(r"\] (?P<key>Channel|Overall|DC offset|Peak level dB):?\s*(?P<value>.*)$")
_META_LINE = re.compile(r"^lavfi\.r128\.(?P<key>[A-Za-z0-9_.]+)=(?P<value>[-+0-9.eE]+)$")


def _parse_astats(output: str) -> tuple[list[float | None], list[float | None], float | None, float | None]:
    channel_peaks: list[float | None] = []
    channel_offsets: list[float | None] = []
    overall_peak: float | None = None
    overall_offset: float | None = None
    scope: str | None = None
    current_channel = -1
    for line in output.splitlines():
        match = _ASTATS_LINE.search(line)
        if not match:
            continue
        key = match.group("key")
        value = match.group("value").strip()
        if key == "Channel":
            scope = "channel"
            current_channel = max(0, (_int(value) or 1) - 1)
            while len(channel_peaks) <= current_channel:
                channel_peaks.append(None)
                channel_offsets.append(None)
        elif key == "Overall":
            scope = "overall"
        elif key == "DC offset":
            if scope == "overall":
                overall_offset = _float(value)
            elif scope == "channel" and current_channel >= 0:
                channel_offsets[current_channel] = _float(value)
        elif key == "Peak level dB":
            if scope == "overall":
                overall_peak = _float(value)
            elif scope == "channel" and current_channel >= 0:
                channel_peaks[current_channel] = _float(value)
    return channel_peaks, channel_offsets, overall_peak, overall_offset


def _parse_ebur128(output: str) -> tuple[float | None, float | None]:
    latest: dict[str, float] = {}
    for line in output.splitlines():
        match = _META_LINE.match(line.strip())
        if match and (value := _float(match.group("value"))) is not None:
            latest[match.group("key")] = value
    return latest.get("I"), latest.get("LRA")


@lru_cache(maxsize=4)
def _ffmpeg_version(executable: str) -> str:
    return _run([executable, "-version"], timeout=15).stdout.splitlines()[0]


def _has_gated_data(output: str, key: str) -> bool:
    return any(
        match and match.group("key") == key
        and (value := _float(match.group("value"))) is not None and value >= -70
        for line in output.splitlines()
        if (match := _META_LINE.match(line.strip()))
    )


def analyze_audio(file_path: str | Path, *, probed: AudioAnalysis | None = None) -> AudioAnalysis:
    result = probed or probe_audio(file_path)
    result.ffmpeg_version = _ffmpeg_version(settings.FFMPEG_PATH)

    def scan(filters: str) -> subprocess.CompletedProcess[str]:
        return _run([
            settings.FFMPEG_PATH, "-hide_banner", "-nostats", "-i", str(file_path),
            "-map", "0:a:0", "-vn", "-sn", "-dn", "-af", filters, "-f", "null", "-",
        ])

    astats = scan("astats=reset=0:measure_perchannel=DC_offset+Peak_level:measure_overall=DC_offset+Peak_level+Number_of_samples")
    peaks, offsets, peak, offset = _parse_astats(astats.stderr)
    sample_counts = re.findall(r"Number of samples:\s*(\d+)", astats.stderr)
    if sample_counts and result.sample_rate_hz:
        result.duration_seconds = int(sample_counts[-1]) / result.sample_rate_hz
    result.sample_peak_dbfs_by_channel = peaks
    result.sample_peak_dbfs = peak
    result.dc_offset_by_channel = offsets
    result.dc_offset = offset
    result.peak_status = "valid" if peak is not None else "silence" if "-inf" in astats.stderr else "unavailable"

    # aresample drains its delay at EOF. astats reports the entire resampled stream,
    # including partial final frames, directly in dB (no rounded linear metadata).
    rate = max(192000, 4 * (result.sample_rate_hz or 48000))
    true_stats = scan(f"aresample={rate}:osf=dbl,astats=reset=0:measure_perchannel=Peak_level:measure_overall=Peak_level")
    true_channels, _, true_peak, _ = _parse_astats(true_stats.stderr)
    result.true_peak_dbtp_by_channel = true_channels
    result.true_peak_dbtp = true_peak

    meter = "ebur128=metadata=1:peak=none,ametadata=mode=print:file=-"
    integrated = scan(meter)
    duration = result.duration_seconds or 0
    if result.peak_status == "silence":
        result.integrated_status = "silence"
    elif duration < 0.4:
        result.integrated_status = "too_short"
    elif not _has_gated_data(integrated.stdout, "M"):
        result.integrated_status = "below_gate"
    else:
        result.integrated_lufs = _parse_ebur128(integrated.stdout)[0]
        result.integrated_status = "valid" if result.integrated_lufs is not None else "unavailable"

    if duration < 3:
        result.lra_status = "too_short"
    elif result.peak_status == "silence":
        result.lra_status = "silence"
    else:
        # EBU Tech 3342 file-based LRA: flush the short-term window separately.
        # This padding must never enter the Integrated or source-peak measurements.
        lra = scan("apad=pad_dur=1.5," + meter)
        if _has_gated_data(lra.stdout, "S"):
            result.loudness_range_lu = _parse_ebur128(lra.stdout)[1]
            result.lra_status = "short_programme" if duration < 60 else "valid"
        else:
            result.lra_status = "below_gate"
    return result


def analysis_read(record: TrackSourceVersion | MasterDelivery) -> AudioAnalysisRead:
    result = None
    if record.audio_analysis:
        try:
            result = AudioAnalysis.model_validate_json(record.audio_analysis)
            if _needs_reanalysis(record):
                result = None
        except Exception:
            logger.warning("Invalid stored audio analysis for %s %s", type(record).__name__, record.id)
    return AudioAnalysisRead(
        status="pending" if record.audio_analysis_status == "ready" and result is None else record.audio_analysis_status,
        result=result,
        error=record.audio_analysis_error,
        attempts=record.audio_analysis_attempts,
        analyzed_at=record.audio_analyzed_at,
    )


def _record_path(record: TrackSourceVersion | MasterDelivery) -> tuple[Path, bool]:
    if record.storage_backend == "r2":
        from app.services.r2 import download_to_temp

        return download_to_temp(record.file_path), True
    return Path(record.file_path), False


def _broadcast(track_id: int) -> None:
    if _event_loop and _event_loop.is_running():
        asyncio.run_coroutine_threadsafe(
            track_manager.broadcast(track_id, {"type": "track_updated", "track_id": track_id}),
            _event_loop,
        )


def _process(kind: str, record_id: int) -> None:
    model = TrackSourceVersion if kind == "source" else MasterDelivery
    db = SessionLocal()
    path: Path | None = None
    temporary = False
    track_id: int | None = None
    retry = False
    key = (kind, record_id)
    with _queue_lock:
        prepared_path = _prepared_paths.pop(key, None)
    if prepared_path is not None:
        path = prepared_path
        temporary = True
    try:
        record = db.get(model, record_id)
        if record is None or (record.audio_analysis_status == "ready" and not _needs_reanalysis(record)):
            return
        if not record.file_path or getattr(record, "source_kind", getattr(record, "delivery_kind", "file")) != "file":
            record.audio_analysis_status = "not_applicable"
            db.commit()
            return
        record.audio_analysis_status = "processing"
        record.audio_analysis_started_at = datetime.now(timezone.utc)
        record.audio_analysis_attempts += 1
        track_id = record.track_id
        db.commit()
        if path is None or not path.exists():
            path, temporary = _record_path(record)
        probed = probe_audio(path)
        record.audio_analysis = probed.model_dump_json()
        db.commit()
        _broadcast(record.track_id)
        result = analyze_audio(path, probed=probed)
        record = db.get(model, record_id)
        if record is None:
            return
        record.audio_analysis = result.model_dump_json()
        record.audio_analysis_status = "ready"
        record.audio_analysis_error = None
        record.audio_analyzed_at = datetime.now(timezone.utc)
        db.commit()
    except Exception as exc:
        db.rollback()
        record = db.get(model, record_id)
        if record is not None:
            record.audio_analysis_status = "failed"
            record.audio_analysis_error = str(exc)[:2000]
            record.audio_analyzed_at = datetime.now(timezone.utc)
            db.commit()
            track_id = record.track_id
            retry = record.audio_analysis_attempts < MAX_ANALYSIS_ATTEMPTS
        logger.exception("Audio analysis failed for %s %s", kind, record_id)
    finally:
        if temporary and path is not None:
            path.unlink(missing_ok=True)
        db.close()
        with _queue_lock:
            _queued.discard((kind, record_id))
        if track_id is not None:
            _broadcast(track_id)
        if retry:
            enqueue_audio_analysis(kind, record_id)


def enqueue_audio_analysis(
    kind: str,
    record_id: int,
    prepared_path: str | Path | None = None,
) -> None:
    local_path = Path(prepared_path) if prepared_path is not None else None
    if not shutil.which(settings.FFPROBE_PATH) or not shutil.which(settings.FFMPEG_PATH):
        logger.warning("Audio analysis remains pending because FFmpeg or FFprobe is unavailable.")
        if local_path is not None:
            local_path.unlink(missing_ok=True)
        return
    key = (kind, record_id)
    with _queue_lock:
        if key in _queued:
            if local_path is not None:
                local_path.unlink(missing_ok=True)
            return
        _queued.add(key)
        if local_path is not None:
            _prepared_paths[key] = local_path
    _executor.submit(_process, kind, record_id)


def _needs_reanalysis(record) -> bool:
    try:
        return json.loads(record.audio_analysis or "{}").get("analyzer_version", 0) < ANALYZER_VERSION
    except (ValueError, TypeError, AttributeError):
        return True


def enqueue_audio_analysis_backfill() -> None:
    stale_before = datetime.now(timezone.utc) - timedelta(minutes=STALE_PROCESSING_MINUTES)
    with SessionLocal() as db:
        for kind, model in (("source", TrackSourceVersion), ("delivery", MasterDelivery)):
            ids = []
            records = db.scalars(select(model).where(model.file_path.is_not(None)).order_by(model.id)).all()
            for record in records:
                if getattr(record, "source_kind", getattr(record, "delivery_kind", "file")) != "file":
                    continue
                if record.audio_analysis_status == "ready" and _needs_reanalysis(record):
                    record.audio_analysis_status = "pending"
                    record.audio_analysis_attempts = 0
                    record.audio_analysis_error = None
                started = record.audio_analysis_started_at
                stale = started is None or started.replace(tzinfo=timezone.utc) < stale_before
                if record.audio_analysis_attempts < MAX_ANALYSIS_ATTEMPTS and (
                    record.audio_analysis_status in {"pending", "failed"}
                    or record.audio_analysis_status == "processing" and stale
                ):
                    ids.append(record.id)
            db.commit()
            for record_id in ids:
                enqueue_audio_analysis(kind, record_id)
