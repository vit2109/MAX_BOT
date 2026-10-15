"""Build the container CA bundle from certifi and pinned public Gosuslugi CAs."""
import hashlib
from pathlib import Path
import ssl
import sys
import urllib.request

import certifi

# Official download URLs documented at https://www.gosuslugi.ru/crt and
# https://www.rustore.ru/help/developers/monetization/payment-callback/Preparing-the-server-for-RuStore%20API
# SHA-256 of the complete downloaded PEM files; reviewed on 2026-10-15.
CA_FILES = {
    "russian_trusted_root_ca_pem.crt": "936a43fea6e8e525bcc0f81acd9c3d21b4fc4b9b68acea7906d698005afc6504",
    "russian_trusted_sub_ca_pem.crt": "f0ae589f36774f29ef3648f7984b08d42fcce6f1ffeeb6236d773daeb2744ea6",
}


def download_ca(name: str, expected_hash: str) -> bytes:
    url = "https://gu-st.ru/content/lending/" + name
    with urllib.request.urlopen(url, timeout=30) as response:
        payload = response.read(100_001)
    if hashlib.sha256(payload).hexdigest() != expected_hash:
        raise ValueError(f"CA {name}: контрольная сумма не совпадает; проверьте обновление сертификата")
    # Parse as a certificate and ensure OpenSSL can load it before extending trust.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(cadata=payload.decode("ascii"))
    return payload


def build_bundle(target: Path) -> None:
    parts = [Path(certifi.where()).read_bytes()]
    for name, expected_hash in CA_FILES.items():
        parts.append(download_ca(name, expected_hash))
    target.write_bytes(b"\n".join(parts) + b"\n")
    # Fail the image build instead of shipping a malformed trust bundle.
    ssl.create_default_context(cafile=str(target))
    print("CA bundle: standard roots + verified Russian Trusted Root/Sub CA")


if __name__ == "__main__":
    build_bundle(Path(sys.argv[1]))
