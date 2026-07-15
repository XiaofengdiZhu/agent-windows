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
        # cache now holds the client under the openai key
        assert "openai:http://x:k" in api._client_cache

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
        # the cached client's key reflects the env fallback
        assert "openai:http://x:env-key" in api._client_cache
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
            assert "anthropic:ck" in api._client_cache
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
            # Client built once (cached), keyed by api_key only — model is per-call.
            assert fake_genai.Client.call_count == 1
            assert r1 == "g" and r2 == "g"
            assert "gemini:gk" in api._client_cache
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
            assert "gemini:keyA" in api._client_cache
            assert "gemini:keyB" in api._client_cache
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

