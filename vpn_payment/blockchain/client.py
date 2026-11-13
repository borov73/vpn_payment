"""The only RPC adapter. Intentionally exposes no transaction signing/sending API."""

import asyncio
import ssl
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

import certifi
from web3 import AsyncHTTPProvider, AsyncWeb3
from web3.exceptions import TransactionNotFound

from vpn_payment.config import Settings

T = TypeVar("T")


class RPCError(RuntimeError):
    """Safe-to-display error; never includes endpoint credentials."""


class MissingTransaction(RPCError):
    pass


def hexstr(value) -> str:
    if isinstance(value, str):
        return value.lower()
    return "0x" + bytes(value).hex()


class BaseClient:
    def __init__(self, config: Settings):
        self.config = config
        tls = ssl.create_default_context()
        tls.load_verify_locations(certifi.where())
        self.w3 = AsyncWeb3(
            AsyncHTTPProvider(
                config.base_rpc_url.get_secret_value(),
                request_kwargs={"timeout": config.rpc_timeout_seconds, "ssl": tls},
                exception_retry_configuration=None,
            )
        )
        self._request_lock = asyncio.Lock()
        self._last_call = 0.0

    async def _call(self, action: Callable[[], Awaitable[T]], label: str) -> T:
        for attempt in range(self.config.rpc_retries):
            try:
                async with self._request_lock:
                    delay = self.config.rpc_request_interval_seconds - (
                        time.monotonic() - self._last_call
                    )
                    if delay > 0:
                        await asyncio.sleep(delay)
                    self._last_call = time.monotonic()
                    return await action()
            except TransactionNotFound:
                raise MissingTransaction("Транзакция или receipt пока не найдены в RPC.") from None
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if attempt + 1 == self.config.rpc_retries:
                    raise RPCError(
                        f"RPC: {label} недоступен ({type(error).__name__}); курсор сохранён. Проверьте RPC, лимиты и поддержку исторического состояния."
                    ) from None
                await asyncio.sleep(min(2**attempt, 4))
        raise AssertionError("unreachable")

    async def check(self) -> int:
        chain_id = await self._call(lambda: self.w3.eth.chain_id, "eth_chainId")
        if chain_id != 8453:
            raise RPCError(f"Неверная сеть: chain ID {chain_id}, нужен Base Mainnet 8453.")
        return await self.head()

    async def head(self) -> int:
        return await self._call(lambda: self.w3.eth.block_number, "eth_blockNumber")

    async def block(self, number: int, full: bool = False):
        return await self._call(
            lambda: self.w3.eth.get_block(number, full_transactions=full), "eth_getBlockByNumber"
        )

    async def nonce(self, sender: str, block: int) -> int:
        return await self._call(
            lambda: self.w3.eth.get_transaction_count(sender, block_identifier=block),
            "eth_getTransactionCount",
        )

    async def code(self, sender: str, block: int) -> bytes:
        return await self._call(
            lambda: self.w3.eth.get_code(sender, block_identifier=block), "eth_getCode"
        )

    async def transaction(self, tx_hash: str):
        return await self._call(
            lambda: self.w3.eth.get_transaction(tx_hash), "eth_getTransactionByHash"
        )

    async def receipt(self, tx_hash: str):
        return await self._call(
            lambda: self.w3.eth.get_transaction_receipt(tx_hash), "eth_getTransactionReceipt"
        )

    async def close(self) -> None:
        await self.w3.provider.disconnect()
