import re
from datetime import UTC, datetime

from vpn_payment.blockchain.client import BaseClient, RPCError, hexstr
from vpn_payment.config import Settings, address
from vpn_payment.database.models import VerifiedTransfer


def transaction_hash(value: str) -> str:
    if not re.fullmatch(r"0x[0-9a-fA-F]{64}", value.strip()):
        raise ValueError("Transaction hash must be 0x and 64 hex digits")
    return value.strip().lower()


def matches(tx: dict, config: Settings) -> bool:
    try:
        return (
            address(tx["from"]) == config.sender_address
            and tx.get("to") is not None
            and address(tx["to"]) in config.recipient_addresses
            and int(tx["value"]) > 0
        )
    except (KeyError, ValueError, TypeError):
        return False


async def verify(
    client: BaseClient,
    config: Settings,
    tx_hash: str,
    head: int | None = None,
) -> VerifiedTransfer | None:
    tx_hash = transaction_hash(tx_hash)
    tx = await client.transaction(tx_hash)
    if not matches(tx, config):
        return None
    if tx.get("blockNumber") is None:
        raise RPCError("Транзакция ещё не включена в блок; повторите импорт позже.")
    receipt = await client.receipt(tx_hash)
    if int(receipt["status"]) != 1:
        return None
    if hexstr(tx["hash"]) != tx_hash or hexstr(receipt["transactionHash"]) != tx_hash:
        raise RPCError("RPC вернул несовпадающий hash транзакции/receipt.")
    block = await client.block(int(tx["blockNumber"]))
    canonical = hexstr(block["hash"])
    if (
        hexstr(tx["blockHash"]) != canonical
        or hexstr(receipt["blockHash"]) != canonical
        or int(receipt["blockNumber"]) != int(tx["blockNumber"])
    ):
        raise RPCError("Блок транзакции изменился (возможная реорганизация); повторите проверку.")
    latest = head if head is not None else await client.head()
    confirmations = max(0, latest - int(tx["blockNumber"]) + 1)
    return VerifiedTransfer(
        tx_hash,
        address(tx["from"]),
        address(tx["to"]),
        int(tx["value"]),
        int(tx["blockNumber"]),
        canonical,
        datetime.fromtimestamp(int(block["timestamp"]), UTC),
        confirmations,
        "confirmed" if confirmations >= config.confirmations else "pending",
    )
