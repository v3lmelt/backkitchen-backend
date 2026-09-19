import json
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.config import settings
from app.models.track_source_version import TrackSourceVersion
from app.schemas.audio_analysis import AudioAnalysis, AudioSpec, AudioSpecs
from app.services import audio_analysis
from app.services.audio_analysis import analyze_audio, probe_audio
from app.services.audio_specs import effective_specs, check_spec

def test_analysis_retry_stops_after_three_attempts(factory, session_factory, monkeypatch):
    producer = factory.user(role="producer")
    mastering = factory.user()
    submitter = factory.user()
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter])
    track = factory.track(album=album, submitter=submitter)
    source = track.source_versions[0]
    monkeypatch.setattr(audio_analysis, "probe_audio", lambda _path: AudioAnalysis())
    retries: list[tuple[str, int]] = []
    monkeypatch.setattr(audio_analysis, "SessionLocal", session_factory)
    monkeypatch.setattr(audio_analysis, "analyze_audio", lambda _path, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(audio_analysis, "enqueue_audio_analysis", lambda kind, record_id: retries.append((kind, record_id)))
    monkeypatch.setattr(audio_analysis, "_broadcast", lambda _track_id: None)

    for _ in range(3):
        audio_analysis._process("source", source.id)

    factory.session.expire_all()
    stored = factory.session.get(TrackSourceVersion, source.id)
    assert stored.audio_analysis_status == "failed"
    assert stored.audio_analysis_attempts == 3
    assert retries == [("source", source.id), ("source", source.id)]


def test_analysis_backfill_enqueues_pending_failed_and_stale_records(factory, session_factory, monkeypatch):
    producer = factory.user(role="producer")
    mastering = factory.user()
    submitter = factory.user()
    album = factory.album(producer=producer, mastering_engineer=mastering, members=[submitter])
    track = factory.track(album=album, submitter=submitter, status="mastering")
    source = track.source_versions[0]
    source.audio_analysis_status = "processing"
    source.audio_analysis_started_at = datetime.now(timezone.utc) - timedelta(minutes=31)
    delivery = factory.master_delivery(track=track, uploaded_by=mastering)
    delivery.audio_analysis_status = "failed"
    delivery.audio_analysis_attempts = 1
    factory.session.commit()
    queued: list[tuple[str, int]] = []
    monkeypatch.setattr(audio_analysis, "SessionLocal", session_factory)
    monkeypatch.setattr(audio_analysis.shutil, "which", lambda _name: "available")
    monkeypatch.setattr(audio_analysis, "enqueue_audio_analysis", lambda kind, record_id: queued.append((kind, record_id)))

    audio_analysis.enqueue_audio_analysis_backfill()

    assert ("source", source.id) in queued
    assert ("delivery", delivery.id) in queued


def test_old_analysis_is_hidden_and_requeued_once(factory, session_factory, monkeypatch):
    owner = factory.user(role="producer")
    album = factory.album(producer=owner, mastering_engineer=owner)
    track = factory.track(album=album, submitter=owner)
    source = track.source_versions[0]
    source.audio_analysis_status = "ready"
    source.audio_analysis_attempts = 3
    source.audio_analysis = '{"analyzer_version":1,"integrated_lufs":-70}'
    factory.session.commit()
    assert audio_analysis.analysis_read(source).result is None
    assert audio_analysis.analysis_read(source).status == "pending"
    queued = []
    monkeypatch.setattr(audio_analysis, "SessionLocal", session_factory)
    monkeypatch.setattr(audio_analysis, "enqueue_audio_analysis", lambda *args: queued.append(args))
    audio_analysis.enqueue_audio_analysis_backfill()
    factory.session.expire_all()
    assert source.audio_analysis_attempts == 0
    assert source.audio_analysis_status == "pending"
    assert ("source", source.id) in queued
    # Analysis worker has stored the new version: it must not be requeued again.
    source.audio_analysis_status = "ready"
    source.audio_analysis = AudioAnalysis().model_dump_json()
    factory.session.commit()
    queued.clear()
    audio_analysis.enqueue_audio_analysis_backfill()
    assert queued == []


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg is unavailable")
def test_ffmpeg_analysis_matches_reference_tone(tmp_path: Path):
    path = tmp_path / "reference.wav"
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=1000:duration=1",
            "-c:a", "pcm_s24le", "-ar", "48000", "-y", str(path),
        ],
        check=True,
    )
    probed = probe_audio(path)
    assert probed.container == "wav"
    assert probed.sample_format == "pcm_s24"
    assert probed.bit_depth == 24
    assert probed.sample_rate_hz == 48000

    result = analyze_audio(path)
    assert result.sample_peak_dbfs == pytest.approx(-18.06, abs=0.1)
    assert result.true_peak_dbtp == pytest.approx(-18.06, abs=0.1)
    assert result.integrated_lufs == pytest.approx(-21.07, abs=0.1)
    assert result.dc_offset == pytest.approx(0.0, abs=1e-6)
    assert result.sample_peak_dbfs_by_channel == pytest.approx([-18.06], abs=0.1)
    assert result.true_peak_dbtp_by_channel == pytest.approx([-18.06], abs=0.1)
    assert result.dc_offset_by_channel == pytest.approx([0.0], abs=1e-6)


@pytest.mark.parametrize(
    ("encoder", "expected_format", "expected_depth"),
    [
        ("pcm_s16le", "pcm_s16", 16),
        ("pcm_s24le", "pcm_s24", 24),
        ("pcm_s32le", "pcm_s32", 32),
        ("pcm_f32le", "pcm_f32", 32),
    ],
)
@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg is unavailable")
def test_ffprobe_recognizes_supported_wav_sample_formats(
    tmp_path: Path,
    encoder: str,
    expected_format: str,
    expected_depth: int,
):
    path = tmp_path / f"{expected_format}.wav"
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=500:duration=0.1",
            "-c:a", encoder, "-ar", "96000", "-rf64", "always", "-y", str(path),
        ],
        check=True,
    )

    result = probe_audio(path)

    assert path.read_bytes()[:4] == b"RF64"
    assert result.container == "wav"
    assert result.sample_format == expected_format
    assert result.bit_depth == expected_depth
    assert result.sample_rate_hz == 96000
