from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from tests.conftest import T0
from vpn_payment.blockchain.client import BaseClient, RPCError
from vpn_payment.blockchain.discovery import block_before, next_sender_block
from vpn_payment.blockchain.transactions import matches, transaction_hash, verify

HASH = "0x" + "ab" * 32
BLOCK_HASH = "0x" + "cd" * 32


def make_tx(config, **overrides):
    return {
        "hash": HASH,
        "from": config.sender_address,
        "to": config.recipient_addresses[0],
        "value": 2_000_000_000_000_000,
        "blockNumber": 10,
        "blockHash": BLOCK_HASH,
        **overrides,
    }


@pytest.fixture
def client(config):
    return AsyncMock(
        transaction=AsyncMock(return_value=make_tx(config)),
        receipt=AsyncMock(
            return_value={
                "status": 1,
                "transactionHash": HASH,
                "blockNumber": 10,
                "blockHash": BLOCK_HASH,
            }
        ),
        block=AsyncMock(return_value={"hash": BLOCK_HASH, "timestamp": int(T0.timestamp())}),
        head=AsyncMock(return_value=100),
    )


@pytest.mark.parametrize("recipient", [0, 1])
async def test_both_recipient_addresses(config, client, recipient):
    client.transaction.return_value["to"] = config.recipient_addresses[recipient].lower()
    result = await verify(client, config, HASH)
    assert result.recipient == config.recipient_addresses[recipient]
    assert result.value_wei == 2_000_000_000_000_000  # gas is never included
    assert result.status == "confirmed" and result.timestamp == T0


@pytest.mark.parametrize(
    "overrides",
    [
        {"to": "0x" + "11" * 20},
        {"from": "0x" + "11" * 20},
        {"to": None},
        {"value": 0},
        {"to": "invalid"},
    ],
)
async def test_unrelated_transactions_ignored(config, client, overrides):
    client.transaction.return_value.update(overrides)
    assert await verify(client, config, HASH) is None
    client.receipt.assert_not_awaited()


async def test_failed_receipt_ignored(config, client):
    client.receipt.return_value["status"] = 0
    assert await verify(client, config, HASH) is None


async def test_insufficient_confirmations_are_pending(config, client):
    result = await verify(client, config, HASH, head=15)
    assert result.confirmations == 6 and result.status == "pending"


async def test_reorg_mismatch_rejected(config, client):
    client.receipt.return_value["blockHash"] = "0x" + "ff" * 32
    with pytest.raises(RPCError, match="изменился"):
        await verify(client, config, HASH)


async def test_mempool_not_counted(config, client):
    client.transaction.return_value["blockNumber"] = None
    with pytest.raises(RPCError, match="не включена"):
        await verify(client, config, HASH)


async def test_nonce_search_is_logarithmic():
    client = AsyncMock()
    client.nonce.side_effect = lambda sender, number: 4 if number < 700_000 else 5
    assert await next_sender_block(client, "wallet", 1, 1_000_000) == 700_000
    assert client.nonce.await_count <= 22
    client.block.assert_not_called()


async def test_unchanged_nonce_skips_whole_interval():
    client = AsyncMock()
    client.nonce.return_value = 4
    assert await next_sender_block(client, "wallet", 100, 1_000_000) is None
    assert client.nonce.await_count == 2


async def test_decreasing_nonce_is_not_accepted_as_complete():
    client = AsyncMock()
    client.nonce.side_effect = [4, 2]
    with pytest.raises(RPCError, match="уменьшился"):
        await next_sender_block(client, "wallet", 1, 10)


async def test_timestamp_binary_search():
    client = AsyncMock()
    client.block.side_effect = lambda number: {"timestamp": int(T0.timestamp()) + number * 2}
    assert await block_before(client, T0 + timedelta(seconds=1001), 1000) == 500
    assert client.block.await_count < 15


async def test_no_genesis_or_future_history_scan():
    client = AsyncMock()
    client.block.side_effect = lambda number: {"timestamp": int(T0.timestamp()) + number * 2}
    with pytest.raises(RPCError, match="genesis"):
        await block_before(client, T0, 1000)
    with pytest.raises(RPCError, match="позже"):
        await block_before(client, T0 + timedelta(days=1), 1000)


async def test_rpc_failure_redacts_url_and_does_not_escape(config):
    client = BaseClient(config)
    try:

        async def fail():
            raise OSError("https://secret-key-at-provider.example")

        with pytest.raises(RPCError) as error:
            await client._call(fail, "eth_getBlockByNumber")
        assert "secret-key" not in str(error.value)
    finally:
        await client.close()


def test_hash_validation(config):
    assert transaction_hash(HASH.upper().replace("0X", "0x")) == HASH
    with pytest.raises(ValueError):
        transaction_hash("0x1234")
    assert not matches({}, config)
