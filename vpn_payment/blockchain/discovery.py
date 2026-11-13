"""Bounded RPC discovery without logs, indexers or a genesis scan."""

from datetime import datetime

from vpn_payment.blockchain.client import BaseClient, RPCError


async def block_before(client: BaseClient, since: datetime, head: int) -> int:
    """Last block strictly before since. Binary search timestamps, not transactions."""
    target = int(since.timestamp())
    latest = await client.block(head)
    if target > int(latest["timestamp"]):
        raise RPCError("HISTORY_START позже подтверждённой вершины; история ещё не наступила.")
    first = await client.block(0)
    if target <= int(first["timestamp"]):
        raise RPCError(
            "HISTORY_START должен быть позже genesis Base; сканирование от genesis запрещено."
        )
    low, high = 0, head
    while low + 1 < high:
        mid = (low + high) // 2
        if int((await client.block(mid))["timestamp"]) < target:
            low = mid
        else:
            high = mid
    return low


async def next_sender_block(
    client: BaseClient, sender: str, cursor: int, target: int
) -> int | None:
    """Find first block with outgoing EOA activity in (cursor, target].

    Any top-level transaction by the sender increments its nonce, successful or
    failed. EIP-7702 authorizations can add extra changes, which only cause extra
    block inspections. We never use a changed nonce as proof of a payment.
    Non-monotonic nonces are rejected; smart-contract wallet internals are out of scope.
    """
    before = await client.nonce(sender, cursor)
    after = await client.nonce(sender, target)
    if after < before:
        raise RPCError("Nonce уменьшился: RPC/тип кошелька не подходит для nonce-поиска.")
    if before == after:
        return None
    low, high = cursor + 1, target
    while low < high:
        mid = (low + high) // 2
        value = await client.nonce(sender, mid)
        if not before <= value <= after:
            raise RPCError("Исторические nonce непоследовательны; курсор не продвинут.")
        if value > before:
            high = mid
        else:
            low = mid + 1
    return low
