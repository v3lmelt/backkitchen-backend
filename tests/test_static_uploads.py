from pathlib import Path

import pytest

from app.config import ALLOWED_AUDIO_EXTENSIONS


@pytest.mark.parametrize("extension", sorted(ALLOWED_AUDIO_EXTENSIONS) + [".WAV", ".bin"])
def test_static_uploads_deny_audio_even_for_authenticated_members(
    client, factory, auth_headers, upload_dir: Path, extension: str,
):
    member = factory.user()
    audio = upload_dir / f"private_master_v1{extension}"
    audio.write_bytes(b"private audio")

    for headers in ({}, auth_headers(member)):
        response = client.get(f"/uploads/{audio.name}", headers=headers)
        assert response.status_code == 404
        assert b"private audio" not in response.content


def test_static_uploads_preserve_nested_public_images(client, upload_dir: Path):
    directory = upload_dir / "discussion_images"
    directory.mkdir()
    image = directory / "reference.png"
    content = b"\x89PNG\r\n\x1a\npublic image"
    image.write_bytes(content)

    response = client.get("/uploads/discussion_images/reference.png")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content == content


def test_static_uploads_block_directory_traversal(client, upload_dir: Path):
    (upload_dir.parent / "private.png").write_bytes(b"outside upload directory")

    response = client.get("/uploads/%2e%2e/private.png")

    assert response.status_code == 404
    assert b"outside upload directory" not in response.content
