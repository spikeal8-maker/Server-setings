#!/usr/bin/env python3
"""Install only the versioned VM100 GFS operator shortcut (no backup, no reboot)."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import socket
import stat
import sys
import tempfile

SOURCE = Path(__file__).resolve().parent
DESTINATION = Path("/usr/local/bin/asa-school-gfs")


class InstallError(RuntimeError):
    pass


def require(condition: bool, code: str) -> None:
    if not condition:
        raise InstallError(code)


def is_managed_link(previous: Path, releases_dir: Path) -> bool:
    return (previous.is_absolute() and previous.name == "asa-school-gfs-cli.py"
            and previous.parent.parent == releases_dir
            and re.fullmatch(r"[a-f0-9]{40}", previous.parent.name) is not None)


def preflight() -> Path:
    require(os.geteuid() == 0 and socket.gethostname() == "asa-school-01",
            "VM100_ROOT_REQUIRED")
    require(SOURCE.parent.name == "releases" and
            re.fullmatch(r"[a-f0-9]{40}", SOURCE.name) is not None,
            "PINNED_GITHUB_RELEASE_REQUIRED")
    cli = SOURCE / "asa-school-gfs-cli.py"
    for file in (cli, SOURCE / "school-yandex-restore.py"):
        require(file.is_file() and not file.is_symlink() and file.stat().st_uid == 0,
                "VERSIONED_CLI_OR_RESTORE_UNAVAILABLE")
    require(DESTINATION.parent.is_dir() and not DESTINATION.parent.is_symlink(),
            "SYSTEM_BIN_UNAVAILABLE")
    if DESTINATION.exists() or DESTINATION.is_symlink():
        require(DESTINATION.is_symlink(), "EXISTING_SHORTCUT_NOT_MANAGED")
        previous = Path(os.readlink(DESTINATION))
        require(is_managed_link(previous, SOURCE.parent),
                "FOREIGN_SHORTCUT_REFUSE_OVERWRITE")
    return cli


def apply() -> None:
    cli = preflight()
    cli.chmod(0o755)
    if DESTINATION.is_symlink() and os.readlink(DESTINATION) == str(cli):
        print("GFS_OPERATOR_ALREADY_INSTALLED", str(DESTINATION))
        return
    fd, path = tempfile.mkstemp(prefix=".asa-school-gfs-", dir=DESTINATION.parent)
    os.close(fd)
    os.unlink(path)
    try:
        os.symlink(cli, path)
        os.replace(path, DESTINATION)
    finally:
        if os.path.lexists(path):
            os.unlink(path)
    require(DESTINATION.is_symlink() and
            os.readlink(DESTINATION) == str(cli), "SHORTCUT_READBACK_FAILED")
    print("GFS_OPERATOR_INSTALLED", str(DESTINATION), "->", str(cli))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.apply:
            apply()
        else:
            print("GFS_OPERATOR_PREFLIGHT_PASS", preflight())
        return 0
    except (OSError, InstallError, ValueError) as error:
        code = str(error) if isinstance(error, InstallError) else type(error).__name__
        print("GFS_OPERATOR_INSTALL_BLOCKED=" + code, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
