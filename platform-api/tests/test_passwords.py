"""M04 密码哈希测试。"""

from __future__ import annotations

from agentforge_platform.passwords import hash_password, verify_password


def test_hash_roundtrip_and_no_plaintext_storage() -> None:
    encoded = hash_password("s3cret-value")

    assert encoded != "s3cret-value"
    assert "s3cret-value" not in encoded
    assert encoded.startswith("scrypt$")
    assert verify_password("s3cret-value", encoded)


def test_wrong_password_is_rejected() -> None:
    encoded = hash_password("correct-horse")

    assert not verify_password("battery-staple", encoded)


def test_hashes_are_salted_and_unique() -> None:
    assert hash_password("same") != hash_password("same")


def test_malformed_hash_is_rejected() -> None:
    assert not verify_password("x", "not-a-valid-hash")
    assert not verify_password("x", "bcrypt$whatever")
