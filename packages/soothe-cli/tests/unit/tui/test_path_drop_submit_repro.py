"""Reproduction: multi-file drop then immediate submit must keep attachments.

Simulates the production sequence for loop 01a21817 (2026-11-26 21:42):
terminal delivers a paste payload with 3 image paths, user hits Enter,
execute_task_textual snapshots image_tracker — tracker must still hold 3 images.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from textual.app import App, ComposeResult
from textual.events import Paste

from soothe_cli.display.media_utils import ImageData
from soothe_cli.tui.input import MediaTracker
from soothe_cli.tui.widgets.chat_input import ChatInput


def _tiny_png_bytes() -> bytes:
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (2, 2), color=(10, 20, 30)).save(buf, format="PNG")
    return buf.getvalue()


def _write_png_files(directory: Path, count: int) -> list[Path]:
    paths = []
    for index in range(count):
        path = directory / f"img{index}.png"
        path.write_bytes(_tiny_png_bytes())
        paths.append(path)
    return paths


class _HostApp(App):
    def __init__(self, tracker: MediaTracker) -> None:
        super().__init__()
        self._tracker = tracker
        self.chat: ChatInput | None = None

    def get_css_variables(self) -> dict[str, str]:
        vars_ = super().get_css_variables()
        vars_.setdefault("mode-bash", "red")
        vars_.setdefault("mode-command", "blue")
        vars_.setdefault("mode-normal", "green")
        return vars_

    def compose(self) -> ComposeResult:
        yield ChatInput(image_tracker=self._tracker, id="input-area")


@pytest.mark.asyncio
async def test_multi_path_drop_then_submit_keeps_attachments(tmp_path: Path) -> None:
    paths = _write_png_files(tmp_path, 3)
    payload = "\n".join(str(p) for p in paths)
    tracker = MediaTracker()
    app = _HostApp(tracker)
    async with app.run_test() as pilot:
        chat = app.chat or app.query_one("#input-area", ChatInput)
        text_area = chat._text_area
        assert text_area is not None

        # Deliver the drop payload the way a terminal bracketed-paste does.
        text_area.post_message(Paste(payload))
        await pilot.pause()

        # Attachments should be tracked and placeholders inserted.
        assert len(tracker.get_images()) == 3, (
            f"after paste: {len(tracker.get_images())} images, text={text_area.text!r}"
        )
        assert "[image 1:" in text_area.text

        # Submit exactly like pressing Enter (value = current text).
        chat._submit_value(text_area.text.strip())
        await pilot.pause()
        await asyncio.sleep(0.05)
        await pilot.pause()

        # The tracker must survive until the adapter snapshot (execute start).
        assert len(tracker.get_images()) == 3, (
            f"after submit: {len(tracker.get_images())} images, "
            f"text={text_area.text!r}, skip={chat._skip_media_sync_events}"
        )
        assert all(isinstance(img, ImageData) for img in tracker.get_images())
