from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from web3 import Web3

from vpn_payment.billing.models import utc


def address(value: str) -> str:
    """Validate 20-byte hex address, store canonical EIP-55 spelling.

    Input case is normalized rather than treated as proof of checksum validity.
    No ENS lookups and no truncated/padded addresses are accepted.
    """
    if not isinstance(value, str) or not value.startswith("0x") or not Web3.is_address(value):
        raise ValueError("Expected an EVM address: 0x and exactly 40 hex digits")
    return Web3.to_checksum_address(value)


def parse_time(value: str) -> datetime:
    return utc(datetime.fromisoformat(value.strip().replace("Z", "+00:00")))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)

    telegram_bot_token: SecretStr = SecretStr("")
    telegram_user_id: int | None = None
    base_rpc_url: SecretStr = SecretStr("https://mainnet.base.org")
    sender_address: str
    recipient_addresses: Annotated[tuple[str, ...], NoDecode]
    active_recipient_addresses: Annotated[tuple[str, ...], NoDecode]
    database_path: Path = Path("var/vpn_payment.sqlite3")
    history_start: datetime
    history_mode: Literal["nonce", "manual"] = "nonce"
    monitor_mode: Literal["nonce", "blocks"] = "nonce"
    confirmations: int = Field(default=64, ge=1, le=100_000)
    sync_interval_seconds: int = Field(default=900, ge=10)
    notification_interval_seconds: int = Field(default=60, ge=10)
    max_tx_blocks_per_sync: int = Field(default=50, ge=1, le=1000)
    max_blocks_per_sync: int = Field(default=1000, ge=1, le=10000)
    rpc_request_interval_seconds: float = Field(default=0.15, ge=0)
    rpc_timeout_seconds: int = Field(default=20, ge=1, le=120)
    rpc_retries: int = Field(default=3, ge=1, le=5)
    price_exchanges: Annotated[tuple[str, ...], NoDecode] = ("bitstamp", "coinbaseexchange")
    price_timeout_seconds: int = Field(default=20, ge=1, le=120)
    monthly_price_eur: Decimal = Field(default=Decimal("4.00"), gt=0, le=10000, decimal_places=2)
    period_days: int = Field(default=30, ge=1, le=366)
    subscription_start: datetime | None = None
    notifications_enabled: bool = True
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    @field_validator("sender_address")
    @classmethod
    def normalize_sender(cls, value: str) -> str:
        return address(value)

    @field_validator("recipient_addresses", "active_recipient_addresses", mode="before")
    @classmethod
    def normalize_recipients(cls, value):
        parts = value.split(",") if isinstance(value, str) else value
        result = tuple(dict.fromkeys(address(part.strip()) for part in parts))
        if not result:
            raise ValueError("At least one recipient is required")
        return result

    @field_validator("price_exchanges", mode="before")
    @classmethod
    def exchanges(cls, value):
        parts = value.split(",") if isinstance(value, str) else value
        result = tuple(dict.fromkeys(part.strip() for part in parts))
        if not result or any(x not in {"bitstamp", "coinbaseexchange"} for x in result):
            raise ValueError("Supported exchanges: bitstamp,coinbaseexchange")
        return result

    @model_validator(mode="after")
    def active_recipients_known(self):
        if not set(self.active_recipient_addresses) <= set(self.recipient_addresses):
            raise ValueError("Active recipients must also be included in RECIPIENT_ADDRESSES")
        return self

    @field_validator("history_start", "subscription_start", mode="before")
    @classmethod
    def empty_date(cls, value):
        return None if value == "" else value

    @field_validator("history_start", "subscription_start")
    @classmethod
    def aware_date(cls, value):
        return utc(value) if value else value

    @field_validator("telegram_user_id", mode="before")
    @classmethod
    def empty_user(cls, value):
        if value in (None, ""):
            return None
        if int(value) <= 0:
            raise ValueError("TELEGRAM_USER_ID must be a positive integer")
        return value

    @field_validator("base_rpc_url")
    @classmethod
    def rpc_url(cls, value: SecretStr) -> SecretStr:
        parsed = urlsplit(value.get_secret_value())
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("BASE_RPC_URL must be an HTTP(S) URL")
        return value

    def require_telegram(self) -> None:
        if not self.telegram_bot_token.get_secret_value() or not self.telegram_user_id:
            raise ValueError("Fill TELEGRAM_BOT_TOKEN and TELEGRAM_USER_ID in .env first")
