"""Certificate trust for sites that do not send their intermediate certificate.

    from scrape_hub_client.tls import ca_bundle
    s = requests.Session()
    s.verify = ca_bundle()                       # certifi + the public intermediates shipped with this package

Many Indian government servers send only their own certificate, without the intermediate CA certificate that links it to a
root. Browsers fill the gap themselves; requests and urllib3 fail with CERTIFICATE_VERIFY_FAILED. `ca_bundle()` returns the path
of one PEM file: certifi's roots plus the public intermediate certificates in `certs/` (each one is a CA certificate that chains
to a root in certifi). Verification stays on: the extra certificates only complete chains that the server should send itself.

Add your own with `ca_bundle(extra_pems=[path_or_pem, ...])` (file paths, or PEM text as str or bytes). A certificate that every
family can use belongs in `certs/` of s1d-labs/runner (one certificate per file, a comment header, see the other files).
"""
import hashlib
import os
import re
import tempfile
import threading
from pathlib import Path

CERTS_DIR = Path(__file__).resolve().parent / "certs"
_PEM = re.compile(rb"-----BEGIN CERTIFICATE-----\s.*?-----END CERTIFICATE-----", re.S)
_lock = threading.Lock()
_made = {}


def bundled_pems():
    """The intermediate certificate files shipped with the package, sorted by name."""
    return sorted(CERTS_DIR.glob("*.pem"))


def _pem_blocks(item):
    if isinstance(item, (bytes, bytearray)):
        data = bytes(item)
    elif isinstance(item, str) and "-----BEGIN CERTIFICATE-----" in item:
        data = item.encode()
    else:
        data = Path(item).read_bytes()
    blocks = _PEM.findall(data)
    if not blocks:
        raise ValueError(f"no PEM certificate in {str(item)[:80]!r}")
    return blocks


def ca_bundle(extra_pems=()):
    """Path of a PEM file with certifi's roots, the bundled public intermediates and `extra_pems`; use it as requests' `verify=`.

    The file is written once per process for each set of inputs (in the temp directory) and reused. Certificates that occur twice
    are written once. Verification stays on."""
    import certifi

    blocks, seen = [], set()
    for item in [*bundled_pems(), *extra_pems]:
        for b in _pem_blocks(item):
            key = b"".join(b.split())
            if key not in seen:
                seen.add(key)
                blocks.append(b)
    roots = Path(certifi.where()).read_bytes()
    body = roots.rstrip(b"\n") + b"\n\n# scrape_hub_client.tls: extra intermediate certificates\n" + b"\n".join(blocks) + b"\n"
    digest = hashlib.sha256(body).hexdigest()
    with _lock:
        path = _made.get(digest)
        if path and os.path.isfile(path):
            return path
        fd, path = tempfile.mkstemp(prefix="scrape-hub-ca-", suffix=".pem")
        with os.fdopen(fd, "wb") as f:
            f.write(body)
        _made[digest] = path
        return path
