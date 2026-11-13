from datetime import UTC, datetime
from pathlib import Path

import pytest

from vpn_payment.config import Settings
from vpn_payment.database.repository import Repository

T0 = datetime(2026, 8, 1, tzinfo=UTC)
# Public synthetic fixtures, never personal wallet addresses.
SENDER = "0x52908400098527886E0F7030069857D2E4169EE7"
RECIPIENTS = ("0x" + "22" * 20, "0x" + "33" * 20)
ACTIVE_RECIPIENTS = (RECIPIENTS[1],)


def settings(**updates) -> Settings:
    values = {
        "sender_address": SENDER,
        "recipient_addresses": RECIPIENTS,
        "active_recipient_addresses": ACTIVE_RECIPIENTS,
        "history_start": T0,
    }
    return Settings(_env_file=None, **(values | updates))


@pytest.fixture
def config(tmp_path: Path) -> Settings:
    return settings(
        database_path=tmp_path / "test.sqlite3",
        history_start=T0,
        subscription_start=None,
        rpc_retries=1,
        rpc_request_interval_seconds=0,
    )


@pytest.fixture
async def repo(config):
    repository = Repository(config.database_path)
    await repository.open(config)
    yield repository
    await repository.close()
