"""Ephemeral loopback TLS for synthetic fixtures; never changes system trust."""

import atexit
import base64
from contextlib import contextmanager
from functools import lru_cache
import hashlib
import os
from pathlib import Path
import ssl
import subprocess
import tempfile


@lru_cache(maxsize=1)
def _certificate():
    temporary = tempfile.TemporaryDirectory(prefix="metaphor-fixture-tls-")
    atexit.register(temporary.cleanup)
    root = Path(temporary.name)
    config = root / "openssl.cnf"
    config.write_text("""[req]
distinguished_name = dn
x509_extensions = extensions
prompt = no
[dn]
CN = METAPHOR synthetic loopback fixture
[extensions]
subjectAltName = IP:127.0.0.1
basicConstraints = critical,CA:TRUE
keyUsage = critical,digitalSignature,keyEncipherment,keyCertSign
extendedKeyUsage = serverAuth
""")
    cert, key = root / "cert.pem", root / "key.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
                    "-config", str(config), "-keyout", str(key), "-out", str(cert)],
                   check=True, timeout=15, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    key.chmod(0o600)
    public = subprocess.check_output(["openssl", "x509", "-in", str(cert), "-pubkey", "-noout"], timeout=5)
    der = subprocess.check_output(["openssl", "pkey", "-pubin", "-outform", "DER"], input=public, timeout=5)
    return cert, key, base64.b64encode(hashlib.sha256(der).digest()).decode()


@contextmanager
def local_https(server):
    """Trust only an ephemeral test CA in this fixture's process environment.

    Existing CA roots are retained for callers using external model providers.
    Fixtures run sequentially; no system/browser trust store is modified.
    """
    cert, key, pin = _certificate()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    with tempfile.TemporaryDirectory(prefix="metaphor-fixture-trust-") as root:
        bundle = Path(root) / "ca.pem"
        roots = [ssl.get_default_verify_paths().cafile, os.environ.get("NODE_EXTRA_CA_CERTS")]
        data = b"\n".join(Path(path).read_bytes() for path in dict.fromkeys(roots) if path and Path(path).is_file())
        bundle.write_bytes(data + b"\n" + cert.read_bytes())
        values = {"SSL_CERT_FILE": str(bundle), "NODE_EXTRA_CA_CERTS": str(bundle), "METAPHOR_TEST_TLS_SPKI": pin}
        previous = {key: os.environ.get(key) for key in values}
        os.environ.update(values)
        try:
            yield
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
