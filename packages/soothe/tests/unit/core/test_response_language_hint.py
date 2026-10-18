"""Tests for dynamic response-language hint builder."""

from soothe.prompts import SystemTemplates
from soothe.sloop.intention.models import ResponseLanguage


def test_build_response_language_hint_zh() -> None:
    hint = SystemTemplates.build_response_language_hint(ResponseLanguage.ZH)
    assert "Chinese (zh)" in hint
    assert "<RESPONSE_LANGUAGE_HINT>" in hint


def test_build_response_language_hint_fallback_for_other() -> None:
    fallback = SystemTemplates.RESPONSE_LANGUAGE_HINT_FALLBACK
    assert SystemTemplates.build_response_language_hint(ResponseLanguage.OTHER) == fallback
    assert SystemTemplates.build_response_language_hint(None) == fallback


def test_pick_generic_chitchat_fallback_uses_structured_language() -> None:
    from soothe.sloop.intention.chitchat_fallbacks import (
        GENERIC_CHITCHAT_FALLBACKS_ZH,
        pick_generic_chitchat_fallback,
    )

    reply = pick_generic_chitchat_fallback(ResponseLanguage.ZH)
    assert reply in GENERIC_CHITCHAT_FALLBACKS_ZH
