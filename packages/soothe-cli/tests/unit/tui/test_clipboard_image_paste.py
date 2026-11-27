"""Tests for OS clipboard image paste into the TUI input."""

from __future__ import annotations

import base64
import io
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

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


def test_cmd_v_binding_registered_for_clipboard_paste() -> None:
    from textual.binding import Binding

    from soothe_cli.tui.widgets.chat_input import ChatTextArea

    bindings = [b for b in ChatTextArea.BINDINGS if isinstance(b, Binding)]
    paste_bindings = [b for b in bindings if b.action == "clipboard_paste"]
    assert paste_bindings, "clipboard_paste binding must exist"
    keys = {key.strip() for b in paste_bindings for key in b.key.split(",")}
    assert {"ctrl+v", "cmd+v", "super+v"} <= keys


@pytest.mark.asyncio
async def test_clipboard_paste_falls_back_to_text_when_no_image() -> None:
    """Cmd/Ctrl+V pastes clipboard text when the clipboard has no image."""
    from textual.events import Paste

    from soothe_cli.tui.widgets.chat_input import ChatInput, ChatTextArea

    chat = ChatInput.__new__(ChatInput)
    text_area = MagicMock()
    chat._text_area = text_area

    with (
        patch.object(
            ChatInput, "attach_clipboard_images", new_callable=AsyncMock, return_value=False
        ),
        patch(
            "soothe_cli.display.media_utils.get_text_from_clipboard",
            return_value="hello from clipboard",
        ),
    ):
        await chat.on_chat_text_area_clipboard_paste(
            ChatTextArea.ClipboardPaste(notify_if_empty=True, fallback_to_text=True)
        )

    posted = text_area.post_message.call_args.args[0]
    assert isinstance(posted, Paste)
    assert posted.text == "hello from clipboard"


@pytest.mark.asyncio
async def test_clipboard_paste_empty_bracketed_does_not_repaste_text() -> None:
    """Empty bracketed pastes (image-only clipboards) must not re-paste text."""
    from soothe_cli.tui.widgets.chat_input import ChatInput, ChatTextArea

    chat = ChatInput.__new__(ChatInput)
    text_area = MagicMock()
    chat._text_area = text_area

    with (
        patch.object(
            ChatInput, "attach_clipboard_images", new_callable=AsyncMock, return_value=False
        ),
        patch("soothe_cli.display.media_utils.get_text_from_clipboard") as mock_get_text,
        patch.object(ChatInput, "app", new_callable=PropertyMock) as mock_app,
    ):
        app = MagicMock()
        mock_app.return_value = app
        await chat.on_chat_text_area_clipboard_paste(
            ChatTextArea.ClipboardPaste(notify_if_empty=False)
        )

    mock_get_text.assert_not_called()
    text_area.post_message.assert_not_called()
    app.notify.assert_not_called()


@pytest.mark.asyncio
async def test_clipboard_paste_notifies_when_clipboard_empty() -> None:
    from soothe_cli.tui.widgets.chat_input import ChatInput, ChatTextArea

    chat = ChatInput.__new__(ChatInput)
    text_area = MagicMock()
    chat._text_area = text_area

    with (
        patch.object(
            ChatInput, "attach_clipboard_images", new_callable=AsyncMock, return_value=False
        ),
        patch("soothe_cli.display.media_utils.get_text_from_clipboard", return_value=None),
        patch.object(ChatInput, "app", new_callable=PropertyMock) as mock_app,
    ):
        app = MagicMock()
        mock_app.return_value = app
        await chat.on_chat_text_area_clipboard_paste(
            ChatTextArea.ClipboardPaste(notify_if_empty=True, fallback_to_text=True)
        )

    text_area.post_message.assert_not_called()
    app.notify.assert_called_once()
    assert "no image or text" in app.notify.call_args.args[0].lower()


@pytest.mark.asyncio
async def test_get_text_from_clipboard_prefers_first_non_empty_reader() -> None:
    from soothe_cli.display import media_utils

    with (
        patch.object(media_utils, "_read_clipboard_text_macos", return_value=None),
        patch.object(media_utils, "_read_clipboard_text_wsl", return_value="  "),
        patch.object(media_utils, "_read_clipboard_text_wayland", return_value="wayland text"),
        patch.object(media_utils, "_read_clipboard_text_x11") as mock_x11,
    ):
        assert media_utils.get_text_from_clipboard() == "wayland text"
    mock_x11.assert_not_called()


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
    assert [img.placeholder for img in tracker.get_images()] == ["[image 1: clipboard]"]
    text_area.insert.assert_called_once_with("[image 1: clipboard] ")
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
        "[image 1: clipboard]",
        "[image 2: clipboard]",
        "[image 3: clipboard]",
    ]
    text_area.insert.assert_called_once_with(
        "[image 1: clipboard] [image 2: clipboard] [image 3: clipboard] "
    )
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
    text_area.insert.assert_called_once_with("[image 8: clipboard] ")
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
    assert replacement == (
        f"[image 1: {paths[0].name}]\n[image 2: {paths[1].name}]\n[image 3: {paths[2].name}]"
    )
    app.notify.assert_not_called()


def test_paste_command_registered() -> None:
    from soothe_cli.commands.command_registry import COMMANDS, SIDE_EFFECT_FREE

    names = {cmd.name for cmd in COMMANDS}
    assert "/paste" in names
    assert "/paste" in SIDE_EFFECT_FREE


def test_named_placeholder_retained_and_pruned_by_sync() -> None:
    from soothe_cli.tui.input import MediaTracker

    tracker = MediaTracker()
    placeholder = tracker.add_media(_make_image_data(1), "image", source_name="photo.jpg")
    assert placeholder == "[image 1: photo.jpg]"

    tracker.sync_to_text(f"look at {placeholder} please")
    assert len(tracker.get_images()) == 1

    tracker.sync_to_text("placeholder deleted")
    assert tracker.get_images() == []
    assert tracker.next_image_id == 1


def test_named_placeholder_sanitizes_and_truncates() -> None:
    from soothe_cli.tui.input import PLACEHOLDER_NAME_MAX_LEN, MediaTracker

    tracker = MediaTracker()
    long_name = "x" * 120
    placeholder = tracker.add_media(_make_image_data(1), "image", source_name=long_name)
    assert len(placeholder) <= len("[image 1: ") + PLACEHOLDER_NAME_MAX_LEN + len("]")
    assert placeholder.endswith("…]")

    bracketed = tracker.add_media(_make_image_data(2), "image", source_name="a]b[ c")
    assert bracketed == "[image 2: a)b( c]"


def test_placeholder_pattern_matches_named_token() -> None:
    from soothe_cli.tui.input import IMAGE_PLACEHOLDER_PATTERN

    match = IMAGE_PLACEHOLDER_PATTERN.search("text [image 3: 安全交底.jpg] end")
    assert match is not None
    assert match.group("id") == "3"
    assert match.group("name") == "安全交底.jpg"
    # Legacy format without a name still parses.
    match = IMAGE_PLACEHOLDER_PATTERN.search("text [image 3] end")
    assert match is not None
    assert match.group("id") == "3"
    assert match.group("name") is None


def test_attachments_command_registered() -> None:
    from soothe_cli.commands.command_registry import COMMANDS, SIDE_EFFECT_FREE

    names = {cmd.name for cmd in COMMANDS}
    assert "/attachments" in names
    assert "/attachments" in SIDE_EFFECT_FREE


def test_attachment_summary_lines_and_clear(tmp_path: Path) -> None:
    from soothe_cli.display.media_utils import VideoData
    from soothe_cli.tui.widgets.chat_input import ChatInput

    tracker = MediaTracker()
    image_path = tmp_path / "photo.jpg"
    image_path.write_bytes(_tiny_png_bytes())
    media = image_data_from_bytes(image_path.read_bytes(), fallback_format="jpg")
    assert media is not None
    image_placeholder = tracker.add_media(media, "image", source_name=image_path.name)
    video_b64 = base64.b64encode(b"0123456789abcdef").decode()
    video_placeholder = tracker.add_media(
        VideoData(base64_data=video_b64, format="mp4", placeholder="[video]"),
        "video",
        source_name="clip.mp4",
    )

    chat = ChatInput.__new__(ChatInput)
    chat._image_tracker = tracker
    lines = chat.attachment_summary_lines()
    assert lines[0] == f"{image_placeholder} ({media.format}, ~{_b64_size(media.base64_data)})"
    assert lines[1] == f"{video_placeholder} (mp4, ~{_b64_size(video_b64)})"

    text_area = MagicMock()
    text_area.text = f"{image_placeholder} analyze and {video_placeholder}"
    chat._text_area = text_area
    removed = chat.clear_attachments()

    assert removed == 2
    assert tracker.get_images() == []
    assert tracker.get_videos() == []
    text_area.load_text.assert_called_once_with("analyze and")


def _b64_size(base64_data: str) -> str:
    """Human size label mirroring `_media_size_label` for test expectations."""
    approx = max(len(base64_data) * 3 // 4, 0)
    if approx < 1024:
        return f"{approx} B"
    if approx < 1024 * 1024:
        return f"{approx / 1024:.0f} KB"
    return f"{approx / (1024 * 1024):.1f} MB"


@pytest.mark.asyncio
async def test_clipboard_attach_logs_event(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    from soothe_cli.tui.widgets.chat_input import ChatInput

    chat = ChatInput.__new__(ChatInput)
    chat._image_tracker = MediaTracker()
    chat._text_area = MagicMock()

    with (
        patch.object(ChatInput, "app", new_callable=PropertyMock) as mock_app,
        patch(
            "soothe_cli.display.media_utils.get_images_from_clipboard",
            return_value=[_make_image_data(1)],
        ),
        caplog.at_level(logging.INFO, logger="soothe_cli.tui.widgets.chat_input"),
    ):
        mock_app.return_value = MagicMock()
        await chat.attach_clipboard_images(notify_if_empty=False)

    assert "Clipboard attach: 1 image(s)" in caplog.text
    assert "png" in caplog.text


def test_stale_media_placeholders_stripped_and_notified() -> None:
    """History-recalled placeholder text without tracker data is stripped."""
    from soothe_cli.tui.widgets.chat_input import ChatInput

    chat = ChatInput.__new__(ChatInput)
    chat._image_tracker = MediaTracker()

    value = "[image 1: a.jpg] [image 2: b.jpg] analyze"
    with patch.object(ChatInput, "app", new_callable=PropertyMock) as mock_app:
        app = MagicMock()
        mock_app.return_value = app
        result = chat._strip_stale_media_placeholders(value)

    assert result == "analyze"
    app.notify.assert_called_once()
    assert "Re-attach the files" in app.notify.call_args.args[0]


def test_live_media_placeholders_kept_on_submit() -> None:
    """Placeholders backed by tracker data pass through untouched."""
    from soothe_cli.tui.widgets.chat_input import ChatInput

    tracker = MediaTracker()
    placeholder = tracker.add_media(_make_image_data(1), "image", source_name="photo.jpg")

    chat = ChatInput.__new__(ChatInput)
    chat._image_tracker = tracker

    value = f"{placeholder} analyze this"
    with patch.object(ChatInput, "app", new_callable=PropertyMock) as mock_app:
        app = MagicMock()
        mock_app.return_value = app
        result = chat._strip_stale_media_placeholders(value)

    assert result == value
    app.notify.assert_not_called()


def test_partially_stale_media_placeholders_pruned() -> None:
    """Only placeholders without tracker data are removed; live ones survive."""
    from soothe_cli.tui.widgets.chat_input import ChatInput

    tracker = MediaTracker()
    live = tracker.add_media(_make_image_data(1), "image", source_name="live.jpg")

    chat = ChatInput.__new__(ChatInput)
    chat._image_tracker = tracker

    value = f"{live} [image 2: gone.jpg]"
    with patch.object(ChatInput, "app", new_callable=PropertyMock) as mock_app:
        app = MagicMock()
        mock_app.return_value = app
        result = chat._strip_stale_media_placeholders(value)

    assert result == live
    app.notify.assert_called_once()


def test_stale_strip_noop_without_tracker() -> None:
    """Without a tracker the submitted text is returned unchanged."""
    from soothe_cli.tui.widgets.chat_input import ChatInput

    chat = ChatInput.__new__(ChatInput)
    chat._image_tracker = None

    value = "[image 1: a.jpg] analyze"
    assert chat._strip_stale_media_placeholders(value) == value
