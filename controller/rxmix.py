"""多路接收混音：按采样数消费独立队列，不把网络包当作播放帧。"""

from collections import deque

import numpy as np


def enqueue(queues, source, samples):
    """调用方持锁；每路最多保留 240 ms，避免网络突发积累成长延迟。"""
    if not len(samples):
        return
    queue = queues.setdefault(source, deque())
    queue.append(samples)
    excess = sum(map(len, queue)) - 11520  # Mumble 解码固定为 48 kHz。
    while excess > 0:
        head = queue.popleft()
        if len(head) > excess:
            queue.appendleft(head[excess:])
        excess -= len(head)


def mix(frames):
    if not frames:
        return None
    mixed = np.zeros(max(map(len, frames)), dtype=np.int32)
    for frame in frames:
        mixed[:len(frame)] += frame
    return np.clip(mixed, -32768, 32767).astype(np.int16)


def take(queues, size=960):
    """各路取同样长的时间片；长包留尾，短包拼接，不足部分补静音。"""
    frames = []
    for source, queue in list(queues.items()):
        if queue:
            frame = np.zeros(size, dtype=np.int16)
            offset = 0
            while queue and offset < size:
                head = queue.popleft()
                count = min(len(head), size - offset)
                frame[offset:offset + count] = head[:count]
                offset += count
                if count < len(head):
                    queue.appendleft(head[count:])
            frames.append(frame)
        if not queue:
            del queues[source]
    return mix(frames)


def output_frame(samples, size):
    """声卡只能用较低采样率时，保持 20 ms 播放时长和音调。"""
    if len(samples) == size:
        return samples
    return np.interp(np.arange(size) * len(samples) / size,
                     np.arange(len(samples)), samples).astype(np.int16)
