import http.client
import json
import os
import unittest
from unittest.mock import patch

from core_engine import ModelError, OpenAIModel, TokenUsage
from core_engine.analysis_schema import analysis_schema
from qa_fixtures import screenshot


def envelope():
    return {"status": "completed", "model": "test-model", "output": [
        {"type": "reasoning", "summary": []},
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": '{"ok":true}'}]},
    ], "usage": {"input_tokens": 10, "output_tokens": 30}}


class OpenAIProviderTests(unittest.TestCase):
    def setUp(self):
        self.patch = patch("core_engine.openai_provider.http.client.HTTPSConnection")
        self.factory = self.patch.start()
        self.addCleanup(self.patch.stop)
        self.connection = self.factory.return_value
        self.response = self.connection.getresponse.return_value
        self.response.status = 200
        self.response.read.return_value = json.dumps(envelope()).encode()
        self.model = OpenAIModel(api_key="test-api-secret", model="test-model")

    def generate(self):
        return self.model.generate(instructions="trusted instructions", context={"report": "untrusted text"},
                                   schema=analysis_schema({"report"}))

    def assert_error(self, code):
        with self.assertRaises(ModelError) as raised:
            self.generate()
        self.assertEqual(str(raised.exception), code)
        self.assertNotIn("test-api-secret", str(raised.exception))
        self.connection.close.assert_called_once()
        self.assertEqual(self.connection.request.call_count, 1)

    def test_request_contract_and_response_metadata(self):
        result = self.generate()
        self.assertEqual(result.text, '{"ok":true}')
        self.assertEqual(result.usage, TokenUsage(10, 30))
        args, kwargs = self.connection.request.call_args
        self.assertEqual(args, ("POST", "/v1/responses"))
        self.assertEqual(self.factory.call_args.args, ("api.openai.com",))
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer test-api-secret")
        body = json.loads(kwargs["body"])
        self.assertFalse(body["store"])
        self.assertEqual(body["max_output_tokens"], 3000)
        self.assertNotIn("tools", body)
        self.assertNotIn("temperature", body)
        self.assertEqual(body["instructions"], "trusted instructions")
        self.assertEqual(body["input"][0]["role"], "user")
        self.assertTrue(body["text"]["format"]["strict"])
        self.assertEqual(body["text"]["format"]["type"], "json_schema")
        self.response.read.assert_called_once_with(1024 * 1024 + 1)
        self.connection.close.assert_called_once()
        self.assertNotIn("test-api-secret", repr(self.model))

    def test_no_usage_is_unknown_not_zero(self):
        data = envelope()
        data.pop("usage")
        self.response.read.return_value = json.dumps(data).encode()
        self.assertIsNone(self.generate().usage)

    def test_non_ascii_report_reaches_model_as_readable_literal_text(self):
        context = {'report': '검색어는 없는상품, 합성 상품은 머그입니다.'}
        self.model.generate(instructions='trusted', context=context, schema=analysis_schema({'report'}))
        body = json.loads(self.connection.request.call_args.kwargs['body'])
        text = body['input'][0]['content'][0]['text']
        self.assertIn('없는상품', text)
        self.assertIn('머그', text)
        self.assertNotIn('\\u', text)
        self.assertEqual(json.loads(text), context)

    def test_validated_images_use_data_urls_without_remote_fetches(self):
        image = screenshot()
        self.model.generate(instructions="trusted", context={"report": "screenshot"},
                            schema=analysis_schema({"report"}), images=(image,))
        body = json.loads(self.connection.request.call_args.kwargs["body"])
        content = body["input"][0]["content"]
        self.assertEqual(content[0]["type"], "input_text")
        self.assertEqual(content[1], {"type": "input_image", "image_url": image.data_url(), "detail": "high"})
        self.connection.reset_mock()
        with self.assertRaises(ValueError):
            self.model.generate(instructions="trusted", context={}, schema={}, images=("https://untrusted.test/image",))
        self.connection.request.assert_not_called()

    def test_http_failures_and_redirect_do_not_retry_or_read_error_body(self):
        for status, code in ((401, "MODEL_AUTH_FAILED"), (403, "MODEL_AUTH_FAILED"), (429, "MODEL_RATE_LIMITED"), (413, "MODEL_REQUEST_TOO_LARGE"),
                             (500, "MODEL_HTTP_ERROR"), (307, "MODEL_HTTP_ERROR")):
            with self.subTest(status=status):
                self.connection.reset_mock()
                self.response.status = status
                self.assert_error(code)
                self.response.read.assert_not_called()

    def test_timeout_and_transport_failure(self):
        for error, code in ((TimeoutError("private info"), "MODEL_TIMEOUT"),
                            (OSError("private info"), "MODEL_UNAVAILABLE"),
                            (http.client.IncompleteRead(b"private info"), "MODEL_UNAVAILABLE")):
            with self.subTest(error=error):
                self.connection.reset_mock()
                self.response.read.side_effect = error
                self.assert_error(code)

    def test_refusal_and_incomplete_are_not_parsed_as_success(self):
        data = envelope()
        data["output"][1]["content"] = [{"type": "refusal", "refusal": "do not log this"}]
        self.response.read.return_value = json.dumps(data).encode()
        self.assert_error("MODEL_REFUSED")
        self.connection.reset_mock()
        data["status"] = "incomplete"
        self.response.read.return_value = json.dumps(data).encode()
        self.assert_error("MODEL_INCOMPLETE")

    def test_malformed_envelopes_and_usage(self):
        bad_data = [[], {}, {**envelope(), "output": []}, {**envelope(), "model": "secret\n"},
                    {**envelope(), "usage": {"input_tokens": True, "output_tokens": -1}},
                    {**envelope(), "output": [{"type": "function_call", "name": "execute"}]}]
        for value in bad_data:
            with self.subTest(value=value):
                self.connection.reset_mock()
                self.response.read.return_value = json.dumps(value).encode()
                self.assert_error("MODEL_INVALID_RESPONSE")
        for raw in (b"invalid-json-secret", b'\xff', b'{"status":1,"status":2}', b'{"value":NaN}', b"[" * 2000):
            self.connection.reset_mock()
            self.response.read.return_value = raw
            self.assert_error("MODEL_INVALID_RESPONSE")

    def test_response_and_input_size_limits(self):
        self.response.read.return_value = b"x" * (1024 * 1024 + 1)
        self.assert_error("MODEL_RESPONSE_TOO_LARGE")
        self.connection.reset_mock()
        with self.assertRaises(ValueError):
            self.model.generate(instructions="a" * (128 * 1024), context={}, schema={})
        self.connection.request.assert_not_called()

    def test_config_requires_explicit_key_and_model(self):
        for changes in ({"api_key": ""}, {"api_key": "bad\nkey"}, {"api_key": "bad\x01key"},
                        {"model": ""}, {"model": "model\n"}, {"timeout_seconds": 61},
                        {"timeout_seconds": float("nan")}, {"max_output_tokens": True}, {"max_output_tokens": 9000}):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    OpenAIModel(**{"api_key": "test-key", "model": "test-model", **changes})
        with patch.dict(os.environ, {"OPENAI_API_KEY": "env-secret", "OPENAI_MODEL": "configured-model"}, clear=True):
            self.assertEqual(OpenAIModel.from_env().model, "configured-model")
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(ValueError):
            OpenAIModel.from_env()


if __name__ == "__main__":
    unittest.main()
