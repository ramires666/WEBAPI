"""Tee-wrapper: запускает run.py, дублирует stdout+stderr в консоль и в файл.

Зачем: PowerShell 5.1 ломает stderr нативных exe (NativeCommandError),
cmd не имеет tee. Простой Python-обёртка — самый надёжный путь.

Использование:
    python scripts/run_tee.py
    python scripts/run_tee.py --log logs/custom.log
"""
from __future__ import annotations

import argparse
import datetime
import os
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOGS_DIR = ROOT / "logs"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default=None, help="путь к лог-файлу (по умолч. logs/server_<ts>.log)")
    args = ap.parse_args()

    LOGS_DIR.mkdir(exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = Path(args.log) if args.log else LOGS_DIR / f"server_{ts}.log"
    latest = LOGS_DIR / "latest.log"

    # latest.log = hardlink на свежий лог (для фикс-пути чтения).
    # На NTFS hardlink работает на одном томе.
    try:
        if latest.exists() or latest.is_symlink():
            latest.unlink()
        log_path.touch()
        os.link(str(log_path), str(latest))
        print(f"==> latest.log = hardlink -> {log_path.name}", flush=True)
    except OSError as e:
        print(f"==> hardlink failed ({e}); latest.log будет копией после остановки", flush=True)

    print(f"==> Лог: {log_path}", flush=True)
    print(f"==> Зеркало: {latest}", flush=True)
    print("", flush=True)

    # Открываем лог-файл в append-binary (writes сырые байты, не трогаем кодировку).
    log_f = open(log_path, "ab", buffering=0)

    # Спавним run.py с PIPE на stdout, stderr -> stdout. -u = unbuffered.
    # PYTHONIOENCODING=utf-8 — иначе на Windows дочерний Python кодирует stdout
    # в cp866 (консольную CP), и кириллица в лог-файле превращается в кракозябры.
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.Popen(
        [sys.executable, "-u", str(ROOT / "run.py")],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=str(ROOT),
        bufsize=0,
        env=env,
    )

    # Ctrl+C — пробрасываем в дочерний.
    def _forward(signum, _frame):
        try:
            proc.send_signal(signum)
        except Exception:
            pass

    signal.signal(signal.SIGINT, _forward)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _forward)

    out = sys.stdout.buffer
    try:
        assert proc.stdout is not None
        # Построчное чтение: blocking readline() ждёт newline, а не крутит CPU
        # на каждый байт. loguru/print пишут построчно — устраивает.
        for line in iter(proc.stdout.readline, b""):
            out.write(line)
            out.flush()
            log_f.write(line)
    finally:
        proc.wait()
        log_f.close()
        # Если hardlink не получился — обновим latest.log копией.
        try:
            if not latest.exists() or latest.stat().st_ino != log_path.stat().st_ino:
                latest.write_bytes(log_path.read_bytes())
        except Exception:
            pass
        print(f"\n==> Остановлено. Полный лог: {log_path}", flush=True)
        return proc.returncode or 0


if __name__ == "__main__":
    sys.exit(main())
