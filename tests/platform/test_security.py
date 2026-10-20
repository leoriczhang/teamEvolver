"""Platform security contracts."""

from __future__ import annotations

from teamEvolver.security import SecretBox, mask_secret, secret_fingerprint


def test_secret_never_appears_in_mask_or_fingerprint():
    secret = "tevt_supersecretvalue"
    masked = mask_secret(secret)
    fp = secret_fingerprint(secret)
    assert secret not in masked
    assert secret not in fp
    # Fingerprint is stable and distinguishes different secrets.
    assert fp == secret_fingerprint(secret)
    assert fp != secret_fingerprint("tevt_other")


def test_secret_box_roundtrip_and_ciphertext_hides_plaintext():
    box = SecretBox("master-key")
    ct = box.encrypt("root-key")
    assert "root-key" not in ct
    assert box.decrypt(ct) == "root-key"
