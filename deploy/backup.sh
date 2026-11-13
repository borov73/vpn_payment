#!/usr/bin/env bash
set -euo pipefail
umask 077
cd /opt/vpn_payment
backup_dir=/var/backups/vpn-payment
destination="${backup_dir}/vpn-payment-$(date -u +%Y%m%dT%H%M%S)-${RANDOM}.sqlite3"
.venv/bin/python -m vpn_payment backup --output "$destination"
# Prune only this script's automatic copies, after a successful verified backup.
# Keep manual/pre-update copies, symlinks and subdirectories untouched.
find "$backup_dir" -maxdepth 1 -regextype posix-extended -type f \
    -regex '.*/vpn-payment-[0-9]{8}T[0-9]{6}-[0-9]+\.sqlite3' \
    ! -newermt '5 days ago' -exec bash -e -c '
        for snapshot do
            rm -- "$snapshot"
            # SQLite may leave these files after a backup is opened for inspection.
            for companion in "$snapshot-wal" "$snapshot-shm"; do
                if [[ -f "$companion" && ! -L "$companion" ]]; then
                    rm -- "$companion"
                fi
            done
        done
    ' bash {} +
