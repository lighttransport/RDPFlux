from __future__ import annotations

import asyncio
import logging
from typing import Any

from . import capture, imaging, inject
from .actions import Action
from .service import Screenshot

LOG = logging.getLogger(__name__)


class WindowsBackend:
    """Screen capture and input injection for the interactive RDP session."""

    def __init__(self) -> None:
        capture.ensure_dpi_aware()
        self._jpeg = imaging.have_jpeg()
        if not self._jpeg:
            LOG.info("Pillow is not installed; screenshots will use PNG "
                     "(install rdpflux[control] for smaller JPEG frames)")

    def native_size(self) -> tuple[int, int]:
        _, _, width, height = capture.virtual_screen()
        return width, height

    async def screenshot(self, *, width: int, image_format: str, quality: int) -> Screenshot:
        return await asyncio.to_thread(self._screenshot, width, image_format, quality)

    def _screenshot(self, width: int, image_format: str, quality: int) -> Screenshot:
        rgb, delivered_width, delivered_height, native_width, native_height = capture.grab(width)
        if image_format == "jpeg" and not self._jpeg:
            # Report what was actually sent rather than failing the request.
            image_format = "png"
        if image_format == "jpeg":
            data = imaging.encode_jpeg(rgb, delivered_width, delivered_height, quality)
        else:
            data = imaging.encode_png(rgb, delivered_width, delivered_height)
        return Screenshot(data, delivered_width, delivered_height,
                          native_width, native_height, image_format)

    async def perform(self, action: Action) -> dict[str, Any]:
        return await asyncio.to_thread(inject.perform, action)
