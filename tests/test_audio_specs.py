from app.models.master_delivery import MasterDelivery
from app.schemas.audio_analysis import AudioAnalysis, AudioSpec, AudioSpecs
from app.services.audio_specs import check_spec, effective_specs, record_spec_check
from app.services import audio_analysis
from app.config import settings
from types import SimpleNamespace
from pathlib import Path
import sys


def enabled_spec(rate=48000):
    return AudioSpec(allowed_sample_rates_hz=[rate], allowed_sample_formats=["pcm_s24"])


def test_independent_spec_inheritance_and_disable(factory):
    owner = factory.user(role="producer")
    album = factory.album(producer=owner, mastering_engineer=owner)
    track = factory.track(album=album, submitter=owner)
    album.audio_specs = AudioSpecs(source=enabled_spec(), master=enabled_spec(96000)).model_dump_json()
    track.audio_spec_overrides = AudioSpecs(source=AudioSpec(enabled=False)).model_dump_json()
    specs = effective_specs(album, track)
    assert not specs.source.enabled
    assert specs.master.allowed_sample_rates_hz == [96000]
    track.audio_spec_overrides = AudioSpecs(master=enabled_spec()).model_dump_json()
    specs = effective_specs(album, track)
    assert specs.source.allowed_sample_rates_hz == [48000]
    assert specs.master.allowed_sample_rates_hz == [48000]


def test_check_uses_only_format_and_distinguishes_unknown_external_and_disabled():
    spec = enabled_spec()
    actual = AudioAnalysis(container="wav", sample_format="pcm_s24", sample_rate_hz=48000,
                           integrated_lufs=-4, true_peak_dbtp=2, dc_offset=0.2, loudness_range_lu=0)
    assert check_spec(actual, spec).status == "match"
    actual.sample_rate_hz = 44100
    check = check_spec(actual, spec)
    assert check.status == "mismatch"
    assert check.differences[0].model_dump() == {"field": "sample_rate_hz", "actual": 44100, "allowed": [48000]}
    assert check_spec(None, spec).status == "unknown"
    assert check_spec(AudioAnalysis(container="wav"), spec).status == "unknown"
    assert check_spec(None, spec, applicable=False).status == "not_applicable"
    assert check_spec(actual, AudioSpec(enabled=False)).status == "disabled"


def test_specs_permissions_and_advisory_delivery_flow(client, factory, auth_headers):
    owner = factory.user(role="producer")
    engineer = factory.user()
    composer = factory.user()
    outsider = factory.user()
    album = factory.album(producer=owner, mastering_engineer=engineer, members=[composer])
    track = factory.track(album=album, submitter=composer, status="mastering")
    payload = AudioSpecs(source=enabled_spec(), master=enabled_spec()).model_dump(mode="json")
    response = client.patch(f"/api/albums/{album.id}/audio-specs", headers=auth_headers(engineer), json=payload)
    assert response.status_code == 200, response.text
    assert response.json()["audio_specs"] == payload
    denied = client.patch(f"/api/tracks/{track.id}/audio-specs", headers=auth_headers(outsider), json=payload)
    assert denied.status_code in {403, 404}
    changed = client.patch(f"/api/tracks/{track.id}/audio-specs", headers=auth_headers(owner), json={"source": None, "master": enabled_spec(96000).model_dump()})
    assert changed.status_code == 200, changed.text
    assert changed.json()["effective_audio_specs"]["master"]["allowed_sample_rates_hz"] == [96000]
    # Specs do not add any server approval gates, even when a delivery mismatches.
    uploaded = client.post(f"/api/tracks/{track.id}/master-deliveries", headers=auth_headers(engineer), files={"file": ("master.mp3", b"ID3test", "audio/mpeg")})
    assert uploaded.status_code == 200, uploaded.text
    delivery_id = uploaded.json()["current_master_delivery"]["id"]
    delivery = factory.session.get(MasterDelivery, delivery_id)
    delivery.audio_analysis = AudioAnalysis(container="mp3", sample_rate_hz=44100).model_dump_json()
    factory.session.commit()
    assert record_spec_check(delivery, enabled_spec()).status == "mismatch"
    confirmed = client.post(f"/api/tracks/{track.id}/master-deliveries/{delivery_id}/confirm", headers=auth_headers(engineer))
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["status"] == "final_review"
    assert confirmed.json()["master_spec_check"]["status"] == "mismatch"
    for actor, status in [(owner, "final_review"), (composer, "completed")]:
        approved = client.post(f"/api/tracks/{track.id}/final-review/approve", headers=auth_headers(actor))
        assert approved.status_code == 200, approved.text
        assert approved.json()["status"] == status


def test_r2_delivery_has_no_handoff_gate_and_worker_probes_download(client, factory, auth_headers, session_factory, monkeypatch, tmp_path):
    owner = factory.user(role="producer")
    engineer = factory.user()
    album = factory.album(producer=owner, mastering_engineer=engineer)
    album.audio_specs = AudioSpecs(master=enabled_spec()).model_dump_json()
    track = factory.track(album=album, submitter=owner, status="mastering")
    factory.session.commit()
    local = tmp_path / "r2-download.wav"
    local.write_bytes(b"downloaded")
    downloaded = []
    def download(key):
        downloaded.append(key)
        return local
    monkeypatch.setattr(settings, "R2_ENABLED", True)
    monkeypatch.setitem(sys.modules, "app.services.r2", SimpleNamespace(object_exists=lambda key: True, download_to_temp=download))
    key = f"tracks/{track.id}/master/1/master.wav"
    response = client.post(f"/api/tracks/{track.id}/master-deliveries/confirm-upload", headers=auth_headers(engineer), json={"upload_id":"test", "object_key":key})
    assert response.status_code == 200, response.text
    delivery_id = response.json()["current_master_delivery"]["id"]
    assert response.json()["status"] == "mastering"
    assert response.json()["master_spec_check"]["status"] == "unknown"
    probe = AudioAnalysis(container="wav", sample_format="pcm_s16", sample_rate_hz=44100)
    monkeypatch.setattr(audio_analysis, "SessionLocal", session_factory)
    monkeypatch.setattr(audio_analysis, "probe_audio", lambda path: probe)
    monkeypatch.setattr(audio_analysis, "analyze_audio", lambda path, **kwargs: probe)
    audio_analysis._process("delivery", delivery_id)
    assert downloaded == [key] and not local.exists()
    factory.session.expire_all()
    record = factory.session.get(MasterDelivery, delivery_id)
    assert record.audio_analysis_status == "ready"
    assert record_spec_check(record, enabled_spec()).status == "mismatch"
