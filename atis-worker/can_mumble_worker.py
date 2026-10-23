"""Small CAN ATIS Mumble sender used by the desktop client.

The protocol and channel lifecycle intentionally follow atis/broadcast.py
(the old Python desktop ATIS client, still on can-audio's main branch):
connect with pymumble, create FREQ_xxxxxx as a temporary root channel when
needed, wait for the server echo, move in, then enqueue 20 ms PCM frames.
"""
import argparse
import os
import sys
import time
import json
import threading
import math

base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
python_dir = os.path.dirname(sys.executable)
sys.path.insert(0, base)
os.environ["PATH"] = (base + os.pathsep + python_dir + os.pathsep +
                       os.environ.get("PATH", ""))

import mumblecompat
mumblecompat.install()
import pymumble_py3 as pymumble
from pymumble_py3 import messages
from pymumble_py3.constants import PYMUMBLE_CLBK_CONNECTED


def frequency_khz(value):
    """--frequency -> kHz. Accepts Hz (127800000, what can-atis sends) or MHz (127.8)."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f"not a frequency: {value!r}")
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError(f"not a frequency: {value!r}")
    if number >= 1_000_000:
        khz = number / 1000
    elif number < 1000:
        khz = number * 1000
    else:
        # 1000-999999 could be kHz or a typo; refuse rather than guess a channel
        raise argparse.ArgumentTypeError(
            f"frequency must be in Hz or MHz: {value!r}")
    khz = int(round(khz))
    if not 1 <= khz <= 999_999:
        raise argparse.ArgumentTypeError(f"frequency out of range: {value!r}")
    return khz


def transport_status(mumble):
    """Reused from CAN Python voice.transport_status (MSFS/controller clients)."""
    udp = getattr(mumble, "udp_active", False)
    stats = getattr(mumble, "udp_ping_stats" if udp else "ping_stats", {})
    rtt = stats.get("avg") if stats.get("nb", 0) > 0 else None
    if rtt is not None and (not math.isfinite(rtt) or rtt < 0):
        rtt = None
    return "UDP" if udp else "TCP", rtt


def report(state, transport=None, latency=None, message=None):
    print(json.dumps(dict(state=state, transport=transport, latency=latency,
                          message=message)), flush=True)


def find_channel(mumble, name):
    try:
        return mumble.channels.find_by_name(name)
    except Exception:
        return None


def main():
    if "--self-test" in sys.argv:
        class Fake:
            udp_active = True
            udp_ping_stats = {"nb": 1, "avg": 23.5}
        assert transport_status(Fake()) == ("UDP", 23.5)
        report("ready")
        return 0
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--cid", required=True)
    parser.add_argument("--callsign", required=True)
    parser.add_argument("--frequency", required=True, type=frequency_khz,
                        help="Hz (127800000) or MHz (127.8)")
    args = parser.parse_args()

    khz = args.frequency
    channel_name = f"FREQ_{khz:06d}"
    user = f"{args.cid}_atis{khz:06d}_{args.callsign}"
    pcm = sys.stdin.buffer.read()
    if not pcm:
        return 2

    connected = False
    report("connecting")
    mumble = pymumble.Mumble(args.host, user, password=os.environ.pop("CAN_VOICE_PASSWORD", ""),
                             port=args.port, reconnect=False)
    mumble.set_receive_sound(True)

    def on_connected():
        nonlocal connected
        connected = True

    mumble.callbacks.set_callback(PYMUMBLE_CLBK_CONNECTED, on_connected)
    mumble.start()
    deadline = time.time() + 15
    while not connected and time.time() < deadline:
        time.sleep(0.05)
    if not connected:
        report("error", message="语音连接失败或超时")
        mumble.stop()
        return 3

    channel = find_channel(mumble, channel_name)
    if channel is None:
        mumble.execute_command(messages.CreateChannel(0, channel_name, True),
                               blocking=False)
        deadline = time.time() + 5
        while channel is None and time.time() < deadline:
            channel = find_channel(mumble, channel_name)
            time.sleep(0.05)
    if channel is None:
        report("error", message="无法创建语音频道")
        mumble.stop()
        return 4

    channel_id = channel["channel_id"]
    if mumble.users.myself["channel_id"] != channel_id:
        mumble.execute_command(
            messages.MoveCmd(mumble.users.myself_session, channel_id),
            blocking=False)
        deadline = time.time() + 5
        while mumble.users.myself["channel_id"] != channel_id and time.time() < deadline:
            time.sleep(0.05)
    if mumble.users.myself["channel_id"] != channel_id:
        report("error", message="无法加入语音频道")
        mumble.stop()
        return 5

    def monitor():
        while mumble.is_alive():
            transport, rtt = transport_status(mumble)
            report("connected", transport, rtt)
            time.sleep(1)
        report("disconnected")

    threading.Thread(target=monitor, daemon=True).start()
    frame_bytes = 960 * 2
    while mumble.is_alive():
        for offset in range(0, len(pcm), frame_bytes):
            if not mumble.is_alive():
                report("disconnected")
                return 6
            while mumble.sound_output.get_buffer_size() > 0.2 and mumble.is_alive():
                time.sleep(0.02)
            frame = pcm[offset:offset + frame_bytes]
            frame += b"\x00" * (frame_bytes - len(frame))
            mumble.sound_output.add_sound(frame)
            time.sleep(0.02)
    report("disconnected")
    return 6


if __name__ == "__main__":
    raise SystemExit(main())
