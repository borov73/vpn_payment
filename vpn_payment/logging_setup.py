import logging

from vpn_payment.config import Settings


class RedactingFormatter(logging.Formatter):
    def __init__(self, secrets: list[str]):
        super().__init__("%(asctime)s %(levelname)s %(name)s: %(message)s")
        self.secrets = sorted((value for value in secrets if value), key=len, reverse=True)

    def format(self, record: logging.LogRecord) -> str:
        result = super().format(record)
        for value in self.secrets:
            result = result.replace(value, "[REDACTED]")
        return result


def configure_logging(config: Settings) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(
        RedactingFormatter(
            [
                config.telegram_bot_token.get_secret_value(),
                config.base_rpc_url.get_secret_value(),
            ]
        )
    )
    logging.basicConfig(level=config.log_level, handlers=[handler], force=True)
    logging.getLogger("web3").setLevel(logging.WARNING)
    logging.getLogger("ccxt").setLevel(logging.WARNING)
