#!/usr/bin/env python3
"""Historical V5 key generator; NOT required or consumed by current trusted publication."""

import argparse
import json
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

parser = argparse.ArgumentParser()
parser.add_argument("--directory", type=Path, required=True)
parser.add_argument("--tenant", action="append", required=True)
parser.add_argument("--kid", required=True)
args = parser.parse_args()
args.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
private_path, trust_path = args.directory / "te-private.pem", args.directory / "trusted-te.json"
if private_path.exists() or trust_path.exists():
    parser.error("refusing to overwrite an existing key or trust file")
key = Ed25519PrivateKey.generate()
outputs = {
    private_path: key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ),
    trust_path: json.dumps(
        {
            args.kid: {
                "tenants": args.tenant,
                "public_key": key.public_key()
                .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
                .decode(),
            }
        },
        indent=2,
    ).encode(),
}
for path, data in outputs.items():
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
print("Created TE private key and OV tenant-scoped public trust file. No keys printed.")
