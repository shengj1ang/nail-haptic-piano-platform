import sys
import time
import serial


BAUD = 115200
READ_TIMEOUT = 0.3

# 参数（你可以调）
AMP = 80
ON_MS = 120
OFF_MS = 120
WAIT_BETWEEN_GROUPS_S = 2.0

# 频率参数（Hz）
FREQ1, FREQ2, FREQ3 = 300, 240, 240


def open_serial(port: str) -> serial.Serial:
    ser = serial.Serial(port, BAUD, timeout=READ_TIMEOUT)
    # 打开串口后很多板子会reset，等它启动并输出 boot
    time.sleep(1.5)
    ser.reset_input_buffer()
    return ser


def send_line(ser: serial.Serial, line: str) -> None:
    ser.write((line.strip() + "\n").encode("utf-8"))
    ser.flush()


def read_reply(ser: serial.Serial, timeout_s: float = 2.0) -> str:
    end = time.time() + timeout_s
    while time.time() < end:
        resp = ser.readline().decode("utf-8", errors="replace").strip()
        if not resp:
            continue
        if resp.startswith("OK") or resp.startswith("ERR"):
            return resp
    raise TimeoutError("No OK/ERR reply from Arduino")


def cmd_expect_ok(ser: serial.Serial, line: str) -> str:
    send_line(ser, line)
    resp = read_reply(ser)
    if resp.startswith("ERR"):
        raise RuntimeError(f"Arduino error for '{line}': {resp}")
    return resp


def motor_mask(motor_index_1based: int) -> int:
    return 1 << (motor_index_1based - 1)  # 1->1, 2->2, 3->4


def vibrate_once(ser: serial.Serial, mask: int, amp: int, on_ms: int) -> None:
    cmd_expect_ok(ser, f"S {mask} {amp}")
    time.sleep(on_ms / 1000.0)
    cmd_expect_ok(ser, "X")


def vibrate_n(ser: serial.Serial, mask: int, amp: int, n: int, on_ms: int, off_ms: int) -> None:
    for i in range(n):
        vibrate_once(ser, mask, amp, on_ms)
        if i != n - 1:
            time.sleep(off_ms / 1000.0)


def main():
    if len(sys.argv) < 2:
        print("Usage: python play_pattern.py <serial_port>")
        sys.exit(1)

    port = sys.argv[1]
    ser = open_serial(port)

    try:
        # 保险：先停一下
        try:
            cmd_expect_ok(ser, "X")
        except Exception:
            pass

        # 1) set frequency（会得到 OK freq 或 OK freq_ignored）
        resp = cmd_expect_ok(ser, f"F {FREQ1} {FREQ2} {FREQ3}")
        print("Set frequency:", resp)

        # 2) 按你要求的序列
        print("Motor 1: single")
        vibrate_n(ser, motor_mask(1), AMP, n=1, on_ms=ON_MS, off_ms=OFF_MS)
        vibrate_n(ser, motor_mask(2), AMP, n=2, on_ms=ON_MS, off_ms=OFF_MS)
        vibrate_n(ser, motor_mask(3), AMP, n=3, on_ms=ON_MS, off_ms=OFF_MS)
        
        '''
        time.sleep(WAIT_BETWEEN_GROUPS_S)

        print("Motor 2: double")
        vibrate_n(ser, motor_mask(2), AMP, n=2, on_ms=ON_MS, off_ms=OFF_MS)

        time.sleep(WAIT_BETWEEN_GROUPS_S)

        print("Motor 3: triple")
        vibrate_n(ser, motor_mask(3), AMP, n=3, on_ms=ON_MS, off_ms=OFF_MS)
    '''
        cmd_expect_ok(ser, "X")
        print("Done.")

    finally:
        ser.close()


if __name__ == "__main__":
    main()