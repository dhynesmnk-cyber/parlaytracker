"""Reading a slip screenshot with Qwen (SPEC.md sections 6.3 and 9.6).

One call per screenshot, through OpenRouter's OpenAI-compatible API. The result is parsed
leniently into `ExtractedSlip` (everything optional) and never goes to the database: it only
pre-fills the form, and a person presses Save (constraint 1).

Nothing here logs or raises the API key, the image, or the model's raw reply.
"""
import base64
import io
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Protocol

from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import SecretStr, TypeAdapter, ValidationError

from parlaytracker.core.schemas import ExtractedLeg, ExtractedSlip

log = logging.getLogger("parlaytracker.extraction")

MAX_BYTES = 8 * 1024 * 1024
MAX_SIDE = 2000
MAX_PIXELS = 60_000_000  # a guard against decompression bombs, far above any phone screenshot
TIMEOUT_SECONDS = 45
CANT_READ = "Couldn't read this slip. Enter it manually."

_SIGNATURES = (  # (magic bytes, mime, PIL format)
    (b"\x89PNG\r\n\x1a\n", "image/png", "PNG"),
    (b"\xff\xd8\xff", "image/jpeg", "JPEG"),
)


class ExtractionError(Exception):
    """The slip couldn't be read. `str(e)` is safe to show and log."""


@dataclass(frozen=True)
class PreparedImage:
    data: bytes
    mime: str
    width: int
    height: int

    def data_uri(self) -> str:
        return f"data:{self.mime};base64,{base64.b64encode(self.data).decode()}"


def _sniff(data: bytes) -> tuple[str, str]:
    """The real type from the bytes, not the file name."""
    for magic, mime, fmt in _SIGNATURES:
        if data.startswith(magic):
            return mime, fmt
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", "WEBP"
    raise ExtractionError("Upload a png, jpg or webp image.")


def prepare_image(data: bytes) -> PreparedImage:
    """Check the upload and downscale it so the longest side is at most 2000 px (section 9.6)."""
    if len(data) > MAX_BYTES:
        raise ExtractionError(f"That image is over {MAX_BYTES // 1024 // 1024} MB.")
    mime, fmt = _sniff(data)
    try:
        image = Image.open(io.BytesIO(data))
        if image.width * image.height > MAX_PIXELS:
            raise ExtractionError("That image is too large to read.")
        image = ImageOps.exif_transpose(image)  # phone photos carry their rotation in EXIF
        image.load()
    except ExtractionError:
        raise
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, SyntaxError) as e:
        raise ExtractionError("That file isn't a readable image.") from e
    if max(image.size) > MAX_SIDE:
        image.thumbnail((MAX_SIDE, MAX_SIDE), Image.Resampling.LANCZOS)
    if fmt == "JPEG" and image.mode not in ("RGB", "L"):
        image = image.convert("RGB")
    out = io.BytesIO()
    options = {"PNG": {"optimize": True}, "JPEG": {"quality": 90}, "WEBP": {"quality": 90}}[fmt]
    image.save(out, fmt, **options)
    return PreparedImage(out.getvalue(), mime, image.width, image.height)


# --- The model call ----------------------------------------------------------------------------

PROMPT = """You are reading a screenshot of a sports betting slip. Transcribe exactly what is \
printed on it.

Rules:
- Do not guess. Use null for anything that is not visible.
- Give odds as signed integers, for example -110 or 150. Use a plain minus sign.
- One entry in "legs" per selection. "market_text" is the market wording as printed, for \
example "Receiving Yards" or "Alt Spread", and include "Over" or "Under" if it is shown.
- "line" is the number as printed, for example 5.5 or -7.5.
- "event_text" is the game as printed, for example "PHI @ CHI"; "team_text" is the team the \
selection is on, when it is a team market.
- "sportsbook_text" is the sportsbook's name as printed.
- Return only JSON that matches this schema, with no other text:

"""


class ChatClient(Protocol):
    """The part of the `openai` client used here; tests supply a fake."""

    chat: Any


def _schema_format() -> dict[str, Any]:
    return {"type": "json_schema",
            "json_schema": {"name": "ExtractedSlip", "schema": ExtractedSlip.model_json_schema()}}


def build_request(image: PreparedImage, model: str) -> dict[str, Any]:
    """The one call's arguments (section 6.3)."""
    prompt = PROMPT + json.dumps(ExtractedSlip.model_json_schema())
    return {
        "model": model,
        "temperature": 0,
        "timeout": TIMEOUT_SECONDS,
        "response_format": _schema_format(),
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": image.data_uri()}},
            {"type": "text", "text": prompt},
        ]}],
    }


# --- Parsing -----------------------------------------------------------------------------------

_FENCE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")
_NUMBER = re.compile(r"[+-]?\d+(?:\.\d+)?")


def _json_from(reply: str) -> Any:
    """The JSON in a reply: bare, wrapped in a code fence, or with chatter around it."""
    text = _FENCE.sub("", reply.strip()).strip()
    try:
        return json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise
        return json.loads(text[start:end + 1])


def _number(value: Any) -> float | None:
    """-110, "-110", "+150", "−110" (a Unicode minus) and "5.5" all read as numbers."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        match = _NUMBER.search(value.replace("−", "-").replace("–", "-").replace(",", ""))
        return float(match.group()) if match else None
    return None


def _clean_field(model: type, name: str, value: Any) -> Any:
    """One field, coerced to its type; None when it can't be (the rest of the slip survives)."""
    if value is None:
        return None
    if name == "american_odds":
        number = _number(value)
        return None if number is None or number != int(number) else int(number)
    if name in ("line", "stake", "potential_payout"):
        return _number(value)
    try:
        return TypeAdapter(model.model_fields[name].annotation).validate_python(value)
    except ValidationError:
        return None


def _clean(model: type, raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    return {name: _clean_field(model, name, raw.get(name))
            for name in model.model_fields if name != "legs" and name in raw}


def parse_reply(reply: str) -> ExtractedSlip:
    """A model reply as an `ExtractedSlip`; raises ExtractionError if nothing is usable."""
    try:
        raw = _json_from(reply)
    except ValueError as e:
        raise ExtractionError("The reply wasn't JSON.") from e
    if not isinstance(raw, dict):
        raise ExtractionError("The reply wasn't a slip.")
    cleaned = (_clean(ExtractedLeg, item) for item in (raw.get("legs") or []))
    legs = [ExtractedLeg(**c) for c in cleaned if any(v is not None for v in c.values())]
    slip = ExtractedSlip(**_clean(ExtractedSlip, raw), legs=legs)
    if not legs and all(getattr(slip, n) is None for n in ExtractedSlip.model_fields
                        if n != "legs"):
        raise ExtractionError("Nothing on the slip could be read.")
    return slip


# --- The extractor -----------------------------------------------------------------------------


class Extractor:
    def __init__(self, client: ChatClient, model: str):
        self._client = client
        self._model = model

    @classmethod
    def from_settings(cls, api_key: SecretStr | None, base_url: str, model: str
                      ) -> "Extractor | None":
        """None when there is no key: the Screenshot page then says so and shows the form."""
        if api_key is None:
            return None
        from openai import OpenAI

        return cls(OpenAI(api_key=api_key.get_secret_value(), base_url=base_url), model)

    def extract(self, image: PreparedImage) -> ExtractedSlip:
        """One call. Any failure is an ExtractionError with nothing sensitive in it."""
        try:
            response = self._client.chat.completions.create(**build_request(image, self._model))
            reply = response.choices[0].message.content or ""
        except ExtractionError:
            raise
        except Exception as e:  # the SDK's errors, timeouts, an unexpected response shape
            status = getattr(e, "status_code", None)
            log.warning("slip extraction failed: %s%s", type(e).__name__,
                        f" ({status})" if status else "")
            raise ExtractionError(f"The model call failed ({type(e).__name__}).") from None
        return parse_reply(reply)
