"""实时发射队列：按编码帧裁掉旧积压，保留最近的话音。"""


def enqueue(output, pcm, limit=0.240):
    """原子完成限长和入队；返回裁掉的旧音频秒数。"""
    frame_size = getattr(output, 'encoder_framesize', None)
    if not isinstance(frame_size, (int, float)) or frame_size <= 0:
        output.add_sound(pcm)
        return 0.0
    channels = output.channels
    frame_bytes = int(frame_size * 48000 * 2 * channels)
    budget = max(frame_bytes, int(limit * 48000 * 2 * channels))
    dropped = 0
    with output.lock:
        queue = output.pcm
        if queue and len(queue[-1]) < frame_bytes:
            count = min(frame_bytes - len(queue[-1]), len(pcm))
            queue[-1] += pcm[:count]
            pcm = pcm[count:]
        queue.extend(pcm[i:i + frame_bytes] for i in range(0, len(pcm), frame_bytes))
        total = sum(map(len, queue))
        count = 0
        while count < len(queue) - 1 and total > budget:
            total -= len(queue[count])
            dropped += len(queue[count])
            count += 1
        if count:
            del queue[:count]
    return dropped / (48000 * 2 * channels)
