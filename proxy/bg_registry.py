import os
import subprocess
import threading
import uuid
from datetime import datetime
from pathlib import Path

# w:\_python\APIPROXY\temp\ already exists for dumps; reuse it
_LOG_DIR = Path(__file__).resolve().parent.parent / "temp" / "bg"
_LOG_DIR.mkdir(parents=True, exist_ok=True)

_LOCK = threading.Lock()
_PROCS: dict = {}  # bg_id -> {popen, log_path, log_fh, cmd, started_at}


def start(cmd: str, cwd: str | None = None) -> dict:
    """Launch cmd via shell, detach, return {bg_id, cmd, started_at}.
    Raises on Popen failure (caller turns it into bg_error)."""
    bg_id = uuid.uuid4().hex[:8]
    log_path = _LOG_DIR / f"{bg_id}.log"
    log_fh = open(log_path, "wb", buffering=0)
    started_at = datetime.now().isoformat(timespec="seconds")
    popen = subprocess.Popen(
        cmd,
        shell=True,
        cwd=cwd or os.getcwd(),
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
    )
    with _LOCK:
        _PROCS[bg_id] = {
            "popen": popen,
            "log_path": str(log_path),
            "log_fh": log_fh,
            "cmd": cmd,
            "started_at": started_at,
        }
    return {"bg_id": bg_id, "cmd": cmd, "started_at": started_at}


def status(bg_id: str) -> dict | None:
    with _LOCK:
        rec = _PROCS.get(bg_id)
    if not rec:
        return None
    p = rec["popen"]
    code = p.poll()
    return {
        "bg_id": bg_id,
        "alive": code is None,
        "pid": p.pid,
        "exit_code": code,
        "started_at": rec["started_at"],
        "cmd": rec["cmd"],
    }


def tail(bg_id: str, lines: int = 50) -> str | None:
    with _LOCK:
        rec = _PROCS.get(bg_id)
    if not rec:
        return None
    try:
        with open(rec["log_path"], "rb") as f:
            data = f.read()
    except FileNotFoundError:
        return ""
    text = data.decode("utf-8", errors="replace")
    return "\n".join(text.splitlines()[-lines:])


def stop(bg_id: str) -> dict | None:
    with _LOCK:
        rec = _PROCS.get(bg_id)
    if not rec:
        return None
    p = rec["popen"]
    if p.poll() is None:
        p.terminate()
        try:
            p.wait(timeout=3)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait(timeout=2)
    try:
        rec["log_fh"].close()
    except Exception:
        pass
    return {"bg_id": bg_id, "exit_code": p.returncode}
