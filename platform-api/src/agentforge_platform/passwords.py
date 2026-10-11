"""密码哈希（scrypt，标准库实现，避免新增依赖）。

存储格式：``scrypt$n$r$p$<salt_b64>$<hash_b64>``。仅保存哈希与盐，
不保存明文；校验使用常量时间比较。

维护者可用如下命令为预置账号生成哈希（不会打印明文）：
``uv run python -m agentforge_platform.passwords``
"""

from __future__ import annotations

import base64
import getpass
import hashlib
import hmac
import secrets
import sys

_ALGORITHM = "scrypt"
_N = 2**14
_R = 8
_P = 1
_DK_LEN = 32
_SALT_BYTES = 16
_MAX_MEM = 2**26


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(_SALT_BYTES)
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_N,
        r=_R,
        p=_P,
        dklen=_DK_LEN,
        maxmem=_MAX_MEM,
    )
    return f"{_ALGORITHM}${_N}${_R}${_P}${_b64encode(salt)}${_b64encode(derived)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt_b64, hash_b64 = encoded.split("$")
        if algorithm != _ALGORITHM:
            return False
        salt = _b64decode(salt_b64)
        expected = _b64decode(hash_b64)
        derived = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
            maxmem=_MAX_MEM,
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(derived, expected)


def main(argv: list[str]) -> int:
    if argv:
        password = argv[0]
    else:
        password = getpass.getpass("Password to hash: ")
    if not password:
        print("empty password refused", file=sys.stderr)
        return 2
    print(hash_password(password))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
