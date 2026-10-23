"""实时语音状态：复用现有引擎的传输和 Ping 统计，不主动探测服务器。"""
def snapshot(client):
    connected = bool(client and client.connected)
    transport, latency = client.transport_status() if connected else (None, None)
    if connected:
        stats = getattr(client.mumble, 'udp_ping_stats' if transport == 'UDP' else 'ping_stats', {})
        if not stats.get('nb', 0):
            latency = None
    return dict(connected=connected, running=bool(client and client.running),
                transport=transport, latency_ms=latency)
