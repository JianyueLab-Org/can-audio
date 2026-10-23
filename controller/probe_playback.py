"""本机静音压力验证，不连接网络、不打开麦克风。"""
import time
import threading
import numpy as np
import pyaudio
from test_voice import make_client
import rxmix

client = make_client()
audio = pyaudio.PyAudio()
count = [0]
status_codes = []
sizes = []
done = threading.Event()

def callback(data, frames, info, status):
    if status:
        status_codes.append(status)
    sizes.append(frames)
    result = client._playback_callback(data, frames, info, status)
    count[0] += 1
    if count[0] >= 1500:
        done.set()
    return result

stream = audio.open(format=pyaudio.paInt16, channels=1, rate=48000,
                    output=True, frames_per_buffer=960, stream_callback=callback)
start = time.perf_counter()
while not done.is_set() and time.perf_counter() - start < 35:
    # 两路静音接收，模拟网络突发和空档。
    with client._rx_mix_wakeup:
        for source in (1, 2):
            rxmix.enqueue(client._rx_mix_frames, source, np.zeros(2880, np.int16))
    time.sleep(.060)
stream.stop_stream()
stream.close()
audio.terminate()
print('callbacks=%d status=%r elapsed=%.3f frame_sizes=%r' %
      (count[0], status_codes, time.perf_counter()-start, sorted(set(sizes))))
assert count[0] >= 1500
assert not status_codes
