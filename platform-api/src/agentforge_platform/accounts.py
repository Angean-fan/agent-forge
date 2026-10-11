"""平台内测预置账号。

平台账号由维护者预置（不做注册/找回），以 JSON 文件提供，仅保存密码哈希。
这些是**平台账号**，与生成项目内的独立登录账号完全不同，不可复用。
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger("agentforge.platform")

DEFAULT_ACCOUNTS_FILE = "config/accounts.json"


@dataclass(frozen=True)
class Account:
    id: int
    username: str
    password_hash: str
    is_active: bool = True


def accounts_file() -> Path:
    return Path(os.environ.get("AGENTFORGE_ACCOUNTS_FILE", DEFAULT_ACCOUNTS_FILE))


def load_accounts(path: str | Path) -> dict[int, Account]:
    """从 JSON 文件读取预置账号；文件缺失时返回空映射并告警。"""
    file_path = Path(path)
    if not file_path.exists():
        logger.warning("accounts file not found: %s (no preset accounts loaded)", file_path)
        return {}

    payload = json.loads(file_path.read_text(encoding="utf-8"))
    accounts: dict[int, Account] = {}
    for item in payload.get("accounts", []):
        account = Account(
            id=int(item["id"]),
            username=str(item["username"]),
            password_hash=str(item["password_hash"]),
            is_active=bool(item.get("is_active", True)),
        )
        accounts[account.id] = account
    return accounts


@lru_cache(maxsize=1)
def get_accounts() -> dict[int, Account]:
    return load_accounts(accounts_file())


def find_by_username(accounts: dict[int, Account], username: str) -> Account | None:
    for account in accounts.values():
        if account.username == username:
            return account
    return None


def find_by_id(accounts: dict[int, Account], account_id: int) -> Account | None:
    return accounts.get(account_id)
