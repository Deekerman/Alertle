"""Self-update helper — downloads the latest main branch from GitHub and applies it in-place."""

from __future__ import annotations

import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Callable, Optional

import requests

REPO_RAW = "https://raw.githubusercontent.com/Deekerman/Alertle/main"
REPO_ARCHIVE = "https://github.com/Deekerman/Alertle/archive/refs/heads/main.tar.gz"

_RSYNC_EXCLUDES = [
    ".git",
    "__pycache__",
    "*.pyc",
    "*.db",
    "alertle.db",
    "config.yaml",
]


def fetch_latest_version() -> str:
    """Return the VERSION string from the main branch. Raises on any error."""
    resp = requests.get(f"{REPO_RAW}/VERSION", timeout=10)
    resp.raise_for_status()
    return resp.text.strip()


def apply_update(
    install_dir: Path,
    venv_dir: Path,
    log_fn: Optional[Callable[[str], None]] = None,
) -> None:
    """Download main.tar.gz, extract, rsync into install_dir, reinstall deps."""

    def _log(msg: str) -> None:
        if log_fn:
            log_fn(msg)

    _log("Downloading latest source from GitHub…")
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        tarball = tmp_path / "main.tar.gz"

        resp = requests.get(REPO_ARCHIVE, timeout=60, stream=True)
        resp.raise_for_status()
        with tarball.open("wb") as f:
            for chunk in resp.iter_content(chunk_size=65536):
                f.write(chunk)
        _log("Download complete. Extracting…")

        with tarfile.open(tarball, "r:gz") as tf:
            # filter='data' strips unsafe members (absolute paths, device files);
            # available in 3.12+, silently ignored on older versions via getattr
            kwargs = {"filter": "data"} if hasattr(tarfile.TarFile, "extraction_filter") else {}
            tf.extractall(tmp_path, **kwargs)

        # GitHub tarballs extract to a single top-level directory like Alertle-main/
        extracted_dirs = [d for d in tmp_path.iterdir() if d.is_dir() and d.name != "__MACOSX"]
        if not extracted_dirs:
            raise RuntimeError("Tarball contained no top-level directory")
        src = extracted_dirs[0]
        _log(f"Extracted to {src.name}/")

        exclude_args = []
        for ex in _RSYNC_EXCLUDES:
            exclude_args += ["--exclude", ex]

        _log("Applying files…")
        rsync_cmd = [
            "rsync", "-a", "--delete",
            *exclude_args,
            str(src) + "/",
            str(install_dir) + "/",
        ]
        result = subprocess.run(rsync_cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"rsync failed: {result.stderr.strip()}")
        _log("Files applied.")

        pip = venv_dir / "bin" / "pip"
        if not pip.exists():
            pip = Path(sys.executable).parent / "pip"

        req_file = install_dir / "requirements.txt"
        if req_file.exists():
            _log("Installing Python dependencies…")
            pip_result = subprocess.run(
                [str(pip), "install", "--quiet", "-r", str(req_file)],
                capture_output=True,
                text=True,
            )
            if pip_result.returncode != 0:
                raise RuntimeError(f"pip failed: {pip_result.stderr.strip()}")
            _log("Dependencies up to date.")

    _log("Update applied successfully. Restarting service…")
