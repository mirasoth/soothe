"""Tests for OS clipboard image paste into the TUI input."""

from __future__ import annotations

import base64
import io
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from soothe_cli.display.media_utils import (
    MAX_IMAGES_PER_MESSAGE,
    ImageData,
    get_images_from_clipboard,
    image_data_from_bytes,
)
from soothe_cli.tui.input import MediaTracker


def _tiny_png_bytes() -> bytes:
    """Return a minimal valid PNG payload via Pillow."""
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (2, 2), color=(10, 20, 30)).save(buf, format="PNG")
    return buf.getvalue()


def _tiny_tiff_bytes() -> bytes:
    """Return a minimal valid TIFF payload via Pillow."""
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (2, 2), color=(10, 20, 30)).save(buf, format="TIFF")
    return buf.getvalue()


def _make_image_data(index: int) -> ImageData:
    """Return a distinct stub `ImageData` for attach-path tests."""
    return ImageData(base64_data=f"abc{index}", format="png", placeholder="[image]")


def test_image_data_from_bytes_round_trips_png() -> None:
    raw = _tiny_png_bytes()
    data = image_data_from_bytes(raw, fallback_format="png")
    assert data is not None
    assert data.format == "png"
    assert data.base64_data
    assert data.placeholder == "[image]"


def test_image_data_from_bytes_rejects_garbage() -> None:
    assert image_data_from_bytes(b"not-an-image") is None


def test_image_data_from_bytes_rejects_oversized() -> None:
    raw = _tiny_png_bytes()
    with patch("soothe_cli.display.media_utils.MAX_MEDIA_BYTES", 8):
        assert image_data_from_bytes(raw) is None


def test_image_data_from_bytes_reencodes_tiff_to_png() -> None:
    raw = _tiny_tiff_bytes()
    data = image_data_from_bytes(raw, fallback_format="tiff")
    assert data is not None
    # Wire-safe MIME: the daemon allowlist has no image/tiff.
    assert data.format == "png"
    assert base64.b64decode(data.base64_data).startswith(b"\x89PNG")


def test_get_images_from_clipboard_returns_empty_when_empty() -> None:
    with patch(
        "soothe_cli.display.media_utils._read_clipboard_image_payloads",
        return_value=[],
    ):
        assert get_images_from_clipboard() == []


def test_get_images_from_clipboard_wraps_raw_bytes() -> None:
    raw = _tiny_png_bytes()
    with patch(
        "soothe_cli.display.media_utils._read_clipboard_image_payloads",
        return_value=[raw],
    ):
        images = get_images_from_clipboard()
    assert len(images) == 1
    assert images[0].format == "png"


def test_get_images_from_clipboard_reads_multiple_payloads() -> None:
    payloads = [_tiny_png_bytes(), _tiny_png_bytes()]
    with patch(
        "soothe_cli.display.media_utils._read_clipboard_image_payloads",
        return_value=payloads,
    ):
        images = get_images_from_clipboard()
    assert len(images) == 2


def test_get_images_from_clipboard_caps_at_max_images_per_message() -> None:
    payloads = [_tiny_png_bytes() for _ in range(MAX_IMAGES_PER_MESSAGE + 2)]
    with patch(
        "soothe_cli.display.media_utils._read_clipboard_image_payloads",
        return_value=payloads,
    ):
        images = get_images_from_clipboard()
    assert len(images) == MAX_IMAGES_PER_MESSAGE


def test_cmd_v_binding_registered_for_clipboard_image_paste() -> None:
    from textual.binding import Binding

    from soothe_cli.tui.widgets.chat_input import ChatTextArea

    bindings = [b for b in ChatTextArea.BINDINGS if isinstance(b, Binding)]
    paste_bindings = [b for b in bindings if b.action == "clipboard_image_paste"]
    assert paste_bindings, "clipboard_image_paste binding must exist"
    keys = {key.strip() for b in paste_bindings for key in b.key.split(",")}
    assert {"ctrl+v", "cmd+v"} <= keys


@pytest.mark.asyncio
async def test_attach_clipboard_images_inserts_single_placeholder() -> None:
    from soothe_cli.tui.widgets.chat_input import ChatInput

    tracker = MediaTracker()
    chat = ChatInput.__new__(ChatInput)
    chat._image_tracker = tracker
    text_area = MagicMock()
    chat._text_area = text_area

    media = _make_image_data(1)
    with (
        patch.object(ChatInput, "app", new_callable=PropertyMock) as mock_app,
        patch(
            "soothe_cli.display.media_utils.get_images_from_clipboard",
            return_value=[media],
        ),
    ):
        app = MagicMock()
        mock_app.return_value = app
        ok = await chat.attach_clipboard_images(notify_if_empty=True)

    assert ok is True
    assert [img.placeholder for img in tracker.get_images()] == ["[image 1]"]
    text_area.insert.assert_called_once_with("[image 1] ")
    app.notify.assert_not_called()


@pytest.mark.asyncio
async def test_attach_clipboard_images_inserts_multiple_placeholders() -> None:
    from soothe_cli.tui.widgets.chat_input import ChatInput

    tracker = MediaTracker()
    chat = ChatInput.__new__(ChatInput)
    chat._image_tracker = tracker
    text_area = MagicMock()
    chat._text_area = text_area

    media_list = [_make_image_data(i) for i in range(3)]
    with (
        patch.object(ChatInput, "app", new_callable=PropertyMock) as mock_app,
        patch(
            "soothe_cli.display.media_utils.get_images_from_clipboard",
            return_value=media_list,
        ),
    ):
        app = MagicMock()
        mock_app.return_value = app
        ok = await chat.attach_clipboard_images(notify_if_empty=True)

    assert ok is True
    assert [img.placeholder for img in tracker.get_images()] == [
        "[image 1]",
        "[image 2]",
        "[image 3]",
    ]
    text_area.insert.assert_called_once_with("[image 1] [image 2] [image 3] ")
    app.notify.assert_not_called()


@pytest.mark.asyncio
async def test_attach_clipboard_images_notifies_when_empty() -> None:
    from soothe_cli.tui.widgets.chat_input import ChatInput

    chat = ChatInput.__new__(ChatInput)
    chat._image_tracker = MediaTracker()
    chat._text_area = MagicMock()

    with (
        patch.object(ChatInput, "app", new_callable=PropertyMock) as mock_app,
        patch(
            "soothe_cli.display.media_utils.get_images_from_clipboard",
            return_value=[],
        ),
    ):
        app = MagicMock()
        mock_app.return_value = app
        ok = await chat.attach_clipboard_images(notify_if_empty=True)

    assert ok is False
    app.notify.assert_called_once()
    assert "No image" in app.notify.call_args.args[0]


@pytest.mark.asyncio
async def test_attach_clipboard_images_caps_when_tracker_near_limit() -> None:
    from soothe_cli.tui.widgets.chat_input import ChatInput

    tracker = MediaTracker()
    for i in range(MAX_IMAGES_PER_MESSAGE - 1):
        tracker.add_media(_make_image_data(i), "image")

    chat = ChatInput.__new__(ChatInput)
    chat._image_tracker = tracker
    text_area = MagicMock()
    chat._text_area = text_area

    media_list = [_make_image_data(i) for i in range(3)]
    with (
        patch.object(ChatInput, "app", new_callable=PropertyMock) as mock_app,
        patch(
            "soothe_cli.display.media_utils.get_images_from_clipboard",
            return_value=media_list,
        ),
    ):
        app = MagicMock()
        mock_app.return_value = app
        ok = await chat.attach_clipboard_images(notify_if_empty=True)

    assert ok is True
    assert len(tracker.get_images()) == MAX_IMAGES_PER_MESSAGE
    text_area.insert.assert_called_once_with("[image 8] ")
    app.notify.assert_called_once()
    assert "Image limit reached" in app.notify.call_args.args[0]


def _write_png_files(directory: Path, count: int) -> list[Path]:
    """Write `count` tiny PNG files into `directory` and return their paths."""
    paths = []
    for index in range(count):
        path = directory / f"img{index}.png"
        path.write_bytes(_tiny_png_bytes())
        paths.append(path)
    return paths


def test_build_path_replacement_caps_images_per_message(tmp_path: Path) -> None:
    from soothe_cli.tui.widgets.chat_input import ChatInput

    paths = _write_png_files(tmp_path, MAX_IMAGES_PER_MESSAGE + 1)

    tracker = MediaTracker()
    chat = ChatInput.__new__(ChatInput)
    chat._image_tracker = tracker

    raw_text = " ".join(str(p) for p in paths)
    with patch.object(ChatInput, "app", new_callable=PropertyMock) as mock_app:
        app = MagicMock()
        mock_app.return_value = app
        replacement, attached = chat._build_path_replacement(
            raw_text, paths, add_trailing_space=True
        )

    assert attached is True
    assert len(tracker.get_images()) == MAX_IMAGES_PER_MESSAGE
    assert str(paths[-1]) in replacement, "overflow file kept as path text"
    app.notify.assert_called_once()
    notify_text = app.notify.call_args.args[0]
    assert "Image limit reached" in notify_text


def test_build_path_replacement_attaches_all_under_limit(tmp_path: Path) -> None:
    from soothe_cli.tui.widgets.chat_input import ChatInput

    paths = _write_png_files(tmp_path, 3)

    tracker = MediaTracker()
    chat = ChatInput.__new__(ChatInput)
    chat._image_tracker = tracker

    raw_text = "\n".join(str(p) for p in paths)
    with patch.object(ChatInput, "app", new_callable=PropertyMock) as mock_app:
        app = MagicMock()
        mock_app.return_value = app
        replacement, attached = chat._build_path_replacement(
            raw_text, paths, add_trailing_space=False
        )

    assert attached is True
    assert len(tracker.get_images()) == 3
    assert replacement == "[image 1]\n[image 2]\n[image 3]"
    app.notify.assert_not_called()


def test_paste_command_registered() -> None:
    from soothe_cli.commands.command_registry import COMMANDS, SIDE_EFFECT_FREE

    names = {cmd.name for cmd in COMMANDS}
    assert "/paste" in names
    assert "/paste" in SIDE_EFFECT_FREE
