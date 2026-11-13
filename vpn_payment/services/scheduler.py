import asyncio
import logging

from vpn_payment.blockchain.monitor import Monitor
from vpn_payment.config import Settings
from vpn_payment.services.notifications import Notifications

log = logging.getLogger(__name__)


async def sync_worker(config: Settings, monitor: Monitor, notifications: Notifications) -> None:
    failures = 0
    while True:
        delay = config.sync_interval_seconds
        try:
            report = await monitor.sync()
            if report.errors:
                failures += 1
                log.warning("Sync incomplete: %s", "; ".join(report.errors))
                delay = min(config.sync_interval_seconds, 30 * 2 ** min(failures - 1, 5))
            else:
                failures = 0
                log.info("Sync finished: added=%d priced=%d", report.added, report.priced)
                for name in ("live", "history"):
                    state = await monitor.repo.scan(name)
                    if state and state["target"] and state["cursor"] < state["target"]:
                        delay = 5
                live = await monitor.repo.scan("live")
                target = await monitor.repo.meta("last_confirmed_target")
                if live and target and live["cursor"] < int(target):
                    delay = 5
            await notifications.tick()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            log.error("Sync worker failed (%s)", type(error).__name__)
            try:
                await monitor.repo.set_meta(
                    "last_live_error", f"Worker error: {type(error).__name__}"
                )
                await monitor.repo.set_meta("last_error", f"Worker error: {type(error).__name__}")
            except Exception as db_error:
                log.error("Unable to persist sync error (%s)", type(db_error).__name__)
            delay = min(config.sync_interval_seconds, 60)
        await asyncio.sleep(delay)


async def notification_worker(config: Settings, notifications: Notifications) -> None:
    while True:
        try:
            await notifications.tick()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            log.error("Notification worker failed (%s)", type(error).__name__)
        await asyncio.sleep(config.notification_interval_seconds)
