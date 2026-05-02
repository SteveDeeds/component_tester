"""Shared serial instrument helpers for SCPI-based GUIs."""

from __future__ import annotations

import time
from typing import Callable

try:
    import serial
    import serial.tools.list_ports
except Exception:
    serial = None  # type: ignore[assignment]


Logger = Callable[[str], None]


def serial_is_available() -> bool:
    return serial is not None


def list_serial_devices() -> list[tuple[str, str]]:
    """Return available serial ports as (port, description)."""
    if not serial_is_available():
        return []
    try:
        ports = serial.tools.list_ports.comports()
        return [(p.device, p.description or "Unknown") for p in ports]
    except Exception:
        return []


class InstrumentSession:
    """Thin wrapper around pyserial with SCPI-friendly helpers."""

    def __init__(
        self,
        *,
        logger: Logger | None = None,
        baud: int = 115200,
        timeout_s: float = 5.0,
    ) -> None:
        self._log = logger
        self._baud = int(baud)
        self._timeout_s = float(timeout_s)
        self._conn = None

    @property
    def is_connected(self) -> bool:
        return self._conn is not None

    def open(self, port: str) -> None:
        if not serial_is_available():
            raise RuntimeError("pyserial not available")
        if self._conn is not None:
            return
        self._conn = serial.Serial(port, self._baud, timeout=self._timeout_s)

    def close(self) -> None:
        if self._conn is None:
            return
        try:
            self._conn.close()
        except Exception:
            pass
        self._conn = None

    def drain_rx(self) -> None:
        if self._conn is None:
            return
        try:
            waiting = self._conn.in_waiting
            if waiting:
                stale = self._conn.read(waiting)
                self._emit(f"RX DRAINED {waiting}B | {stale!r}")
        except Exception:
            pass

    def send_scpi(
        self,
        cmd: str,
        *,
        expect_response: bool = False,
        timeout_s: float | None = None,
    ) -> str:
        """Send SCPI command and optionally return one line response."""
        self._emit(f"TX {cmd}")
        if self._conn is None:
            return ""

        self.drain_rx()

        try:
            self._conn.write((cmd + "\n").encode("ascii", errors="ignore"))
            self._conn.flush()
            if not expect_response:
                return ""

            deadline = time.monotonic() + (timeout_s if timeout_s is not None else self._timeout_s)
            buf = bytearray()
            while time.monotonic() < deadline:
                waiting = self._conn.in_waiting
                if waiting:
                    chunk = self._conn.read(waiting)
                    buf += chunk
                    if b"\n" in buf:
                        break
                else:
                    time.sleep(0.005)

            line = buf.decode("ascii", errors="replace").strip()
            self._emit(f"RX {line}" if line else "RX <timeout>")
            return line
        except Exception as exc:
            self._emit(f"ERROR | Serial TX/RX failed: {exc}")
            self.close()
            return ""

    def _emit(self, text: str) -> None:
        if self._log is not None:
            self._log(text)
