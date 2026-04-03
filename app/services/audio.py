from dataclasses import dataclass
from pathlib import Path


@dataclass
class AudioMetadata:
    duration: float | None = None
    bitrate: int | None = None
    sample_rate: int | None = None


def extract_audio_metadata(file_path: str | Path) -> AudioMetadata:
    """Extract audio metadata using mutagen.

    Returns an AudioMetadata dataclass with whatever information could be
    extracted.  If mutagen is not installed or the file cannot be parsed the
    function returns an AudioMetadata with all fields set to None.
    """
    try:
        from mutagen import File as MutagenFile

        audio = MutagenFile(str(file_path))
        if audio is None:
            return AudioMetadata()

        duration: float | None = None
        bitrate: int | None = None
        sample_rate: int | None = None

        if audio.info is not None:
            if hasattr(audio.info, "length"):
                duration = float(audio.info.length)
            if hasattr(audio.info, "bitrate"):
                bitrate = int(audio.info.bitrate)
            if hasattr(audio.info, "sample_rate"):
                sample_rate = int(audio.info.sample_rate)

        return AudioMetadata(
            duration=duration,
            bitrate=bitrate,
            sample_rate=sample_rate,
        )
    except Exception:
        # Graceful fallback: return empty metadata if anything goes wrong
        return AudioMetadata()
