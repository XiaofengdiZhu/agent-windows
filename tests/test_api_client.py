"""Tests for utils/api.py: client caching, engine dispatch, env-key fallback,
and the encode_image branches NOT already covered by test_api.py.

test_api.py already covers: encode_image default-JPEG / PNG / RGBA-flatten,
and _resolve_image_format default/png/invalid. This file adds:
- encode_image: max_size downscale, JPG alias, palette-mode flatten, JPEG quality
- _client_cache: same key reuses client, different keys create new clients
- call_vision_model: engine routing (openai/anthropic/gemini/unknown→openai)
- env-key fallback when config api_key is empty
"""

import base64
import io
from unittest.mock import patch, MagicMock

import pytest
from PIL import Image

import agent_windows.utils.api as api
from agent_windows.utils.api import (
    encode_image,
    encode_image_bytes,
    call_vision_model,
)


def _decode(b64: str) -> Image.Image:
    raw = base64.b64decode(b64)
    img = Image.open(io.BytesIO(raw))
    img.load()
    return img


# ── encode_image: branches beyond test_api.py ────────────────────────


class TestEncodeImageScaling:
    def test_max_size_downscales_large_dimension(self):
        """An image exceeding max_size is resized so the longer side == max_size."""
        img = Image.new("RGB", (1000, 500), (10, 20, 30))
        decoded = _decode(encode_image(img, max_size=200))
        assert decoded.width == 200
        assert decoded.height == 100  # aspect ratio preserved

    def test_max_size_no_resize_when_within(self):
        """An image already within max_size is not resized."""
        img = Image.new("RGB", (50, 50), (1, 2, 3))
        decoded = _decode(encode_image(img, max_size=200))
        assert decoded.size == (50, 50)

    def test_max_size_uses_shorter_axis_when_both_exceed(self):
        """When both dims exceed max_size, scale = min ratio (fit within box)."""
        img = Image.new("RGB", (800, 400), (0, 0, 0))
        decoded = _decode(encode_image(img, max_size=100))
        # scale = min(100/800, 100/400) = 0.125 → 100x50
        assert decoded.size == (100, 50)

    def test_max_size_zero_means_no_resize(self):
        img = Image.new("RGB", (5000, 5000), (0, 0, 0))
        decoded = _decode(encode_image(img, max_size=0))
        assert decoded.size == (5000, 5000)


class TestEncodeImageFormatAlias:
    def test_jpg_alias_treated_as_jpeg(self):
        img = Image.new("RGB", (10, 10), (0, 0, 0))
        assert _decode(encode_image(img, format="JPG")).format == "JPEG"

    def test_lowercase_format_accepted(self):
        img = Image.new("RGB", (10, 10), (0, 0, 0))
        assert _decode(encode_image(img, format="jpeg")).format == "JPEG"

    def test_palette_mode_flattened_for_jpeg(self):
        """P (palette) mode can't go to JPEG directly — must convert to RGB."""
        img = Image.new("P", (10, 10))
        decoded = _decode(encode_image(img, format="JPEG"))
        assert decoded.mode == "RGB"
        assert decoded.format == "JPEG"

    def test_l_mode_passes_through_jpeg(self):
        """L (grayscale) is JPEG-native and should NOT be converted to RGB."""
        img = Image.new("L", (10, 10), 128)
        decoded = _decode(encode_image(img, format="JPEG"))
        assert decoded.mode == "L"

    def test_jpeg_quality_affects_size(self):
        """Higher quality → larger bytes. Proves the quality kwarg is wired."""
        img = Image.new("RGB", (100, 100))
        # gradient so JPEG has something to compress
        for x in range(100):
            for y in range(100):
                img.putpixel((x, y), (x * 2 % 256, y * 2 % 256, (x + y) % 256))
        low = len(base64.b64decode(encode_image(img, quality=10)))
        high = len(base64.b64decode(encode_image(img, quality=95)))
        assert high > low


# ── call_vision_model: engine routing ────────────────────────────────


class TestEngineRouting:
    """call_vision_model dispatches by engine name. Each backend is patched so
    no real network/SDK import happens."""

    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        """Each test starts with an empty client cache."""
        api._client_cache.clear()
        yield
        api._client_cache.clear()

    def test_openai_engine_routes_to_openai_backend(self):
        with patch.object(api, "_call_openai_compatible", return_value="ok-openai") as m_o, \
             patch.object(api, "_call_anthropic") as m_a, \
             patch.object(api, "_call_gemini") as m_g:
            result = call_vision_model(Image.new("RGB", (4, 4)), "p", {"engine": "openai"})
        assert result == "ok-openai"
        m_o.assert_called_once()
        m_a.assert_not_called()
        m_g.assert_not_called()

    def test_doubao_routes_to_openai_backend(self):
        """'doubao' is OpenAI-compatible and shares the openai path."""
        with patch.object(api, "_call_openai_compatible", return_value="ok") as m:
            call_vision_model(Image.new("RGB", (4, 4)), "p", {"engine": "doubao"})
        m.assert_called_once()

    def test_gemini_compatible_routes_to_openai_backend(self):
        """'gemini_compatible' is a third OpenAI-routed alias."""
        with patch.object(api, "_call_openai_compatible", return_value="ok") as m:
            call_vision_model(Image.new("RGB", (4, 4)), "p", {"engine": "gemini_compatible"})
        m.assert_called_once()

    def test_anthropic_engine_routes(self):
        with patch.object(api, "_call_anthropic", return_value="ok-claude") as m_a, \
             patch.object(api, "_call_openai_compatible") as m_o:
            assert call_vision_model(Image.new("RGB", (4, 4)), "p", {"engine": "anthropic"}) == "ok-claude"
        m_o.assert_not_called()

    def test_gemini_engine_routes(self):
        with patch.object(api, "_call_gemini", return_value="ok-gem") as m:
            assert call_vision_model(Image.new("RGB", (4, 4)), "p", {"engine": "gemini"}) == "ok-gem"

    def test_unknown_engine_defaults_to_openai(self):
        with patch.object(api, "_call_openai_compatible", return_value="ok") as m:
            call_vision_model(Image.new("RGB", (4, 4)), "p", {"engine": "totally-unknown"})
        m.assert_called_once()

    def test_engine_case_insensitive(self):
        with patch.object(api, "_call_openai_compatible", return_value="ok") as m:
            call_vision_model(Image.new("RGB", (4, 4)), "p", {"engine": "OpenAI"})
        m.assert_called_once()

    def test_no_engine_defaults_to_openai(self):
        with patch.object(api, "_call_openai_compatible", return_value="ok") as m:
            call_vision_model(Image.new("RGB", (4, 4)), "p", {})
        m.assert_called_once()


# ── _client_cache: dedup + keying ────────────────────────────────────


class TestClientCache:
    """The OpenAI-compatible backend lazily imports openai.OpenAI and caches the
    client keyed by (api_base, api_key). Same key → reuse; different → new."""

    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        api._client_cache.clear()
        yield
        api._client_cache.clear()

    def test_same_key_reuses_client(self):
        """Two calls with identical api_base+api_key create the OpenAI client once."""
        fake_client = MagicMock()
        fake_client.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content="  reply  "))]
        )
        with patch("agent_windows.utils.api.encode_image", return_value="b64"), \
             patch("openai.OpenAI", return_value=fake_client) as mock_openai:
            cfg = {"api_key": "k", "api_base": "http://x", "model": "m"}
            r1 = call_vision_model(Image.new("RGB", (4, 4)), "p", cfg)
            r2 = call_vision_model(Image.new("RGB", (4, 4)), "p", cfg)
        assert mock_openai.call_count == 1  # client built once
        assert r1 == "reply" and r2 == "reply"
        # cache now holds the client under the openai key (includes timeout, G19b)
        assert "openai:http://x:k:30" in api._client_cache

    def test_different_api_base_creates_new_client(self):
        fake_client = MagicMock()
        fake_client.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content="r"))]
        )
        with patch("agent_windows.utils.api.encode_image", return_value="b64"), \
             patch("openai.OpenAI", return_value=fake_client) as mock_openai:
            call_vision_model(Image.new("RGB", (4, 4)), "p", {"api_base": "http://a", "api_key": "k"})
            call_vision_model(Image.new("RGB", (4, 4)), "p", {"api_base": "http://b", "api_key": "k"})
        assert mock_openai.call_count == 2

    def test_different_api_key_creates_new_client(self):
        fake_client = MagicMock()
        fake_client.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content="r"))]
        )
        with patch("agent_windows.utils.api.encode_image", return_value="b64"), \
             patch("openai.OpenAI", return_value=fake_client) as mock_openai:
            call_vision_model(Image.new("RGB", (4, 4)), "p", {"api_key": "k1"})
            call_vision_model(Image.new("RGB", (4, 4)), "p", {"api_key": "k2"})
        assert mock_openai.call_count == 2

    def test_falls_back_to_env_api_key_when_config_empty(self, monkeypatch):
        """An empty/absent config api_key falls back to the env var."""
        monkeypatch.setenv("AGENT_WINDOWS_VLM_API_KEY", "env-key")
        fake_client = MagicMock()
        fake_client.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content="r"))]
        )
        with patch("agent_windows.utils.api.encode_image", return_value="b64"), \
             patch("openai.OpenAI", return_value=fake_client) as mock_openai:
            call_vision_model(Image.new("RGB", (4, 4)), "p", {"api_base": "http://x"})  # no api_key
        # the cached client's key reflects the env fallback (+ timeout, G19b)
        assert "openai:http://x:env-key:30" in api._client_cache
        mock_openai.assert_called_once()
        # and the KEY actually reaches the OpenAI constructor (not "" / None)
        assert mock_openai.call_args.kwargs["api_key"] == "env-key"

    def test_openai_request_shape_and_defaults(self):
        """A config with no model/timeout still wires sensible defaults into the
        chat.completions.create call: model, temperature=0.0, max_tokens, and the
        client timeout plumbed into OpenAI(...)."""
        fake_client = MagicMock()
        fake_client.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content="r"))]
        )
        with patch("agent_windows.utils.api.encode_image", return_value="b64"), \
             patch("openai.OpenAI", return_value=fake_client) as mock_openai:
            call_vision_model(Image.new("RGB", (4, 4)), "the prompt",
                              {"api_key": "k", "api_base": "http://x"},
                              max_tokens=100)
        # client timeout defaults to 30s when config omits it
        assert mock_openai.call_args.kwargs["timeout"] == 30.0
        create_kwargs = fake_client.chat.completions.create.call_args.kwargs
        assert create_kwargs["model"] == "gpt-4o-mini"  # default model
        assert create_kwargs["temperature"] == 0.0
        assert create_kwargs["max_tokens"] == 100
        # the prompt text reaches the first content block
        content = create_kwargs["messages"][0]["content"]
        assert content[0] == {"type": "text", "text": "the prompt"}

    def test_anthropic_client_cached_across_calls(self):
        """The anthropic backend caches its client keyed by api_key. anthropic
        isn't installed, so inject a fake module into sys.modules at the lazy
        import site."""
        import sys
        fake_module = MagicMock()
        fake_client = MagicMock()
        fake_client.messages.create.return_value = MagicMock(
            content=[MagicMock(text="  ok  ")]
        )
        fake_module.Anthropic.return_value = fake_client
        sys.modules["anthropic"] = fake_module
        try:
            with patch("agent_windows.utils.api.encode_image", return_value="b64"):
                cfg = {"engine": "anthropic", "api_key": "ck", "model": "claude-x"}
                r1 = call_vision_model(Image.new("RGB", (4, 4)), "p", cfg)
                r2 = call_vision_model(Image.new("RGB", (4, 4)), "p", cfg)
            assert fake_module.Anthropic.call_count == 1  # client built once
            assert r1 == "ok" and r2 == "ok"
            assert "anthropic:ck:30" in api._client_cache  # + timeout (G19b)
        finally:
            sys.modules.pop("anthropic", None)

    def test_gemini_client_cached_across_calls(self):
        """The gemini backend caches the new-SDK Client keyed by api_key (NOT by
        model — the Client is model-agnostic, so the model is passed per-call to
        client.models.generate_content). google-genai isn't installed, so inject
        a fake (sub)module. This mirrors the openai/anthropic isolation design:
        the api_key is bound into the cached Client object, so there is no
        process-global configure() state (D1)."""
        import sys
        fake_client = MagicMock()
        fake_client.models.generate_content.return_value = MagicMock(text="  g  ")
        fake_genai = MagicMock()
        fake_genai.Client.return_value = fake_client
        # google is a namespace package; register both the parent and the leaf
        # submodule so `from google import genai` resolves. Point the parent's
        # attribute at the same fake so the import returns the right object.
        fake_google = MagicMock()
        fake_google.genai = fake_genai
        sys.modules["google"] = fake_google
        sys.modules["google.genai"] = fake_genai
        try:
            with patch("agent_windows.utils.api.encode_image", return_value="b64"):
                cfg = {"engine": "gemini", "api_key": "gk", "model": "gemini-x"}
                r1 = call_vision_model(Image.new("RGB", (4, 4)), "p", cfg)
                r2 = call_vision_model(Image.new("RGB", (4, 4)), "p", cfg)
            # Client built once (cached), keyed by api_key + timeout — model is
            # per-call (G19b added timeout to the key).
            assert fake_genai.Client.call_count == 1
            assert r1 == "g" and r2 == "g"
            assert "gemini:gk:30" in api._client_cache
            # The model is passed per-call, not baked into the cached object, so
            # switching vlm.model does NOT rebuild the client.
            assert fake_genai.Client.call_args.kwargs["api_key"] == "gk"
            # Each generate_content call carries the model name.
            calls = fake_client.models.generate_content.call_args_list
            assert all(c.kwargs["model"] == "gemini-x" for c in calls)
        finally:
            sys.modules.pop("google", None)
            sys.modules.pop("google.genai", None)

    def test_gemini_client_re_keys_on_different_api_key(self):
        """D1: a different api_key builds a distinct cached Client — the key is
        bound into the object, so two keys never share global state. (The old
        genai.configure() global singleton would have let the second key's
        configure overwrite the first's default credentials.)"""
        import sys
        fake_client_a = MagicMock()
        fake_client_a.models.generate_content.return_value = MagicMock(text="a")
        fake_client_b = MagicMock()
        fake_client_b.models.generate_content.return_value = MagicMock(text="b")
        fake_genai = MagicMock()
        fake_genai.Client.side_effect = [fake_client_a, fake_client_b]
        fake_google = MagicMock()
        fake_google.genai = fake_genai
        sys.modules["google"] = fake_google
        sys.modules["google.genai"] = fake_genai
        try:
            with patch("agent_windows.utils.api.encode_image", return_value="b64"):
                ra = call_vision_model(Image.new("RGB", (4, 4)), "p",
                                       {"engine": "gemini", "api_key": "keyA", "model": "m"})
                rb = call_vision_model(Image.new("RGB", (4, 4)), "p",
                                       {"engine": "gemini", "api_key": "keyB", "model": "m"})
            assert ra == "a" and rb == "b"
            assert fake_genai.Client.call_count == 2  # two distinct clients
            assert "gemini:keyA:30" in api._client_cache  # + timeout (G19b)
            assert "gemini:keyB:30" in api._client_cache
            # Each key's call carried its own api_key into Client construction.
            keys_used = [c.kwargs["api_key"] for c in fake_genai.Client.call_args_list]
            assert keys_used == ["keyA", "keyB"]
        finally:
            sys.modules.pop("google", None)
            sys.modules.pop("google.genai", None)


# ── G18: empty / filtered model response → RuntimeError ─────────────


class TestEmptyResponseGuard:
    """G18: the project supports small models that emit empty or content-filtered
    responses (e.g. xopqwen36v35b). The three backends each indexed/strip'd the
    response with no guard, so a None/empty content crashed with the SDK's native
    exception (AttributeError / IndexError / ValueError) instead of the
    docstring-promised RuntimeError. On the daemon side that AttributeError leaked
    to the generic ``except Exception`` (DAEMON_INTERNAL_ERROR) while a Gemini
    ``ValueError``/Anthropic ``IndexError`` was mis-mapped to INVALID_PARAMS — so
    wrapping each as RuntimeError also stops the wrong-code mapping. (G18)

    Each test injects a fake client whose ``create``/``generate_content`` returns
    an empty/filtered response, and asserts ``call_vision_model`` raises
    ``RuntimeError`` (not the native crash).
    """

    def test_openai_none_content_raises_runtime_error(self):
        """OpenAI content can be None when the model returns only a tool call or
        a content-filter finish. ``content.strip()`` → AttributeError without a
        guard."""
        fake_client = MagicMock()
        fake_client.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content=None))]
        )
        with patch("agent_windows.utils.api.encode_image", return_value="b64"), \
             patch("openai.OpenAI", return_value=fake_client):
            with pytest.raises(RuntimeError, match="(?i)empty|response"):
                call_vision_model(
                    Image.new("RGB", (4, 4)), "p",
                    {"api_key": "k", "api_base": "http://x", "model": "m"})

    def test_openai_empty_content_raises_runtime_error(self):
        """An empty-string content (whitespace-only after strip) is also empty."""
        fake_client = MagicMock()
        fake_client.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content="   "))]
        )
        with patch("agent_windows.utils.api.encode_image", return_value="b64"), \
             patch("openai.OpenAI", return_value=fake_client):
            with pytest.raises(RuntimeError):
                call_vision_model(
                    Image.new("RGB", (4, 4)), "p",
                    {"api_key": "k", "api_base": "http://x", "model": "m"})

    def test_openai_empty_choices_raises_runtime_error(self):
        """Some OpenAI-compatible providers return choices=[] (e.g. a filtered
        finish). choices[0] → IndexError without a guard; must be RuntimeError."""
        fake_client = MagicMock()
        fake_client.chat.completions.create.return_value = MagicMock(choices=[])
        with patch("agent_windows.utils.api.encode_image", return_value="b64"), \
             patch("openai.OpenAI", return_value=fake_client):
            with pytest.raises(RuntimeError, match="(?i)empty|response"):
                call_vision_model(
                    Image.new("RGB", (4, 4)), "p",
                    {"api_key": "k", "api_base": "http://x", "model": "m"})

    def test_anthropic_empty_content_raises_runtime_error(self):
        """Anthropic content can be [] when the response is filtered. ``content[0]``
        → IndexError without a guard."""
        import sys
        fake_module = MagicMock()
        fake_client = MagicMock()
        fake_client.messages.create.return_value = MagicMock(content=[])
        fake_module.Anthropic.return_value = fake_client
        sys.modules["anthropic"] = fake_module
        try:
            with patch("agent_windows.utils.api.encode_image", return_value="b64"):
                with pytest.raises(RuntimeError, match="(?i)empty|response"):
                    call_vision_model(
                        Image.new("RGB", (4, 4)), "p",
                        {"engine": "anthropic", "api_key": "ck", "model": "claude-x"})
        finally:
            sys.modules.pop("anthropic", None)

    def test_gemini_no_text_raises_runtime_error(self):
        """Gemini ``response.text`` raises ValueError when there are no text parts
        (e.g. blocked by safety). Without a guard that ValueError mis-maps to
        INVALID_PARAMS on the daemon side. Must become RuntimeError."""
        import sys
        fake_client = MagicMock()
        # .text is a property on the real type; emulate the SDK's ValueError.
        type(fake_client.models.generate_content.return_value).text = (
            property(lambda self: (_ for _ in ()).throw(
                ValueError("response.text quick accessor is only available "
                           "when a single part is present."))))
        fake_genai = MagicMock()
        fake_genai.Client.return_value = fake_client
        fake_google = MagicMock()
        fake_google.genai = fake_genai
        sys.modules["google"] = fake_google
        sys.modules["google.genai"] = fake_genai
        try:
            with patch("agent_windows.utils.api.encode_image", return_value="b64"):
                with pytest.raises(RuntimeError, match="(?i)empty|response"):
                    call_vision_model(
                        Image.new("RGB", (4, 4)), "p",
                        {"engine": "gemini", "api_key": "gk", "model": "gemini-x"})
        finally:
            sys.modules.pop("google", None)
            sys.modules.pop("google.genai", None)

    def test_gemini_empty_text_raises_runtime_error(self):
        """Gemini ``response.text`` can also return an empty string (no text parts
        synthesized). Must raise RuntimeError, not return empty silently."""
        import sys
        fake_client = MagicMock()
        fake_client.models.generate_content.return_value = MagicMock(text="   ")
        fake_genai = MagicMock()
        fake_genai.Client.return_value = fake_client
        fake_google = MagicMock()
        fake_google.genai = fake_genai
        sys.modules["google"] = fake_google
        sys.modules["google.genai"] = fake_genai
        try:
            with patch("agent_windows.utils.api.encode_image", return_value="b64"):
                with pytest.raises(RuntimeError):
                    call_vision_model(
                        Image.new("RGB", (4, 4)), "p",
                        {"engine": "gemini", "api_key": "gk", "model": "gemini-x"})
        finally:
            sys.modules.pop("google", None)
            sys.modules.pop("google.genai", None)


# ── G19: Gemini image encoding + client timeout ─────────────────────


class TestEncodeImageBytes:
    """G19(a): encode_image_bytes returns RAW bytes (not base64) so Gemini can
    feed them to types.Part.from_bytes. It must apply the same resize/flatten/
    quality logic as encode_image — only the final base64 step is skipped."""

    def test_returns_raw_jpeg_bytes(self):
        img = Image.new("RGB", (10, 10), (0, 0, 0))
        raw = encode_image_bytes(img)
        assert isinstance(raw, bytes)
        # The bytes are a valid JPEG (not base64): PIL can reopen them and the
        # format is JPEG. base64 of a JPEG would NOT start with the JPEG SOI magic.
        assert raw[:2] == b"\xff\xd8"  # JPEG SOI marker

    def test_same_quality_as_encode_image(self):
        """encode_image_bytes and encode_image must produce identical underlying
        bytes for the same image/format/quality (encode_image just base64s them)."""
        img = Image.new("RGB", (20, 20), (10, 20, 30))
        raw = encode_image_bytes(img, format="JPEG", quality=85)
        b64 = encode_image(img, format="JPEG", quality=85)
        assert base64.b64decode(b64) == raw

    def test_respects_format_and_quality(self):
        """PNG format and a different quality are honored (parity with encode_image)."""
        img = Image.new("RGB", (30, 30))
        for x in range(30):
            for y in range(30):
                img.putpixel((x, y), (x * 8 % 256, y * 8 % 256, (x + y) % 256))
        # PNG is lossless; two identical inputs → identical bytes
        png1 = encode_image_bytes(img, format="PNG")
        png2 = encode_image_bytes(img, format="PNG")
        assert png1 == png2
        assert png1[:8] == b"\x89PNG\r\n\x1a\n"  # PNG signature

    def test_downscales_max_size(self):
        """max_size resize logic is shared with encode_image."""
        img = Image.new("RGB", (1000, 500), (10, 20, 30))
        raw = encode_image_bytes(img, max_size=200)
        decoded = Image.open(io.BytesIO(raw))
        decoded.load()
        assert decoded.width == 200 and decoded.height == 100


class TestGeminiImageEncoding:
    """G19(a): Gemini must run the image through encode_image_bytes (JPEG
    compression + user image_format/quality) and send it via
    types.Part.from_bytes — NOT the raw PIL image. Before the fix, Gemini passed
    the PIL image straight to generate_content, so H-05/H-08 were lost."""

    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        api._client_cache.clear()
        yield
        api._client_cache.clear()

    def test_gemini_uses_part_from_bytes_not_raw_image(self):
        """The image reaching generate_content must be a Part built from encoded
        bytes, not the PIL image. We capture the contents list and assert."""
        import sys
        captured = {}

        fake_client = MagicMock()
        fake_client.models.generate_content.return_value = MagicMock(text="ok")

        def _fake_from_bytes(data, mime_type=None):
            part = MagicMock(name=f"Part<{mime_type}>")
            captured["data"] = data
            captured["mime_type"] = mime_type
            return part

        fake_types = MagicMock()
        fake_types.Part.from_bytes.side_effect = _fake_from_bytes
        fake_types.GenerateContentConfig = MagicMock()
        # a real-ish HttpOptions (just needs to accept kwargs)
        fake_types.HttpOptions = MagicMock()
        fake_genai = MagicMock()
        fake_genai.Client.return_value = fake_client
        fake_genai.types = fake_types
        fake_google = MagicMock()
        fake_google.genai = fake_genai
        # `from google.genai import types` must resolve to fake_types
        sys.modules["google"] = fake_google
        sys.modules["google.genai"] = fake_genai
        sys.modules["google.genai.types"] = fake_types
        try:
            with patch("agent_windows.utils.api.encode_image_bytes",
                       return_value=b"\xff\xd8rawjpeg") as mock_enc:
                call_vision_model(
                    Image.new("RGB", (4, 4)), "p",
                    {"engine": "gemini", "api_key": "gk", "model": "m"})
            # encode_image_bytes WAS called (not skipped)...
            mock_enc.assert_called_once()
            # ...and its bytes reached Part.from_bytes with a jpeg mime type
            assert captured["data"] == b"\xff\xd8rawjpeg"
            assert captured["mime_type"] == "image/jpeg"
        finally:
            sys.modules.pop("google", None)
            sys.modules.pop("google.genai", None)
            sys.modules.pop("google.genai.types", None)

    def test_gemini_respects_user_image_format(self):
        """A user config of image_format=PNG must reach Part.from_bytes as
        image/png, not the JPEG default."""
        import sys
        captured = {}

        fake_client = MagicMock()
        fake_client.models.generate_content.return_value = MagicMock(text="ok")

        def _fake_from_bytes(data, mime_type=None):
            captured["mime_type"] = mime_type
            return MagicMock()

        fake_types = MagicMock()
        fake_types.Part.from_bytes.side_effect = _fake_from_bytes
        fake_types.GenerateContentConfig = MagicMock()
        fake_types.HttpOptions = MagicMock()
        fake_genai = MagicMock()
        fake_genai.Client.return_value = fake_client
        fake_genai.types = fake_types
        fake_google = MagicMock()
        fake_google.genai = fake_genai
        sys.modules["google"] = fake_google
        sys.modules["google.genai"] = fake_genai
        sys.modules["google.genai.types"] = fake_types
        try:
            with patch("agent_windows.utils.api.encode_image_bytes",
                       return_value=b"\x89PNG"):
                call_vision_model(
                    Image.new("RGB", (4, 4)), "p",
                    {"engine": "gemini", "api_key": "gk", "model": "m",
                     "image_format": "PNG"})
            assert captured["mime_type"] == "image/png"
        finally:
            sys.modules.pop("google", None)
            sys.modules.pop("google.genai", None)
            sys.modules.pop("google.genai.types", None)


class TestGeminiClientTimeout:
    """G19(b): Gemini genai.Client must receive http_options=HttpOptions(timeout=MS)
    so a hung call fails at the client instead of only the IPC read_timeout.
    Project config vlm.timeout is in SECONDS; HttpOptions.timeout is MILLISECONDS."""

    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        api._client_cache.clear()
        yield
        api._client_cache.clear()

    def test_gemini_client_receives_http_options_timeout(self):
        import sys
        fake_client = MagicMock()
        fake_client.models.generate_content.return_value = MagicMock(text="g")
        captured = {}
        fake_types = MagicMock()
        fake_types.Part.from_bytes.return_value = MagicMock()

        def _http_options(**kwargs):
            captured.update(kwargs)
            return MagicMock(name="HttpOptions")

        fake_types.HttpOptions.side_effect = _http_options
        fake_types.GenerateContentConfig = MagicMock()
        fake_genai = MagicMock()
        fake_genai.Client.return_value = fake_client
        fake_genai.types = fake_types
        fake_google = MagicMock()
        fake_google.genai = fake_genai
        sys.modules["google"] = fake_google
        sys.modules["google.genai"] = fake_genai
        sys.modules["google.genai.types"] = fake_types
        try:
            with patch("agent_windows.utils.api.encode_image_bytes",
                       return_value=b"\xff\xd8jpg"):
                call_vision_model(
                    Image.new("RGB", (4, 4)), "p",
                    {"engine": "gemini", "api_key": "gk", "model": "m",
                     "timeout": 45})
            # 45 seconds → 45000 milliseconds
            assert captured.get("timeout") == 45000
            # and it was actually passed to Client as http_options
            assert "http_options" in fake_genai.Client.call_args.kwargs
        finally:
            sys.modules.pop("google", None)
            sys.modules.pop("google.genai", None)
            sys.modules.pop("google.genai.types", None)

    def test_gemini_default_timeout_is_30_seconds(self):
        """No timeout in config → default 30s → 30000ms."""
        import sys
        captured = {}
        fake_types = MagicMock()
        fake_types.Part.from_bytes.return_value = MagicMock()

        def _http_options(**kwargs):
            captured.update(kwargs)
            return MagicMock()

        fake_types.HttpOptions.side_effect = _http_options
        fake_types.GenerateContentConfig = MagicMock()
        fake_client = MagicMock()
        fake_client.models.generate_content.return_value = MagicMock(text="g")
        fake_genai = MagicMock()
        fake_genai.Client.return_value = fake_client
        fake_genai.types = fake_types
        fake_google = MagicMock()
        fake_google.genai = fake_genai
        sys.modules["google"] = fake_google
        sys.modules["google.genai"] = fake_genai
        sys.modules["google.genai.types"] = fake_types
        try:
            with patch("agent_windows.utils.api.encode_image_bytes",
                       return_value=b"\xff\xd8jpg"):
                call_vision_model(
                    Image.new("RGB", (4, 4)), "p",
                    {"engine": "gemini", "api_key": "gk", "model": "m"})
            assert captured.get("timeout") == 30000
        finally:
            sys.modules.pop("google", None)
            sys.modules.pop("google.genai", None)
            sys.modules.pop("google.genai.types", None)


class TestCacheKeyIncludesTimeout:
    """G19(b): the three backends bake timeout into the cached client (OpenAI/
    Anthropic via constructor, Gemini via HttpOptions). But the cache KEY omitted
    timeout, so `config set vlm.timeout` on a long-running daemon was swallowed
    by the stale cached client. timeout must be part of the key so changing it
    builds a fresh client."""

    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        api._client_cache.clear()
        yield
        api._client_cache.clear()

    def test_timeout_zero_floored_to_one_second(self):
        """A misconfigured vlm.timeout of 0 would become HttpOptions(timeout=0)
        (0 ms → instant timeout on every Gemini request). The resolver floors it
        at 1s; the cache key then carries :1, and HttpOptions gets 1000ms."""
        from agent_windows.utils.api import _resolve_timeout
        assert _resolve_timeout({"timeout": 0}) == 1.0
        assert _resolve_timeout({"timeout": -5}) == 1.0
        # normal values pass through
        assert _resolve_timeout({"timeout": 45}) == 45.0
        assert _resolve_timeout({}) == 30.0  # default

    def test_openai_different_timeout_builds_new_client(self):
        fake_client = MagicMock()
        fake_client.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content="r"))]
        )
        with patch("agent_windows.utils.api.encode_image", return_value="b64"), \
             patch("openai.OpenAI", return_value=fake_client) as mock_openai:
            call_vision_model(Image.new("RGB", (4, 4)), "p",
                              {"api_key": "k", "api_base": "http://x", "timeout": 30})
            call_vision_model(Image.new("RGB", (4, 4)), "p",
                              {"api_key": "k", "api_base": "http://x", "timeout": 60})
        # two distinct timeouts → two clients (config change took effect)
        assert mock_openai.call_count == 2
        assert "openai:http://x:k:30" in api._client_cache
        assert "openai:http://x:k:60" in api._client_cache

    def test_openai_same_timeout_reuses_client(self):
        fake_client = MagicMock()
        fake_client.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content="r"))]
        )
        with patch("agent_windows.utils.api.encode_image", return_value="b64"), \
             patch("openai.OpenAI", return_value=fake_client) as mock_openai:
            call_vision_model(Image.new("RGB", (4, 4)), "p",
                              {"api_key": "k", "api_base": "http://x", "timeout": 30})
            call_vision_model(Image.new("RGB", (4, 4)), "p",
                              {"api_key": "k", "api_base": "http://x", "timeout": 30})
        assert mock_openai.call_count == 1  # same timeout → reused

    def test_gemini_different_timeout_builds_new_client(self):
        import sys
        fake_client = MagicMock()
        fake_client.models.generate_content.return_value = MagicMock(text="g")
        fake_types = MagicMock()
        fake_types.Part.from_bytes.return_value = MagicMock()
        fake_types.HttpOptions.return_value = MagicMock()
        fake_types.GenerateContentConfig = MagicMock()
        fake_genai = MagicMock()
        fake_genai.Client.return_value = fake_client
        fake_genai.types = fake_types
        fake_google = MagicMock()
        fake_google.genai = fake_genai
        sys.modules["google"] = fake_google
        sys.modules["google.genai"] = fake_genai
        sys.modules["google.genai.types"] = fake_types
        try:
            with patch("agent_windows.utils.api.encode_image_bytes",
                       return_value=b"\xff\xd8jpg"):
                call_vision_model(Image.new("RGB", (4, 4)), "p",
                                  {"engine": "gemini", "api_key": "k", "timeout": 30})
                call_vision_model(Image.new("RGB", (4, 4)), "p",
                                  {"engine": "gemini", "api_key": "k", "timeout": 60})
            assert fake_genai.Client.call_count == 2  # timeout change → new client
            assert "gemini:k:30" in api._client_cache
            assert "gemini:k:60" in api._client_cache
        finally:
            sys.modules.pop("google", None)
            sys.modules.pop("google.genai", None)
            sys.modules.pop("google.genai.types", None)


