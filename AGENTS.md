# VPN Payment Tracker: development rules

Personal **read-only** native ETH tracker for Base (8453). Never sign or send
blockchain transactions, request wallet keys/seed phrases, publish or push without
explicit user authorization. Never print .env, tokens or credential URLs.

## Scope and architecture

- Telegram has only Balance and Top-ups. Show concise cards, Moscow dates and EUR;
  no transaction metadata, statistics, settings or admin actions. Owner ID and
  private chat checks apply to messages AND callbacks; obsolete buttons are inert.
- The tariff footer is only `4,00 € / 30 дней`, without a market-value label.
  Keep the explanation of EUR market estimates in documentation.
- Continuous bookkeeping only: full upfront periods from confirmed start, no daily
  proration or automatic pauses/restarts. Carry negative book balances; later funds
  cover the deficit. Exclude future payments from past as-of snapshots. Never call
  this provider billing, debt, activation or guaranteed service availability.
- Rules and rare EUR corrections are administered through CLI with preview, explicit
  --apply, backup and audit. Do not restore a Telegram settings UI.
  Preview must calculate the actual saved ledger with the proposed change before
  writing anything; reject unsupported dates/amounts. CLI amounts are in EUR.
- RECIPIENT_ADDRESSES preserves both old and current recipients for history;
  ACTIVE_RECIPIENT_ADDRESSES contains only current recipients.
  New transfers to retired addresses are excluded. Persist the technical retirement
  block on first sync; never invent the provider's actual switch date. Rescans must
  not credit later retired-address transfers. Keep recorded historical payments.
  Use the same retirement boundary for live discovery, rescans and manual imports.
- Wallet addresses and HISTORY_START are required private configuration, without
  personal defaults in source. Keep personal addresses, transaction hashes, server
  credentials/hostnames/IPs and real payment details out of publishable files;
  tests and documentation use synthetic fixtures. Preserve the working private .env.
- blockchain/client.py is the only RPC adapter and exposes read operations only.
  Discover EOA outgoing activity through historical nonce bisection; verify sender,
  recipient, positive value, successful receipt, canonical block and confirmations.
  Never use logs for native ETH history or scan the chain from genesis.
- Preserve separate history/live cursors and honest coverage. Manual hashes do not
  prove completeness. RPC nonce discovery plus manual import suffice; no indexer.
  Retain the bounded new-block fallback for RPCs without historical state.
- Audit pending/recent transfers and canonical cursor checkpoints. Recheck ranges
  on reorg; isolated audit errors must not prevent independent live discovery.
  Persist unfinished full audits across restart; never clear an error just because
  an unverified payment aged out of the recent window.
  Suppress precise reminders while data are incomplete or stale.
- CCXT is market data only: direct ETH/EUR, OPEN of the exact closed minute with
  trades. No current/future price substitute. Preserve source, candle, raw EUR,
  frozen original estimate and adjustment audit. Decimal money, integer-string wei;
  ROUND_HALF_UP once per payment to cents.
- SQLite is persistent. Back up before schema migration, reject unknown future
  schemas. status/backup use existing read-only connections without migrations;
  backup never creates a missing source. Never replace a running DB.
- Daily backup retention is 5 days. Delete only expired automatic copies after a
  successful verified new backup; preserve manual copies, symlinks, directories and
  the working database. The full payment history is never subject to this retention.
- Notify once per confirmed and valued new live payment: EUR, balance and balance
  after next charge. Wait for missing historical quotes. Remind 7/3/1 days before
  the funded-through date, accounting for advance funds.
- Reserve notification keys before delivery. Retry explicit Telegram retry_after
  rejections after a persisted deadline; never retry uncertain delivery blindly.
- Keep KISS. No Docker, Redis, PostgreSQL, multi-subscription framework or ORM.
  Keep process locks, backups, secret redaction and graceful shutdown.

## Required checks

Use Python 3.12+ and this project's environment:

```sh
.venv/bin/python -m pytest -q
.venv/bin/ruff check vpn_payment tests
.venv/bin/ruff format --check vpn_payment tests
.venv/bin/python -m compileall -q vpn_payment
.venv/bin/python -m vpn_payment doctor
.venv/bin/python -m pip check
```

Tests are offline with mock APIs and temporary SQLite files. Preserve meaningful
regressions for money, address retirement, cursors/reorgs, missing receipts/quotes,
notifications/restart/rate limits, auth, read-only backup and migration without data loss.
Record separate live read-only checks and their limits in docs/VERIFICATION.md.
Keep README, .env.example, dependency locks and systemd instructions consistent.
Do not present synthetic payments or mocked delivery as real events.
