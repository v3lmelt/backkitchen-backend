import base64
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class AudioMetadata:
    duration: float | None = None
    bitrate: int | None = None
    sample_rate: int | None = None


def _detect_cover_mime(data: bytes) -> str:
    """Detect image MIME type from raw bytes."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:2] == b"\xff\xd8":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def embed_audio_metadata(
    file_path: str | Path,
    *,
    title: str | None = None,
    artist: str | None = None,
    album: str | None = None,
    album_artist: str | None = None,
    track_number: int | None = None,
    total_tracks: int | None = None,
    genre: str | None = None,
    date: str | None = None,
    catalog_number: str | None = None,
    cover_data: bytes | None = None,
) -> bool:
    """Write metadata tags + optional cover art into an audio file in-place.

    Supports MP3, FLAC, OGG Vorbis, M4A/MP4, and WAV.
    Returns True on success, False on failure (logged as warning).
    """
    try:
        from mutagen import File as MutagenFile

        file_path = Path(file_path)
        suffix = file_path.suffix.lower()
        audio = MutagenFile(str(file_path))
        if audio is None:
            logger.warning("mutagen could not open %s", file_path)
            return False

        cover_mime = _detect_cover_mime(cover_data) if cover_data else "image/jpeg"

        if suffix == ".mp3":
            _embed_id3(audio, title=title, artist=artist, album=album,
                       album_artist=album_artist, track_number=track_number,
                       total_tracks=total_tracks, genre=genre, date=date,
                       catalog_number=catalog_number,
                       cover_data=cover_data, cover_mime=cover_mime)
        elif suffix == ".flac":
            _embed_vorbis(audio, title=title, artist=artist, album=album,
                          album_artist=album_artist, track_number=track_number,
                          total_tracks=total_tracks, genre=genre, date=date,
                          catalog_number=catalog_number)
            if cover_data:
                _embed_flac_picture(audio, cover_data, cover_mime)
        elif suffix == ".ogg":
            _embed_vorbis(audio, title=title, artist=artist, album=album,
                          album_artist=album_artist, track_number=track_number,
                          total_tracks=total_tracks, genre=genre, date=date,
                          catalog_number=catalog_number)
            if cover_data:
                _embed_ogg_picture(audio, cover_data, cover_mime)
        elif suffix in (".m4a", ".mp4", ".aac"):
            _embed_mp4(audio, title=title, artist=artist, album=album,
                       album_artist=album_artist, track_number=track_number,
                       total_tracks=total_tracks, genre=genre, date=date,
                       catalog_number=catalog_number,
                       cover_data=cover_data, cover_mime=cover_mime)
        elif suffix == ".wav":
            _embed_id3(audio, title=title, artist=artist, album=album,
                       album_artist=album_artist, track_number=track_number,
                       total_tracks=total_tracks, genre=genre, date=date,
                       catalog_number=catalog_number,
                       cover_data=cover_data, cover_mime=cover_mime)
        else:
            logger.warning("Unsupported format for metadata embedding: %s", suffix)
            return False

        audio.save()
        return True
    except Exception:
        logger.warning("Failed to embed metadata into %s", file_path, exc_info=True)
        return False


def _embed_id3(audio, *, title, artist, album, album_artist, track_number,
               total_tracks, genre, date, catalog_number, cover_data, cover_mime):
    """Write ID3 tags (MP3 / WAV)."""
    from mutagen.id3 import APIC, ID3, TALB, TCON, TDRC, TIT2, TPE1, TPE2, TRCK, TXXX

    if audio.tags is None:
        audio.add_tags()
    tags = audio.tags
    if not isinstance(tags, ID3):
        # WAVE wraps tags; get the ID3 object
        if hasattr(tags, "_tags"):
            tags = tags._tags

    if title:
        tags.add(TIT2(encoding=3, text=title))
    if artist:
        tags.add(TPE1(encoding=3, text=artist))
    if album:
        tags.add(TALB(encoding=3, text=album))
    if album_artist:
        tags.add(TPE2(encoding=3, text=album_artist))
    if track_number is not None:
        trck = str(track_number)
        if total_tracks is not None:
            trck += f"/{total_tracks}"
        tags.add(TRCK(encoding=3, text=trck))
    if genre:
        tags.add(TCON(encoding=3, text=genre))
    if date:
        tags.add(TDRC(encoding=3, text=date))
    if catalog_number:
        tags.add(TXXX(encoding=3, desc="CATALOGNUMBER", text=catalog_number))
    if cover_data:
        tags.add(APIC(encoding=3, mime=cover_mime, type=3, desc="Cover", data=cover_data))


def _embed_vorbis(audio, *, title, artist, album, album_artist, track_number,
                  total_tracks, genre, date, catalog_number):
    """Write VorbisComment tags (FLAC / OGG)."""
    if audio.tags is None:
        audio.add_tags()
    if title:
        audio.tags["title"] = [title]
    if artist:
        audio.tags["artist"] = [artist]
    if album:
        audio.tags["album"] = [album]
    if album_artist:
        audio.tags["albumartist"] = [album_artist]
    if track_number is not None:
        audio.tags["tracknumber"] = [str(track_number)]
    if total_tracks is not None:
        audio.tags["tracktotal"] = [str(total_tracks)]
    if genre:
        audio.tags["genre"] = [genre]
    if date:
        audio.tags["date"] = [date]
    if catalog_number:
        audio.tags["catalognumber"] = [catalog_number]


def _embed_flac_picture(audio, cover_data: bytes, cover_mime: str):
    """Embed cover art into FLAC via native picture block."""
    from mutagen.flac import Picture

    pic = Picture()
    pic.data = cover_data
    pic.type = 3  # Front cover
    pic.mime = cover_mime
    audio.clear_pictures()
    audio.add_picture(pic)


def _embed_ogg_picture(audio, cover_data: bytes, cover_mime: str):
    """Embed cover art into OGG Vorbis via metadata_block_picture."""
    from mutagen.flac import Picture

    pic = Picture()
    pic.data = cover_data
    pic.type = 3
    pic.mime = cover_mime
    audio.tags["metadata_block_picture"] = [
        base64.b64encode(pic.write()).decode("ascii")
    ]


def _embed_mp4(audio, *, title, artist, album, album_artist, track_number,
               total_tracks, genre, date, catalog_number, cover_data, cover_mime):
    """Write MP4/M4A atoms."""
    from mutagen.mp4 import MP4Cover, MP4FreeForm

    if audio.tags is None:
        audio.add_tags()
    if title:
        audio.tags["\xa9nam"] = [title]
    if artist:
        audio.tags["\xa9ART"] = [artist]
    if album:
        audio.tags["\xa9alb"] = [album]
    if album_artist:
        audio.tags["aART"] = [album_artist]
    if track_number is not None:
        audio.tags["trkn"] = [(track_number, total_tracks or 0)]
    if genre:
        audio.tags["\xa9gen"] = [genre]
    if date:
        audio.tags["\xa9day"] = [date]
    if catalog_number:
        audio.tags["----:com.apple.iTunes:CATALOGNUMBER"] = [
            MP4FreeForm(catalog_number.encode("utf-8"))
        ]
    if cover_data:
        fmt = MP4Cover.FORMAT_PNG if cover_mime == "image/png" else MP4Cover.FORMAT_JPEG
        audio.tags["covr"] = [MP4Cover(cover_data, imageformat=fmt)]


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
        logger.warning("Failed to extract audio metadata from %s", file_path, exc_info=True)
        return AudioMetadata()
