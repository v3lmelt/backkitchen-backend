"""Serve public upload images; issue attachments use authorized API routes."""

import os
from pathlib import Path

from fastapi.staticfiles import StaticFiles

from app.services.attachments import ALLOWED_IMAGE_EXTENSIONS

PRIVATE_IMAGE_DIRECTORIES = {"issue_images", "comment_images"}


class PublicImageStaticFiles(StaticFiles):
    def lookup_path(self, path: str) -> tuple[str, os.stat_result | None]:
        if any(part.lower() in PRIVATE_IMAGE_DIRECTORIES for part in Path(path).parts):
            return "", None
        if Path(path).suffix.lower() not in ALLOWED_IMAGE_EXTENSIONS:
            return "", None
        full_path, stat_result = super().lookup_path(path)
        if stat_result is not None:
            for directory in self.all_directories:
                try:
                    relative_parts = Path(full_path).relative_to(Path(directory).resolve()).parts
                except ValueError:
                    continue
                if any(part.lower() in PRIVATE_IMAGE_DIRECTORIES for part in relative_parts):
                    return "", None
        # Check the resolved target too, so a local image-named symlink cannot
        # turn an audio file into a public asset.
        if Path(full_path).suffix.lower() not in ALLOWED_IMAGE_EXTENSIONS:
            return "", None
        return full_path, stat_result
