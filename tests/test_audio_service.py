from types import SimpleNamespace

from app.services.audio import extract_audio_metadata


def test_extract_audio_metadata_returns_values(monkeypatch, tmp_path):
    audio_path = tmp_path / "demo.wav"
    audio_path.write_bytes(b"wav")

    class FakeInfo:
        length = 98.7
        bitrate = 320000
        sample_rate = 44100

    monkeypatch.setattr(
        "mutagen.File",
        lambda _path: SimpleNamespace(info=FakeInfo()),
    )

    meta = extract_audio_metadata(audio_path)

    assert meta.duration == 98.7
    assert meta.bitrate == 320000
    assert meta.sample_rate == 44100


def test_extract_audio_metadata_returns_empty_on_failure(monkeypatch, tmp_path):
    audio_path = tmp_path / "broken.wav"
    audio_path.write_bytes(b"wav")
    monkeypatch.setattr("mutagen.File", lambda _path: (_ for _ in ()).throw(RuntimeError("bad file")))

    meta = extract_audio_metadata(audio_path)

    assert meta.duration is None
    assert meta.bitrate is None
    assert meta.sample_rate is None
