"""Advisory audio specifications; never change workflow permissions or state."""

from app.schemas.audio_analysis import AudioAnalysis, AudioSpec, AudioSpecCheck, AudioSpecDifference, AudioSpecs


def read_specs(value: str | None) -> AudioSpecs:
    return AudioSpecs.model_validate_json(value) if value else AudioSpecs()


def effective_specs(album, track=None) -> AudioSpecs:
    defaults = read_specs(album.audio_specs)
    overrides = read_specs(track.audio_spec_overrides) if track is not None else AudioSpecs()
    return AudioSpecs(**{
        kind: getattr(overrides, kind) or getattr(defaults, kind) or AudioSpec(enabled=False)
        for kind in ("source", "master")
    })


def check_spec(actual: AudioAnalysis | None, spec: AudioSpec | None, *, applicable: bool = True) -> AudioSpecCheck:
    if not applicable:
        return AudioSpecCheck(status="not_applicable")
    if spec is None or not spec.enabled:
        return AudioSpecCheck(status="disabled")
    if actual is None:
        return AudioSpecCheck(status="unknown")
    differences = []
    unknown = False
    for field, allowed in (
        ("container", spec.allowed_containers),
        ("sample_rate_hz", spec.allowed_sample_rates_hz),
        ("sample_format", spec.allowed_sample_formats),
    ):
        value = getattr(actual, field)
        if value is None:
            unknown = True
        elif value not in allowed:
            differences.append(AudioSpecDifference(field=field, actual=value, allowed=allowed))
    return AudioSpecCheck(status="mismatch" if differences else "unknown" if unknown else "match", differences=differences)


def record_spec_check(record, spec: AudioSpec | None) -> AudioSpecCheck:
    applicable = record is not None and bool(record.file_path) and getattr(record, "source_kind", getattr(record, "delivery_kind", "file")) == "file"
    actual = None
    # Format probing is persisted before the slower loudness pass completes.
    if record is not None and record.audio_analysis:
        try:
            actual = AudioAnalysis.model_validate_json(record.audio_analysis)
        except ValueError:
            pass
    return check_spec(actual, spec, applicable=applicable)
