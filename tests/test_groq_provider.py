import http.client
import json
import os
import unittest
from unittest.mock import patch

from core_engine import GroqModel, ModelError, TokenUsage
from core_engine.critic import REVIEW_SCHEMA
from core_engine.redaction import redact_text
from qa_fixtures import screenshot


def envelope():
    return {'model': 'openai/gpt-oss-120b', 'choices': [{'finish_reason': 'stop',
        'message': {'role': 'assistant', 'content': '{"ok":true}'}}],
        'usage': {'prompt_tokens': 10, 'completion_tokens': 30}}


class GroqProviderTests(unittest.TestCase):
    def setUp(self):
        mock = patch('core_engine.groq_provider.http.client.HTTPSConnection')
        self.factory = mock.start()
        self.addCleanup(mock.stop)
        self.connection = self.factory.return_value
        self.response = self.connection.getresponse.return_value
        self.response.status = 200
        self.response.read.return_value = json.dumps(envelope()).encode()
        self.model = GroqModel(api_key='synthetic-secret', model='openai/gpt-oss-120b')

    def generate(self, **kwargs):
        return self.model.generate(instructions='trusted', context={'report': '입력 순서 확인'},
                                   schema=REVIEW_SCHEMA, **kwargs)

    def test_text_only_request_and_usage(self):
        result = self.generate()
        self.assertEqual(result.usage, TokenUsage(10, 30))
        self.assertEqual(self.factory.call_args.args, ('api.groq.com',))
        args, kwargs = self.connection.request.call_args
        self.assertEqual(args, ('POST', '/openai/v1/chat/completions'))
        body = json.loads(kwargs['body'])
        self.assertEqual(body['messages'][0], {'role': 'system', 'content': 'trusted'})
        self.assertIn('입력 순서', body['messages'][1]['content'])
        self.assertTrue(body['response_format']['json_schema']['strict'])
        self.assertFalse(body['include_reasoning'])
        self.assertEqual(body['reasoning_effort'], 'medium')
        self.assertNotIn('tools', body)
        self.assertNotIn('synthetic-secret', repr(self.model))
        self.response.read.assert_called_once_with(1024 * 1024 + 1)
        self.connection.close.assert_called_once()

    def test_http_errors_do_not_read_bodies_retry_or_follow_redirects(self):
        for status, code in ((401, 'MODEL_AUTH_FAILED'), (403, 'MODEL_AUTH_FAILED'),
                             (429, 'MODEL_RATE_LIMITED'), (307, 'MODEL_HTTP_ERROR'), (500, 'MODEL_HTTP_ERROR')):
            with self.subTest(status=status):
                self.connection.reset_mock()
                self.response.status = status
                with self.assertRaises(ModelError) as raised:
                    self.generate()
                self.assertEqual(str(raised.exception), code)
                self.response.read.assert_not_called()
                self.connection.request.assert_called_once()
                self.connection.close.assert_called_once()

    def test_timeouts_and_transport_errors_are_sanitized(self):
        for error, code in ((TimeoutError('secret'), 'MODEL_TIMEOUT'), (OSError('secret'), 'MODEL_UNAVAILABLE'),
                            (http.client.IncompleteRead(b'secret'), 'MODEL_UNAVAILABLE')):
            self.response.read.side_effect = error
            with self.assertRaises(ModelError) as raised:
                self.generate()
            self.assertEqual(str(raised.exception), code)

    def test_refusal_truncation_tools_and_malformed_output(self):
        for changes, code in (({'finish_reason': 'length'}, 'MODEL_INCOMPLETE'),
                              ({'finish_reason': 'content_filter'}, 'MODEL_REFUSED'),
                              ({'message': {'role': 'assistant', 'refusal': 'private'}}, 'MODEL_REFUSED'),
                              ({'message': {'role': 'assistant', 'content': '{}', 'tool_calls': [{}]}}, 'MODEL_INVALID_RESPONSE')):
            data = envelope()
            data['choices'][0].update(changes)
            self.response.read.return_value = json.dumps(data).encode()
            with self.assertRaises(ModelError) as raised:
                self.generate()
            self.assertEqual(str(raised.exception), code)
        for data in ([], {}, {**envelope(), 'choices': []}, {**envelope(), 'model': 'bad\n'},
                     {**envelope(), 'usage': {'prompt_tokens': True, 'completion_tokens': -1}}):
            self.response.read.return_value = json.dumps(data).encode()
            with self.assertRaises(ModelError) as raised:
                self.generate()
            self.assertEqual(str(raised.exception), 'MODEL_INVALID_RESPONSE')
        for raw in (b'private-invalid', b'{"choices":[],"choices":[]}', b'{"n":NaN}', b'\xff'):
            self.response.read.return_value = raw
            with self.assertRaises(ModelError) as raised:
                self.generate()
            self.assertEqual(str(raised.exception), 'MODEL_INVALID_RESPONSE')

    def test_limits_no_images_and_unknown_usage(self):
        data = envelope(); data.pop('usage')
        self.response.read.return_value = json.dumps(data).encode()
        self.assertIsNone(self.generate().usage)
        self.response.read.return_value = b'x' * (1024 * 1024 + 1)
        with self.assertRaises(ModelError) as raised:
            self.generate()
        self.assertEqual(raised.exception.code, 'MODEL_RESPONSE_TOO_LARGE')
        self.connection.reset_mock()
        with self.assertRaises(ValueError):
            self.generate(images=(screenshot(),))
        with self.assertRaises(ValueError):
            self.model.generate(instructions='a' * (128 * 1024), context={}, schema={})
        self.connection.request.assert_not_called()

    def test_explicit_configuration_and_groq_secret_redaction(self):
        for changes in ({'api_key': ''}, {'api_key': 'bad\n'}, {'model': ''}, {'model': 'bad\n'},
                        {'timeout_seconds': float('nan')}, {'max_output_tokens': True}):
            with self.assertRaises(ValueError):
                GroqModel(**{'api_key': 'synthetic', 'model': 'openai/gpt-oss-120b', **changes})
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(ValueError):
            GroqModel.from_env()
        with patch.dict(os.environ, {'GROQ_API_KEY': 'synthetic', 'GROQ_MODEL': 'openai/gpt-oss-120b'}, clear=True):
            self.assertEqual(GroqModel.from_env().model, 'openai/gpt-oss-120b')
        secret = 'gsk_' + 'x' * 48
        self.assertEqual(redact_text('제보 ' + secret), '제보 [REDACTED_KEY]')
