from datetime import timedelta

from tests.conftest import T0
from vpn_payment.blockchain.client import MissingTransaction, RPCError


def h(number):
    return f"0x{number:064x}"


class FakeChain:
    def __init__(self, config):
        self.config = config
        self.height = 200
        self.txs = {}
        self.failed = set()
        self.nonce_calls = 0
        self.block_calls = []
        self.fail_nonce_below = -1
        self.fail_all = False
        self.fork_from = 10**12
        self.receipt_calls = []
        self.fail_receipts = set()

    def block_hash(self, number):
        return h(number + 1_000_000 + (10_000_000 if number >= self.fork_from else 0))

    def add(self, number, *, recipient=None, value=2_000_000_000_000_000, success=True, unique=0):
        key = h(number * 100 + unique)
        self.txs[key] = {
            "hash": key,
            "from": self.config.sender_address,
            "to": recipient or self.config.active_recipient_addresses[0],
            "value": value,
            "blockNumber": number,
            "blockHash": self.block_hash(number),
        }
        if not success:
            self.failed.add(key)
        return key

    async def check(self):
        if self.fail_all:
            raise RPCError("RPC unavailable")
        return self.height

    async def head(self):
        return self.height

    async def block(self, number, full=False):
        self.block_calls.append((number, full))
        return {
            "number": number,
            "hash": self.block_hash(number),
            "timestamp": int((T0 + timedelta(seconds=(number - 1) * 2)).timestamp()),
            "transactions": [
                dict(tx, blockHash=self.block_hash(number)) if full else key
                for key, tx in self.txs.items()
                if tx["blockNumber"] == number
            ],
        }

    async def nonce(self, sender, block):
        self.nonce_calls += 1
        if block < self.fail_nonce_below:
            raise RPCError("Historical state unavailable")
        return sum(tx["from"] == sender and tx["blockNumber"] <= block for tx in self.txs.values())

    async def code(self, sender, block):
        return b""

    async def transaction(self, key):
        if key not in self.txs:
            raise MissingTransaction("Not found")
        tx = self.txs[key]
        return dict(tx, blockHash=self.block_hash(tx["blockNumber"]))

    async def receipt(self, key):
        self.receipt_calls.append(key)
        if key in self.fail_receipts:
            raise RPCError("Old receipt unavailable")
        tx = await self.transaction(key)
        return {
            "status": 0 if key in self.failed else 1,
            "transactionHash": key,
            "blockNumber": tx["blockNumber"],
            "blockHash": tx["blockHash"],
        }
