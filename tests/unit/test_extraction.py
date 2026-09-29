"""Slip extraction: the image, the one model call, and parsing what comes back (SPEC.md 6.3, 9.6).

The Qwen replies are hand-written to the schema (see tests/fixtures/qwen/README.md): they are
not real model output.
"""
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image
from pydantic import SecretStr

from parlaytracker.ingest import extraction as ex
from parlaytracker.ingest.extraction import ExtractionError, Extractor

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "qwen"
KEY = "sk-or-test-key-0123456789"


def reply(name: str) -> str:
    return (FIXTURES / name).read_text()


def image_bytes(size=(400, 300), fmt="PNG", mode="RGB", **save) -> bytes:
    out = io.BytesIO()
    Image.new(mode, size, "white").save(out, fmt, **save)
    return out.getvalue()


# --- The image ------------------------------------------------------------------------------


@pytest.mark.parametrize(("fmt", "mime"), [
    ("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp")])
def test_png_jpg_and_webp_are_accepted(fmt, mime):
    prepared = ex.prepare_image(image_bytes(fmt=fmt))
    assert prepared.mime == mime and (prepared.width, prepared.height) == (400, 300)
    assert prepared.data_uri().startswith(f"data:{mime};base64,")


def test_the_type_comes_from_the_bytes_not_the_name():
    for bad in (b"GIF89a....", b"%PDF-1.7 ...", b"<html>not an image</html>", b""):
        with pytest.raises(ExtractionError, match="png, jpg or webp"):
            ex.prepare_image(bad)


def test_a_truncated_image_is_refused_politely():
    with pytest.raises(ExtractionError, match="readable image"):
        ex.prepare_image(image_bytes()[:200])


def test_over_8_mb_is_refused():
    with pytest.raises(ExtractionError, match="8 MB"):
        ex.prepare_image(b"\x89PNG\r\n\x1a\n" + b"0" * ex.MAX_BYTES)


def test_the_size_limit_is_8_mb_exactly():
    assert ex.MAX_BYTES == 8 * 1024 * 1024


@pytest.mark.parametrize(("size", "expected"), [
    ((3000, 1500), (2000, 1000)),  # landscape: the width is the longest side
    ((1200, 4000), (600, 2000)),   # a tall phone screenshot
    ((2000, 2000), (2000, 2000)),  # exactly at the limit: untouched
    ((800, 600), (800, 600)),
])
def test_the_longest_side_is_at_most_2000_px_keeping_the_shape(size, expected):
    prepared = ex.prepare_image(image_bytes(size))
    assert (prepared.width, prepared.height) == expected
    assert Image.open(io.BytesIO(prepared.data)).size == expected  # the bytes agree


def test_a_photo_rotated_by_exif_is_turned_upright():
    out = io.BytesIO()
    image = Image.new("RGB", (400, 200), "white")
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 270: the camera was held sideways
    image.save(out, "JPEG", exif=exif)
    prepared = ex.prepare_image(out.getvalue())
    assert (prepared.width, prepared.height) == (200, 400)


def test_a_png_with_transparency_and_a_jpeg_of_another_mode_are_accepted():
    assert ex.prepare_image(image_bytes(mode="RGBA")).mime == "image/png"
    out = io.BytesIO()
    Image.new("CMYK", (50, 50)).save(out, "JPEG")
    assert ex.prepare_image(out.getvalue()).mime == "image/jpeg"


def test_a_decompression_bomb_is_refused(monkeypatch):
    monkeypatch.setattr(ex, "MAX_PIXELS", 10_000)
    with pytest.raises(ExtractionError, match="too large"):
        ex.prepare_image(image_bytes((200, 200)))


# --- The request ----------------------------------------------------------------------------


def test_the_request_is_the_one_call_the_spec_describes():
    prepared = ex.prepare_image(image_bytes())
    request = ex.build_request(prepared, "qwen/qwen3-vl-32b-instruct")
    assert request["model"] == "qwen/qwen3-vl-32b-instruct"
    assert request["temperature"] == 0 and request["timeout"] == 45
    assert request["response_format"]["type"] == "json_schema"
    assert request["response_format"]["json_schema"]["schema"]["title"] == "ExtractedSlip"
    (message,) = request["messages"]  # a single user message
    assert message["role"] == "user"
    image, text = message["content"]
    assert image == {"type": "image_url", "image_url": {"url": prepared.data_uri()}}
    prompt = text["text"]
    for rule in ("Do not guess", "null", "signed integers", "only JSON"):
        assert rule in prompt
    assert '"legs"' in prompt  # the schema itself is in the prompt


# --- Parsing what comes back ----------------------------------------------------------------


def test_a_valid_single():
    slip = ex.parse_reply(reply("single_valid.json"))
    assert (slip.sportsbook_text, slip.american_odds, slip.stake) == ("DraftKings", -115, 10.0)
    (leg,) = slip.legs
    assert (leg.player_name, leg.market_text, leg.line, leg.american_odds) == (
        "Trey McBride", "Receiving Yards", 70.5, -115)


def test_a_valid_sgp_has_all_three_legs():
    slip = ex.parse_reply(reply("sgp_valid.json"))
    assert [leg.market_text for leg in slip.legs] == ["Receptions", "Total Points", "Alt Spread"]
    assert slip.legs[2].line == -3.5 and slip.legs[2].team_text == "49ers"


def test_a_unicode_minus_and_string_numbers_are_understood():
    slip = ex.parse_reply(reply("unicode_minus.json"))
    assert (slip.american_odds, slip.stake) == (-110, 25.0)
    (leg,) = slip.legs
    assert (leg.line, leg.american_odds) == (-3.5, 150)


def test_a_partial_reply_keeps_what_was_read():
    slip = ex.parse_reply(reply("partial.json"))
    assert slip.sportsbook_text is None and slip.american_odds is None
    (leg,) = slip.legs
    assert (leg.team_text, leg.line, leg.market_text) == ("Bears", 44.5, None)


def test_wrong_typed_fields_are_dropped_and_the_rest_survives():
    slip = ex.parse_reply(reply("bad_fields.json"))
    assert slip.sportsbook_text is None and slip.american_odds is None and slip.stake is None
    (leg,) = slip.legs  # the junk entries and the all-null leg are gone
    assert (leg.player_name, leg.market_text) == ("Trey McBride", "Receptions")
    assert (leg.line, leg.american_odds) == (None, None)  # "five and a half", 110.5


def test_json_in_a_code_fence_with_chatter_is_found():
    slip = ex.parse_reply(reply("fenced.txt"))
    assert slip.legs[0].market_text == "Game Total" and slip.american_odds == -110


def test_bare_json_with_a_fence_but_no_chatter():
    assert ex.parse_reply('```json\n{"american_odds": 150}\n```').american_odds == 150
    assert ex.parse_reply('```\n{"american_odds": 150}\n```').american_odds == 150


@pytest.mark.parametrize("text", [
    reply("malformed.txt"), "", "   ", "[]", "42", '"just a string"', '{"legs": ',
    reply("empty.json"), '{"legs": [{}], "stake": null}'])
def test_nothing_usable_is_an_error_with_a_message_safe_to_show(text):
    with pytest.raises(ExtractionError) as info:
        ex.parse_reply(text)
    assert str(info.value) and "sk-" not in str(info.value)


def test_odd_odds_are_not_accepted_as_integers():
    slip = ex.parse_reply('{"legs": [{"market_text": "x", "american_odds": "+150.5"}]}')
    assert slip.legs[0].american_odds is None
    with pytest.raises(ExtractionError):  # a boolean is not a number, so nothing was read
        ex.parse_reply('{"american_odds": true}')


# --- The call -------------------------------------------------------------------------------


class FakeCompletions:
    def __init__(self, content=None, error=None, choices=None):
        self.content, self.error, self.choices = content, error, choices
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        choices = self.choices if self.choices is not None else [
            SimpleNamespace(message=SimpleNamespace(content=self.content))]
        return SimpleNamespace(choices=choices)


def extractor(**kwargs) -> tuple[Extractor, FakeCompletions]:
    completions = FakeCompletions(**kwargs)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return Extractor(client, "qwen/qwen3-vl-32b-instruct"), completions


IMAGE = ex.prepare_image(image_bytes())


def test_one_call_per_screenshot_and_the_reply_is_parsed():
    e, completions = extractor(content=reply("single_valid.json"))
    slip = e.extract(IMAGE)
    assert len(completions.calls) == 1
    assert completions.calls[0]["model"] == "qwen/qwen3-vl-32b-instruct"
    assert slip.legs[0].player_name == "Trey McBride"


@pytest.mark.parametrize("kwargs", [
    {"content": reply("malformed.txt")},
    {"content": reply("empty.json")},
    {"content": None},
    {"content": ""},
    {"choices": []},
    {"error": TimeoutError("slow")},
    {"error": ConnectionError("down")},
    {"error": RuntimeError("anything at all")},
])
def test_every_failure_is_an_extraction_error(kwargs):
    e, _ = extractor(**kwargs)
    with pytest.raises(ExtractionError):
        e.extract(IMAGE)


def test_an_error_carrying_the_key_never_leaks_it(caplog):
    e, _ = extractor(error=RuntimeError(f"401 for key {KEY}"))
    with caplog.at_level("DEBUG"):
        with pytest.raises(ExtractionError) as info:
            e.extract(IMAGE)
    assert KEY not in str(info.value) and KEY not in caplog.text
    assert info.value.__cause__ is None and info.value.__suppress_context__ is not False


def test_an_http_status_is_logged_but_nothing_else(caplog):
    error = RuntimeError("body with secrets")
    error.status_code = 429
    e, _ = extractor(error=error)
    with caplog.at_level("WARNING"):
        with pytest.raises(ExtractionError):
            e.extract(IMAGE)
    assert "429" in caplog.text and "secrets" not in caplog.text


def test_without_a_key_there_is_no_extractor():
    assert Extractor.from_settings(None, "https://openrouter.ai/api/v1", "m") is None


def test_with_a_key_the_openai_client_points_at_openrouter():
    e = Extractor.from_settings(SecretStr(KEY), "https://openrouter.ai/api/v1", "m")
    client = e._client
    assert str(client.base_url).startswith("https://openrouter.ai/api/v1")
    assert client.api_key == KEY


def test_the_schema_in_the_request_is_valid_json():
    request = ex.build_request(IMAGE, "m")
    json.dumps(request["response_format"])  # the SDK must be able to serialise it
