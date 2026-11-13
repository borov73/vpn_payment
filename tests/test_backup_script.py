"""Exercise the deployed Bash script with GNU find and a stub backup command."""

import os
import shlex
import shutil
import subprocess
import time
from pathlib import Path

import pytest

find = shutil.which("find")
probe = subprocess.run([find, "--version"], capture_output=True, text=True) if find else None
pytestmark = pytest.mark.skipif(
    probe is None or "GNU findutils" not in probe.stdout,
    reason="Backup deployment script targets Ubuntu GNU find",
)
SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "backup.sh"


def prepare(tmp_path, *, fail=False):
    project = tmp_path / "project"
    executable = project / ".venv" / "bin" / "python"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/usr/bin/env bash\n" + ("exit 1\n" if fail else 'touch "${@: -1}"\n'))
    executable.chmod(0o700)
    backups = tmp_path / "backups"
    backups.mkdir()
    script = tmp_path / "backup.sh"
    script.write_text(
        SCRIPT.read_text()
        .replace("cd /opt/vpn_payment", "cd " + shlex.quote(str(project)))
        .replace("backup_dir=/var/backups/vpn-payment", "backup_dir=" + shlex.quote(str(backups)))
    )
    return script, backups


def aged(path, days):
    path.write_text("synthetic backup")
    timestamp = time.time() - days * 86400
    os.utime(path, (timestamp, timestamp))
    return path


def test_retention_removes_only_expired_automatic_files(tmp_path):
    script, backups = prepare(tmp_path)
    expired = aged(backups / "vpn-payment-20260101T000000-1.sqlite3", 5.01)
    expired_wal = aged(Path(str(expired) + "-wal"), 5.01)
    expired_shm = aged(Path(str(expired) + "-shm"), 0)
    retained = aged(backups / "vpn-payment-20260102T000000-2.sqlite3", 4.99)
    retained_wal = aged(Path(str(retained) + "-wal"), 60)
    manual = aged(backups / "before-update-20260101.sqlite3", 60)
    cli_copy = aged(backups / "vpn-payment-20260101T000000000000Z.sqlite3", 60)
    unrelated = aged(backups / "another-project.sqlite3", 60)
    directory = backups / "archive"
    directory.mkdir()
    nested = aged(directory / expired.name, 60)
    outside = aged(tmp_path / "outside.sqlite3", 60)
    link = backups / "vpn-payment-20260101T000000-3.sqlite3"
    link.symlink_to(outside)
    subprocess.run(["bash", str(script)], check=True, capture_output=True, text=True)
    assert not expired.exists()
    assert not expired_wal.exists() and not expired_shm.exists()
    assert retained_wal.exists()
    assert all(p.exists() for p in (retained, manual, cli_copy, unrelated, nested, outside))
    assert link.is_symlink()
    fresh = [
        p for p in backups.glob("vpn-payment-*.sqlite3") if p.is_file() and p.stat().st_size == 0
    ]
    assert len(fresh) == 1 and fresh[0].stat().st_mode & 0o777 == 0o600


def test_failed_backup_keeps_expired_copies(tmp_path):
    script, backups = prepare(tmp_path, fail=True)
    expired = aged(backups / "vpn-payment-20260101T000000-1.sqlite3", 60)
    result = subprocess.run(["bash", str(script)], capture_output=True, text=True)
    assert result.returncode != 0
    assert expired.read_text() == "synthetic backup"
    assert list(backups.iterdir()) == [expired]
