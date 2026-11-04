import socket
import threading
import unittest
from unittest.mock import patch

from core_engine import BudgetLimits, PolicyError, RequestBudget, RunPolicy
from core_engine.transport import GuardedTransport
from fixture_site import site


def local_policy(origin):
    return RunPolicy("demo", 1, origin, ("/app",), allow_private_targets=True, private_cidrs=("127.0.0.1/32",))


class TransportTests(unittest.TestCase):
    def test_numeric_connection_preserves_host_and_rechecks_dns(self):
        with site() as (origin, hits, _):
            port = origin.rsplit(":", 1)[1]
            host = f"http://approved.test:{port}"
            answers = ["127.0.0.1"]
            def resolver(*args, **kwargs):
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (answers[0], int(port)))]
            transport = GuardedTransport(local_policy(host), RequestBudget(BudgetLimits()), lambda: True, threading.Event(), resolver=resolver)
            with patch("core_engine.transport.socket.create_connection", wraps=socket.create_connection) as connect:
                self.assertEqual(transport.fetch(host + "/app/bug").status, 200)
                self.assertEqual(connect.call_args.args[0], ("127.0.0.1", int(port)))
            self.assertEqual(hits[0][2], f"approved.test:{port}")
            answers[0] = "169.254.169.254"
            with self.assertRaises(PolicyError):
                transport.fetch(host + "/app/bug")
            self.assertEqual(len(hits), 1)

    def test_post_and_outside_redirect_never_reach_destination(self):
        with site() as (origin, hits, _):
            transport = GuardedTransport(local_policy(origin), RequestBudget(BudgetLimits()), lambda: True, threading.Event())
            with self.assertRaises(PolicyError):
                transport.fetch(origin + "/app/write", "POST")
            with self.assertRaises(PolicyError):
                transport.fetch(origin + "/app/redirect")
            self.assertEqual([hit[:2] for hit in hits], [("GET", "/app/redirect")])

    def test_large_and_truncated_responses_are_incomplete(self):
        with site() as (origin, _, _):
            limits = BudgetLimits(max_response_bytes=1024, max_download_bytes=2048)
            transport = GuardedTransport(local_policy(origin), RequestBudget(limits), lambda: True, threading.Event())
            for path, reason in (("/app/large", "RESPONSE_LIMIT"), ("/app/truncated", "TRUNCATED_RESPONSE")):
                with self.subTest(path=path), self.assertRaises(PolicyError) as error:
                    transport.fetch(origin + path)
                self.assertEqual(error.exception.code, reason)

    def test_revocation_after_dns_prevents_send(self):
        with site() as (origin, hits, _):
            authorized = [True]
            def resolver(host, port, **kwargs):
                authorized[0] = False
                return socket.getaddrinfo(host, port, **kwargs)
            transport = GuardedTransport(local_policy(origin), RequestBudget(BudgetLimits()), lambda: authorized[0], threading.Event(), resolver=resolver)
            with self.assertRaises(PolicyError) as error:
                transport.fetch(origin + "/app/bug")
            self.assertEqual(error.exception.code, "AUTHORIZATION_REVOKED")
            self.assertEqual(hits, [])
