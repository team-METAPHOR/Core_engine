import socket
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core_engine.policy import PolicyError, RunPolicy, canonical_url


def policy(**overrides):
    values = dict(
        program_id="demo",
        scope_revision=1,
        base_url="https://staging.example.test/",
        allowed_path_prefixes=("/app",),
        excluded_path_prefixes=("/app/logout",),
    )
    values.update(overrides)
    return RunPolicy(**values)


def dns(*addresses):
    def resolve(_host, port, *, type):
        assert type == socket.SOCK_STREAM
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port)) for address in addresses]
    return resolve


class CanonicalUrlTests(unittest.TestCase):
    def test_normalizes_host_port_and_encoded_path(self):
        self.assertEqual(canonical_url("https://EXAMPLE.COM:443/a%62?term=one"), "https://example.com/ab?term=one")

    def test_rejects_ambiguous_or_sensitive_urls(self):
        values = (
            "https://user:pass@example.com/",
            "https://example.com/a/../b",
            "https://example.com/a%252fb",
            "https://example.com//admin",
            "https://example.com:0/",
            "https://example.com:/",
            "https://example.com/a#fragment",
            "https://example.com/?access_token=secret",
            "https://2130706433/",
            "https://example.com\\@evil.test/",
        )
        for value in values:
            with self.subTest(value=value), self.assertRaises(PolicyError):
                canonical_url(value)


class ScopeTests(unittest.TestCase):
    def test_segment_boundary_exclusion_method_and_origin(self):
        p = policy()
        self.assertEqual(p.check_url("https://staging.example.test/app/search?q=cat"), "https://staging.example.test/app/search?q=cat")
        for value, method, code in (
            ("https://staging.example.test/application", "GET", "PATH_OUT_OF_SCOPE"),
            ("https://staging.example.test/app/logout", "GET", "PATH_EXCLUDED"),
            ("https://staging.example.test/app/logout/now", "GET", "PATH_EXCLUDED"),
            ("https://other.example.test/app", "GET", "ORIGIN_OUT_OF_SCOPE"),
            ("https://staging.example.test/app", "POST", "METHOD_NOT_ALLOWED"),
            ("https://staging.example.test/app/delete", "GET", "STATE_CHANGING_PATH"),
        ):
            with self.subTest(value=value), self.assertRaises(PolicyError) as error:
                p.check_url(value, method)
            self.assertEqual(error.exception.code, code)

    def test_redirect_is_checked_before_following(self):
        p = policy()
        self.assertEqual(
            p.check_redirect("https://staging.example.test/app/start", "/app/next"),
            "https://staging.example.test/app/next",
        )
        with self.assertRaises(PolicyError) as error:
            p.check_redirect("https://staging.example.test/app/start", "https://evil.test/")
        self.assertEqual(error.exception.code, "ORIGIN_OUT_OF_SCOPE")
        with self.assertRaises(PolicyError) as error:
            p.check_redirect("https://staging.example.test/app/start", "/app/logout")
        self.assertEqual(error.exception.code, "PATH_EXCLUDED")

    def test_dns_pins_approved_ip_and_rejects_mixed_answers(self):
        p = policy()
        target = p.resolve_target("https://staging.example.test/app", resolver=dns("8.8.8.8"))
        self.assertEqual((target.hostname, target.port, target.address), ("staging.example.test", 443, "8.8.8.8"))
        with self.assertRaises(PolicyError) as error:
            p.resolve_target("https://staging.example.test/app", resolver=dns("8.8.8.8", "127.0.0.1"))
        self.assertEqual(error.exception.code, "PRIVATE_DESTINATION_BLOCKED")

    def test_private_target_requires_exact_opt_in(self):
        p = policy(allow_private_targets=True, private_cidrs=("127.0.0.0/8",))
        target = p.resolve_target("https://staging.example.test/app", resolver=dns("127.0.0.1"))
        self.assertEqual(target.address, "127.0.0.1")
        with self.assertRaises(PolicyError):
            policy(private_cidrs=("127.0.0.0/8",))
        with self.assertRaises(PolicyError):
            policy(allow_private_targets=True, private_cidrs=("0.0.0.0/0",))

    def test_empty_dns_is_not_approved(self):
        with self.assertRaises(PolicyError) as error:
            policy().resolve_target("https://staging.example.test/app", resolver=dns())
        self.assertEqual(error.exception.code, "DNS_FAILED")


if __name__ == "__main__":
    unittest.main()
