"""
Tests del WhatsAppClient: modo stub, formato de teléfonos, 429, errores HTTP.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
import httpx
import pytest

import pytest
from app.domain.exceptions import InvalidPhoneError
from app.services.whatsapp import WhatsAppClient, RateLimitError, normalize_phone as _format_phone


class TestFormatPhone:
    def test_eight_digits_adds_591(self):
        assert _format_phone("76543210") == "59176543210"

    def test_already_has_591(self):
        assert _format_phone("59176543210") == "59176543210"

    def test_plus_prefix_removed(self):
        assert _format_phone("+59176543210") == "59176543210"

    def test_invalid_phone_raises_error(self):
        """Números con longitud no reconocida ahora lanzan InvalidPhoneError."""
        with pytest.raises(InvalidPhoneError):
            _format_phone("123")

    def test_invalid_phone_too_long_raises_error(self):
        with pytest.raises(InvalidPhoneError):
            _format_phone("123456789012345")

    def test_strips_non_digits(self):
        assert _format_phone("591-7654-3210") == "59176543210"


class TestWhatsAppClientStubMode:
    def test_empty_token_activates_stub(self):
        c = WhatsAppClient(token="", phone_number_id="pid")
        assert c._stub_mode is True

    def test_empty_phone_id_activates_stub(self):
        c = WhatsAppClient(token="tok", phone_number_id="")
        assert c._stub_mode is True

    def test_both_set_deactivates_stub(self):
        c = WhatsAppClient(token="tok", phone_number_id="pid")
        assert c._stub_mode is False

    def test_stub_send_does_not_raise(self):
        c = WhatsAppClient(token="", phone_number_id="")
        asyncio.run(c.send_text("76543210", "Hola"))

    def test_constructor_params_override_env(self):
        c = WhatsAppClient(token="my_tok", phone_number_id="my_pid", api_version="v20.0")
        assert c._token          == "my_tok"
        assert c._phone_number_id == "my_pid"
        assert c._api_version     == "v20.0"


class TestWhatsAppClientHTTP:
    def _client(self):
        return WhatsAppClient(token="tok", phone_number_id="pid")

    def _mock_resp(self, status, headers=None, text="{}"):
        resp = MagicMock()
        resp.status_code = status
        resp.headers     = headers or {}
        resp.text        = text
        resp.is_success  = 200 <= status < 300
        if not resp.is_success:
            resp.raise_for_status = MagicMock(
                side_effect=httpx.HTTPStatusError("err", request=MagicMock(), response=resp)
            )
        else:
            resp.raise_for_status = MagicMock()
        return resp

    def test_200_sends_without_error(self):
        c = self._client()
        resp = self._mock_resp(200)
        async def _go():
            with patch.object(c._http, "post", new=AsyncMock(return_value=resp)):
                await c.send_text("76543210", "Hola")
        asyncio.run(_go())

    def test_429_raises_rate_limit_error(self):
        c = self._client()
        resp = self._mock_resp(429, headers={"Retry-After": "120"})
        async def _go():
            with patch.object(c._http, "post", new=AsyncMock(return_value=resp)):
                with pytest.raises(RateLimitError) as exc:
                    await c.send_text("76543210", "Hola")
                assert exc.value.retry_after == 120
        asyncio.run(_go())

    def test_429_no_retry_after_defaults_60(self):
        c = self._client()
        resp = self._mock_resp(429, headers={})
        async def _go():
            with patch.object(c._http, "post", new=AsyncMock(return_value=resp)):
                with pytest.raises(RateLimitError) as exc:
                    await c.send_text("76543210", "Hola")
                assert exc.value.retry_after == 60
        asyncio.run(_go())

    def test_500_raises_http_status_error(self):
        c = self._client()
        resp = self._mock_resp(500)
        async def _go():
            with patch.object(c._http, "post", new=AsyncMock(return_value=resp)):
                with pytest.raises(httpx.HTTPStatusError):
                    await c.send_text("76543210", "Hola")
        asyncio.run(_go())

    def test_phone_is_formatted_before_send(self):
        c = self._client()
        resp = self._mock_resp(200)
        captured = {}
        async def _go():
            async def fake_post(url, headers, json):
                captured["to"] = json["to"]
                return resp
            with patch.object(c._http, "post", new=fake_post):
                await c.send_text("76543210", "Hola")
        asyncio.run(_go())
        assert captured["to"] == "59176543210"

    def test_rate_limit_error_message_includes_seconds(self):
        err = RateLimitError(retry_after=90)
        assert "90" in str(err)
        assert err.retry_after == 90
