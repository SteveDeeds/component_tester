"""
Design Verification Test (DVT) for ComponentTester.

Assumption:
- All probes are tied together at one node.

Goal:
- Exercise every currently exposed SCPI command and verify intended behavior.
"""

from __future__ import annotations

import csv
import sys
import time
from dataclasses import dataclass

import serial
import serial.tools.list_ports

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
PORT = None
BAUD = 115200
TIMEOUT_S = 5.0
SETTLE_S = 0.12
CSV_LOG = "dvt_log.csv"

MEAS_ALL_COLUMNS = ["V1_V", "V1_I_A", "V2_V", "V2_I_A", "GND_I_A", "VS_V", "VS_I_A"]
WAVE_COLUMNS_ALLOWED = {
    "V1_V", "V1_I_A", "V2_V", "V2_I_A", "GND_I_A", "VS_V", "VS_I_A"
}
ADC_PRESCALERS = [2, 4, 8, 16, 32, 64, 128]
SHUNT_COMMANDS = ["V1", "V2", "GND", "VS"]


@dataclass
class TestResult:
    name: str
    passed: bool
    details: str


def find_port() -> str:
    ports = serial.tools.list_ports.comports()
    preferred = [
        p.device for p in ports
        if "USB" in (p.description or "").upper()
        or "CH340" in (p.description or "").upper()
        or "ARDUINO" in (p.description or "").upper()
    ]
    if preferred:
        return preferred[0]
    if ports:
        return ports[0].device
    raise RuntimeError("No serial ports found.")


def send(ser: serial.Serial, cmd: str) -> None:
    ser.write((cmd + "\n").encode())


def read_line(ser: serial.Serial) -> str:
    return ser.readline().decode(errors="replace").strip()


def query(ser: serial.Serial, cmd: str, *, retries: int = 3, retry_delay_s: float = 0.2) -> str:
    last_line = ""
    for attempt in range(retries):
        send(ser, cmd)
        line = read_line(ser)
        last_line = line
        if line.startswith("ERROR"):
            raise RuntimeError(f"Device returned error for {cmd!r}: {line}")
        if line:
            return line
        if attempt < retries - 1:
            time.sleep(retry_delay_s)
    raise RuntimeError(f"No response for query {cmd!r}")


def query_float(ser: serial.Serial, cmd: str) -> float:
    return float(query(ser, cmd))


def approx_equal(a: float, b: float, tol: float) -> bool:
    return abs(a - b) <= tol


def parse_csv_floats(line: str, expected_len: int) -> list[float]:
    parts = [p.strip() for p in line.split(",")]
    if len(parts) != expected_len:
        raise ValueError(f"Expected {expected_len} CSV fields, got {len(parts)} from: {line!r}")
    return [float(x) for x in parts]


def run_test(name: str, func) -> TestResult:
    try:
        details = func()
        return TestResult(name=name, passed=True, details=details)
    except Exception as exc:
        return TestResult(name=name, passed=False, details=str(exc))


def wait_for_ready(ser: serial.Serial, *, timeout_s: float = 8.0) -> str:
    """Wait for firmware to boot and respond to *IDN?."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            line = query(ser, "*IDN?", retries=1)
            if line:
                return line
        except Exception:
            pass
        time.sleep(0.2)
    raise RuntimeError("Instrument did not respond to *IDN? after connect")


# ---------------------------------------------------------------------------
# Individual command tests
# ---------------------------------------------------------------------------
def make_tests(ser: serial.Serial):
    def clear_error_queue() -> None:
        for _ in range(8):
            line = query(ser, "SYST:ERR?")
            if line == '0,"No error"':
                return
        raise AssertionError("Error queue did not drain")

    def t_idn() -> str:
        line = query(ser, "*IDN?")
        if "ComponentTester" not in line:
            raise AssertionError(f"Unexpected IDN: {line}")
        return line

    def t_rst() -> str:
        send(ser, "SOUR1:VOLT 3.0")
        send(ser, "SOUR2:VOLT 2.0")
        send(ser, "SENS:ADC:PRES 16")
        send(ser, "WAV:SIGN V1V,V2V")
        send(ser, "WAV:POIN 10")
        send(ser, "*RST")
        time.sleep(0.05)

        v1 = query_float(ser, "SOUR1:VOLT?")
        v2 = query_float(ser, "SOUR2:VOLT?")
        pres = int(query(ser, "SENS:ADC:PRES?"))
        avg_count = int(query(ser, "SENS:AVER:COUN?"))
        signs = query(ser, "WAV:SIGN?")
        pnts = int(query(ser, "WAV:POIN?"))
        pmax = int(query(ser, "WAV:POIN:MAX?"))
        shunts = {
            key: query_float(ser, f"CAL:SHUN:{key}?")
            for key in SHUNT_COMMANDS
        }

        if not approx_equal(v1, 0.0, 0.02):
            raise AssertionError(f"SOUR1 reset mismatch: {v1}")
        if not approx_equal(v2, 0.0, 0.02):
            raise AssertionError(f"SOUR2 reset mismatch: {v2}")
        if pres != 128:
            raise AssertionError(f"ADC prescaler reset mismatch: {pres}")
        if avg_count != 1:
            raise AssertionError(f"Averages reset mismatch: {avg_count}")
        if signs != "V1_V":
            raise AssertionError(f"WAV:SIGN reset mismatch: {signs}")
        if not (1 <= pnts <= pmax):
            raise AssertionError(f"WAV:POIN reset mismatch: {pnts}")
        if not all(v > 0 for v in shunts.values()):
            raise AssertionError(f"Shunt reset mismatch: {shunts}")

        return "reset defaults verified"

    def t_source_limits() -> str:
        s1_min = query_float(ser, "SOUR1:VOLT:MIN?")
        s1_max = query_float(ser, "SOUR1:VOLT:MAX?")
        s2_min = query_float(ser, "SOUR2:VOLT:MIN?")
        s2_max = query_float(ser, "SOUR2:VOLT:MAX?")

        if not (s1_min <= s1_max):
            raise AssertionError(f"Invalid SOUR1 limits: {s1_min}, {s1_max}")
        if not (s2_min <= s2_max):
            raise AssertionError(f"Invalid SOUR2 limits: {s2_min}, {s2_max}")
        return f"S1=[{s1_min:.4f},{s1_max:.4f}] S2=[{s2_min:.4f},{s2_max:.4f}]"

    def t_source_v1() -> str:
        send(ser, "SOUR1:VOLT 1.25")
        got = query_float(ser, "SOUR1:VOLT?")
        if not approx_equal(got, 1.25, 0.02):
            raise AssertionError(f"SOUR1:VOLT? expected ~1.25, got {got}")
        return f"set/query ok ({got:.4f} V)"

    def t_source_v2() -> str:
        send(ser, "SOUR2:VOLT 3.75")
        got = query_float(ser, "SOUR2:VOLT?")
        if not approx_equal(got, 3.75, 0.03):
            raise AssertionError(f"SOUR2:VOLT? expected ~3.75, got {got}")
        return f"set/query ok ({got:.4f} V)"

    def t_sens_adc_pres() -> str:
        for pres in ADC_PRESCALERS:
            send(ser, f"SENS:ADC:PRES {pres}")
            got = int(query(ser, "SENS:ADC:PRES?"))
            if got != pres:
                raise AssertionError(f"Requested {pres}, got {got}")
        send(ser, "SENS:ADC:PRES 128")
        return "all valid prescalers accepted"

    def t_sens_avg_count() -> str:
        for n in (1, 7, 32, 255):
            send(ser, f"SENS:AVER:COUN {n}")
            got = int(query(ser, "SENS:AVER:COUN?"))
            if got != n:
                raise AssertionError(f"Requested averages {n}, got {got}")
        send(ser, "SENS:AVER:COUN 1")
        return "set/query verified"

    def t_cal_shunts() -> str:
        originals = {k: query_float(ser, f"CAL:SHUN:{k}?") for k in SHUNT_COMMANDS}
        targets = {
            "V1": originals["V1"] * 1.01,
            "V2": originals["V2"] * 1.01,
            "GND": originals["GND"] * 1.01,
            "VS": originals["VS"] * 1.01,
        }
        try:
            for key, val in targets.items():
                send(ser, f"CAL:SHUN:{key} {val:.6f}")
                got = query_float(ser, f"CAL:SHUN:{key}?")
                if not approx_equal(got, val, max(1e-4, abs(val) * 0.002)):
                    raise AssertionError(f"CAL:SHUN:{key} mismatch set={val}, got={got}")
        finally:
            for key, val in originals.items():
                send(ser, f"CAL:SHUN:{key} {val:.6f}")

        return "set/query/restore verified"

    def t_meas_scalars() -> str:
        send(ser, "SOUR1:VOLT 1.0")
        send(ser, "SOUR2:VOLT 1.0")
        time.sleep(SETTLE_S)

        vals = {
            "MEAS:VOLT1?": query_float(ser, "MEAS:VOLT1?"),
            "MEAS:VOLT2?": query_float(ser, "MEAS:VOLT2?"),
            "MEAS:CURR1?": query_float(ser, "MEAS:CURR1?"),
            "MEAS:CURR2?": query_float(ser, "MEAS:CURR2?"),
            "MEAS:CURR:GND?": query_float(ser, "MEAS:CURR:GND?"),
            "MEAS:VS?": query_float(ser, "MEAS:VS?"),
        }

        # Sanity bounds only; this is command verification, not calibration.
        for cmd, v in vals.items():
            if not (-1.0 <= v <= 6.0):
                raise AssertionError(f"{cmd} out of sanity bounds: {v}")

        return "all scalar MEAS commands responded with numeric values"

    def t_meas_vs_alias() -> str:
        vs = query_float(ser, "MEAS:VS?")
        aux1 = query_float(ser, "MEAS:AUX1?")
        if not approx_equal(vs, aux1, 0.02):
            raise AssertionError(f"MEAS:VS? and MEAS:AUX1? mismatch: {vs} vs {aux1}")
        return f"alias verified ({vs:.4f} vs {aux1:.4f})"

    def t_meas_all() -> str:
        line = query(ser, "MEAS:ALL?")
        vals = parse_csv_floats(line, len(MEAS_ALL_COLUMNS))
        return f"{len(vals)} fields"

    def t_wav_sign() -> str:
        send(ser, "WAV:SIGN V1V,V2I,GNDI,VSV")
        resp = query(ser, "WAV:SIGN?")
        got = [x.strip() for x in resp.split(",") if x.strip()]
        if set(got) != {"V1_V", "V2_I_A", "GND_I_A", "VS_V"}:
            raise AssertionError(f"Unexpected WAV:SIGN? response: {resp}")

        send(ser, "WAV:SIGN ALL")
        resp_all = query(ser, "WAV:SIGN?")
        got_all = [x.strip() for x in resp_all.split(",") if x.strip()]
        if not got_all:
            raise AssertionError("WAV:SIGN ALL resulted in empty selection")
        if not set(got_all).issubset(WAVE_COLUMNS_ALLOWED):
            raise AssertionError(f"Unexpected columns in WAV:SIGN?: {resp_all}")

        return "set/query/ALL verified"

    def t_wav_points() -> str:
        send(ser, "WAV:SIGN V1V,V2V,GNDI")
        pmax = int(query(ser, "WAV:POIN:MAX?"))
        if pmax < 1:
            raise AssertionError(f"Invalid max points: {pmax}")

        send(ser, "WAV:POIN MAX")
        got_max = int(query(ser, "WAV:POIN?"))
        if got_max != pmax:
            raise AssertionError(f"WAV:POIN MAX mismatch: expected {pmax}, got {got_max}")

        target = min(10, pmax)
        send(ser, f"WAV:POIN {target}")
        got = int(query(ser, "WAV:POIN?"))
        if got != target:
            raise AssertionError(f"WAV:POIN set mismatch: expected {target}, got {got}")

        return f"MAX={pmax}, set/query ok"

    def capture_wave_block(cmd: str) -> tuple[str, str, list[str]]:
        send(ser, cmd)
        meta = read_line(ser)
        header = read_line(ser)
        rows = []
        while True:
            line = read_line(ser)
            if not line:
                raise RuntimeError("Timed out while reading waveform block")
            if line == "WAV,END":
                break
            rows.append(line)
        return meta, header, rows

    def t_wav_data_and_meas_wav_alias() -> str:
        send(ser, "WAV:SIGN V1V,V1I,V2V")
        send(ser, "WAV:POIN 8")
        time.sleep(SETTLE_S)

        meta1, hdr1, rows1 = capture_wave_block("WAV:DATA?")
        meta2, hdr2, rows2 = capture_wave_block("MEAS:WAV?")

        if not meta1.startswith("WAV,POINTS,"):
            raise AssertionError(f"Unexpected WAV:DATA? metadata: {meta1}")
        if not meta2.startswith("WAV,POINTS,"):
            raise AssertionError(f"Unexpected MEAS:WAV? metadata: {meta2}")
        if hdr1 != hdr2:
            raise AssertionError("WAV:DATA? and MEAS:WAV? header mismatch")
        if len(rows1) != len(rows2):
            raise AssertionError("WAV:DATA? and MEAS:WAV? row count mismatch")

        headers = [h.strip() for h in hdr1.split(",")]
        if headers[0] != "INDEX":
            raise AssertionError(f"Wave header missing INDEX: {hdr1}")

        for col in headers[1:]:
            if col not in WAVE_COLUMNS_ALLOWED:
                raise AssertionError(f"Unexpected wave column: {col}")

        for line in rows1:
            parts = [p.strip() for p in line.split(",")]
            if len(parts) != len(headers):
                raise AssertionError(f"Wave row column mismatch: {line}")
            int(parts[0])
            for p in parts[1:]:
                float(p)

        return f"header {hdr1} with {len(rows1)} rows; alias behavior verified"

    def t_error_queue_behaviour() -> str:
        clear_error_queue()

        send(ser, "SOUR1:VOLT:MIN 0")  # read-only command used as set
        e1 = query(ser, "SYST:ERR?")
        if "SOUR1:VOLT:MIN is read-only" not in e1:
            raise AssertionError(f"Expected read-only error, got: {e1}")

        send(ser, "NOPE:COMMAND")
        e2 = query(ser, "SYST:ERR?")
        if "Unrecognised command" not in e2:
            raise AssertionError(f"Expected unrecognised command error, got: {e2}")

        e3 = query(ser, "SYST:ERR?")
        if e3 != '0,"No error"':
            raise AssertionError(f"Expected empty queue, got: {e3}")

        return "enqueue/pop/empty verified"

    return [
        ("*IDN?", t_idn),
        ("*RST", t_rst),
        ("SOURx:VOLT:MIN?/MAX?", t_source_limits),
        ("SOUR1:VOLT + SOUR1:VOLT?", t_source_v1),
        ("SOUR2:VOLT + SOUR2:VOLT?", t_source_v2),
        ("SENS:ADC:PRES + query", t_sens_adc_pres),
        ("SENS:AVER:COUN + query", t_sens_avg_count),
        ("CAL:SHUN:* set/query", t_cal_shunts),
        ("MEAS scalar queries", t_meas_scalars),
        ("MEAS:VS? alias MEAS:AUX1?", t_meas_vs_alias),
        ("MEAS:ALL?", t_meas_all),
        ("WAV:SIGN/WAV:SIGN?", t_wav_sign),
        ("WAV:POIN/WAV:POIN?/WAV:POIN:MAX?", t_wav_points),
        ("WAV:DATA? + MEAS:WAV?", t_wav_data_and_meas_wav_alias),
        ("SYST:ERR? queue behavior", t_error_queue_behaviour),
    ]


def write_log(path: str, results: list[TestResult]) -> None:
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["test", "pass", "details"])
        for r in results:
            w.writerow([r.name, "PASS" if r.passed else "FAIL", r.details])


def main() -> None:
    port = PORT or find_port()
    print(f"Connecting to {port} @ {BAUD}...")

    results: list[TestResult] = []

    with serial.Serial(port, BAUD, timeout=TIMEOUT_S) as ser:
        # Uno-class boards auto-reset when serial opens.
        time.sleep(2.0)
        ser.reset_input_buffer()

        idn = wait_for_ready(ser)
        print(f"Instrument ready: {idn}")

        tests = make_tests(ser)

        for name, fn in tests:
            result = run_test(name, fn)
            results.append(result)
            status = "PASS" if result.passed else "FAIL"
            print(f"[{status}] {name}: {result.details}")

        send(ser, "SOUR1:VOLT 0")
        send(ser, "SOUR2:VOLT 0")

    write_log(CSV_LOG, results)
    print(f"\nSaved DVT log to {CSV_LOG}")

    total = len(results)
    failed = sum(1 for r in results if not r.passed)
    passed = total - failed

    print(f"Summary: {passed}/{total} passed, {failed} failed")
    if failed:
        sys.exit(2)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nAborted by user.")
        sys.exit(130)
    except Exception as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        sys.exit(1)
