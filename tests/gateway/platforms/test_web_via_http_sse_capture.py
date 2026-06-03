"""Unit tests for the lead-capture sentinel in web_via_http_sse.

The web widget renders its contact form when it receives a `capture-prompt`
SSE event. The agent requests that form by embedding the CAPTURE_LEAD sentinel
in its reply; send() strips the marker (so the user never sees it) and emits
the event. These tests pin the pure stripping logic.
"""
from __future__ import annotations

from gateway.platforms.web_via_http_sse import _CAPTURE_SENTINEL, _extract_capture


def test_no_sentinel_passes_through():
    assert _extract_capture("hola, en que te ayudo?") == ("hola, en que te ayudo?", False)


def test_sentinel_strips_and_flags():
    text, capture = _extract_capture(f"Perfecto, te paso el formulario. {_CAPTURE_SENTINEL}")
    assert capture is True
    assert _CAPTURE_SENTINEL not in text
    assert text == "Perfecto, te paso el formulario."


def test_sentinel_only_yields_empty_text():
    text, capture = _extract_capture(_CAPTURE_SENTINEL)
    assert capture is True
    assert text == ""


def test_none_content_is_safe():
    assert _extract_capture(None) == ("", False)


def test_multiple_sentinels_all_removed():
    text, capture = _extract_capture(f"{_CAPTURE_SENTINEL}uno{_CAPTURE_SENTINEL}")
    assert capture is True
    assert _CAPTURE_SENTINEL not in text
    assert text == "uno"
