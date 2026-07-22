import sys
import time
import serial


BAUD = 115200
ACK_TIMEOUT = 2.0  # seconds


def send_cmd_wait_ack(ser: serial.Serial, cmd: str, cmd_id: int) -> str:
    """
    Send a single line command and wait for ACK/ERR that contains id=cmd_id.
    Returns the full response line.
    """
    line = f"{cmd} id={cmd_id}\n"
    ser.write(line.encode("utf-8"))
    ser.flush()

    deadline = time.time() + ACK_TIMEOUT
    while time.time() < deadline:
        resp = ser.readline().decode("utf-8", errors="replace").strip()
        if not resp:
            continue
        # match id
        if f"id={cmd_id}" in resp and (resp.startswith("ACK") or resp.startswith("ERR")):
            return resp

    raise TimeoutError(f"Timeout waiting for ACK/ERR for id={cmd_id}")


def main():
    if len(sys.argv) < 2:
        print("Usage: python vib_seq.py <serial_port> [cycles]")
        print("Example: python vib_seq.py COM5 3")
        print("Example: python vib_seq.py /dev/ttyACM0 1")
        sys.exit(1)

    port = sys.argv[1]
    cycles = int(sys.argv[2]) if len(sys.argv) >= 3 else 1

    ser = serial.Serial(port, BAUD, timeout=0.2)
    time.sleep(1.5)  # give board time to reset after opening serial
    ser.reset_input_buffer()

    cmd_id = 1
    try:
        # optional: stop any running pattern
        try:
            resp = send_cmd_wait_ack(ser, "STOP", cmd_id)
            print(resp)
        except Exception:
            pass
        cmd_id += 1

        for c in range(cycles):
            print(f"=== Cycle {c+1}/{cycles} ===")
            for m in (1, 2, 3):
                # one pulse: n=1, on=100ms, off=0, gap=200ms
                cmd = f"VIB m={m} amp=80 n=1 on=100 off=0 gap=2000 mode=OVR"
                resp = send_cmd_wait_ack(ser, cmd, cmd_id)
                print(f"motor {m}: {resp}")
                if resp.startswith("ERR"):
                    return
                cmd_id += 1

                # small extra spacing between motors (optional)
                time.sleep(0.05)

        # stop at end
        resp = send_cmd_wait_ack(ser, "STOP", cmd_id)
        print(resp)

    finally:
        ser.close()


if __name__ == "__main__":
    main()