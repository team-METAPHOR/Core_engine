import ssl
import threading
import unittest
from unittest.mock import patch

from core_engine import (BrowserOptions, BrowserRunner, BudgetLimits, LoginOptions,
                         PolicyError, RequestBudget, export_playwright_test)
from core_engine.transport import GuardedTransport
from fixture_site import site
from test_login import scenario
from test_transport import local_policy


class CredentialHTTPSTests(unittest.TestCase):
    def transport(self, origin, **kwargs):
        return GuardedTransport(local_policy(origin), RequestBudget(BudgetLimits()),
                                lambda: True, threading.Event(), **kwargs)

    def test_http_credentials_fail_before_dns_or_socket(self):
        with patch('core_engine.transport.socket.create_connection') as connect, \
                patch('socket.getaddrinfo') as dns:
            for headers in ({'Cookie': 'session=synthetic'}, {'Authorization': 'Bearer synthetic'}):
                with self.subTest(headers=list(headers)), self.assertRaises(PolicyError) as error:
                    self.transport('http://example.test', session_headers=headers)
                self.assertEqual(error.exception.code, 'CREDENTIALS_REQUIRE_HTTPS')
            with self.assertRaises(PolicyError) as error:
                self.transport('http://example.test', login_url='http://example.test/app/login')
            self.assertEqual(error.exception.code, 'CREDENTIALS_REQUIRE_HTTPS')
            connect.assert_not_called()
            dns.assert_not_called()

    def test_http_anonymous_still_reaches_target(self):
        with site() as (origin, hits, _):
            transport = self.transport(origin)
            try:
                self.assertEqual(transport.fetch(origin + '/app/bug').status, 200)
            finally:
                transport.close()
            self.assertEqual(len(hits), 1)

    def test_every_credential_request_rechecks_scheme(self):
        with patch('core_engine.transport.socket.create_connection') as connect, \
                patch('socket.getaddrinfo') as dns:
            transport = self.transport('https://example.test', login_url='https://example.test/app/login')
            try:
                for kwargs in ({'browser_headers': {'Cookie': 'session=synthetic'}},
                               {'browser_headers': {'Authorization': 'Bearer synthetic'}},
                               {'login_body': b'password=synthetic'}):
                    with self.subTest(fields=list(kwargs)), self.assertRaises(PolicyError) as error:
                        transport.fetch('http://example.test/app/login', **kwargs)
                    self.assertEqual(error.exception.code, 'CREDENTIALS_REQUIRE_HTTPS')
                transport.close()
                transport = self.transport('http://example.test')
                transport.session_headers['Cookie'] = 'session=late-configuration'
                with self.assertRaises(PolicyError) as error:
                    transport.fetch('http://example.test/app')
                self.assertEqual(error.exception.code, 'CREDENTIALS_REQUIRE_HTTPS')
            finally:
                transport.close()
            connect.assert_not_called()
            dns.assert_not_called()

    def test_https_redirect_cannot_forward_credentials_to_http(self):
        with site() as (plain, plain_hits, _), site(tls=True) as (secure, secure_hits, state):
            state['redirect_location'] = plain + '/app/bug'
            transport = self.transport(secure, session_headers={'Cookie': 'session=synthetic'})
            try:
                with self.assertRaises(PolicyError):
                    transport.fetch(secure + '/app/redirect')
            finally:
                transport.close()
            self.assertEqual(len(secure_hits), 1)
            self.assertEqual(plain_hits, [])

    def test_untrusted_tls_certificate_cannot_receive_credentials(self):
        untrusted = ssl.create_default_context()
        with site(tls=True) as (origin, hits, _):
            transport = self.transport(origin, session_headers={'Cookie': 'session=synthetic'})
            try:
                with patch('core_engine.transport.ssl.create_default_context', return_value=untrusted), \
                        self.assertRaises(PolicyError) as error:
                    transport.fetch(origin + '/app/bug')
                self.assertEqual(error.exception.code, 'TRANSPORT_ERROR')
                self.assertEqual(hits, [])
            finally:
                transport.close()

    def test_http_host_configuration_and_export_rejected(self):
        origin = 'http://example.test'
        options = (
            BrowserOptions(test_context_id='staff', session_headers={'Authorization': 'Bearer synthetic'}),
            BrowserOptions(test_context_id='staff', login=LoginOptions('/app/login', 'synthetic-id',
                'synthetic-password', success_path='/app/home')),
        )
        for config in options:
            with self.subTest(mode=config.public()['session_mode']):
                with self.assertRaises(PolicyError) as error:
                    BrowserRunner(local_policy(origin), options=config)
                self.assertEqual(error.exception.code, 'CREDENTIALS_REQUIRE_HTTPS')
                with self.assertRaises(PolicyError) as error:
                    export_playwright_test(scenario(origin), policy=local_policy(origin), options=config)
                self.assertEqual(error.exception.code, 'CREDENTIALS_REQUIRE_HTTPS')
