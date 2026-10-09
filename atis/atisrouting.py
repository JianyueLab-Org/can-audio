"""Fail-closed geographic ATIS whispers; no coordinates are persisted or logged."""

import json
import logging
import math
import re
import threading
import time
import types
import urllib.request
from dataclasses import dataclass


DEFAULT_URL = "https://data.ceruleanavi.net/v1/data.json"
DEFAULT_RANGE_NM = 100.0
POLL_INTERVAL = 5.0
FEED_MAX_AGE = 20.0
POSITION_MAX_AGE = 90.0
FUTURE_SKEW = 5.0
FETCH_TIMEOUT = 5.0
MAX_FEED_BYTES = 4 * 1024 * 1024
TARGET_ID = 2
_ATIS_NAME = re.compile(r"^[0-9]+_atis([0-9]{6})(?:_([A-Z0-9]{4}_(?:[DA]_)?ATIS))?$")
log = logging.getLogger("atisrouting")


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError, OverflowError):
        return None


def valid_radius(value):
    radius = _number(value)
    return radius if radius is not None and radius > 0 else None


def is_atis_sender(name):
    return isinstance(name, str) and _ATIS_NAME.fullmatch(name) is not None


def _cid(value):
    token = str(value).strip()
    try:
        return str(int(token)) if re.fullmatch(r"[0-9]+", token) else ""
    except ValueError:
        return ""


def _fresh(timestamp, now, max_age):
    return timestamp is not None and -FUTURE_SKEW <= now - timestamp <= max_age


@dataclass(frozen=True)
class Position:
    cid: str
    callsign: str
    frequency: int | None
    latitude: float | None
    longitude: float | None
    timestamp: float | None

    def fresh(self, now):
        return (self.latitude is not None and -90 <= self.latitude <= 90
                and self.longitude is not None and -180 <= self.longitude <= 180
                and _fresh(self.timestamp, now, POSITION_MAX_AGE))


class FeedSnapshot:
    """Minimal current routing fields, retaining duplicates for ambiguity checks."""

    def __init__(self, data):
        if not isinstance(data, dict) or not isinstance(data.get("general"), dict):
            raise ValueError("invalid feed shape")
        self.timestamp = _number(data["general"].get("update_timestamp"))
        self.receivers = {}
        self.stations = {}
        for section in ("pilots", "controllers", "atis"):
            entries = data.get(section)
            if not isinstance(entries, list) or any(not isinstance(e, dict) for e in entries):
                raise ValueError("invalid feed entries")
            for entry in entries:
                frequency = _number(entry.get("frequency"))
                khz = round(frequency * 1000) if frequency is not None else None
                if khz is not None and not 118000 <= khz <= 136975:
                    khz = None
                callsign = str(entry.get("callsign") or "").strip().upper()
                position = Position(_cid(entry.get("cid")), callsign, khz,
                                    _number(entry.get("latitude")),
                                    _number(entry.get("longitude")),
                                    _number(entry.get("position_time" if section == "pilots" else "send_time")))
                table, key = ((self.stations, callsign) if section == "atis"
                              else (self.receivers, position.cid))
                table.setdefault(key, []).append(position)

    def fresh(self, now):
        return _fresh(self.timestamp, now, FEED_MAX_AGE)

    def station(self, callsign, frequency, now):
        positions = self.stations.get(callsign, ())
        if len(positions) != 1:
            return None
        position = positions[0]
        return position if position.frequency == frequency and position.fresh(now) else None

    def receiver(self, name, now):
        if not isinstance(name, str):
            return None
        if re.fullmatch(r"[0-9]+", name):
            positions = self.receivers.get(_cid(name), ())
            return positions[0] if len(positions) == 1 and positions[0].fresh(now) else None
        match = _ATIS_NAME.fullmatch(name)
        return self.station(match[2], int(match[1]), now) if match and match[2] else None


def distance_nm(first, second):
    lat1, lat2 = math.radians(first.latitude), math.radians(second.latitude)
    dlat = lat2 - lat1
    dlon = math.radians(second.longitude - first.longitude)
    haversine = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 3440.065 * 2 * math.asin(math.sqrt(max(0, min(1, haversine))))


def select_sessions(snapshot, callsign, frequency, users, listeners, channel_id,
                    own_session, radius=DEFAULT_RANGE_NM, now=None):
    now = time.time() if now is None else now
    radius = valid_radius(radius)
    if snapshot is None or radius is None or channel_id is None or not snapshot.fresh(now):
        return []
    station = snapshot.station(callsign, frequency, now)
    if station is None:
        return []
    selected = []
    for session, user in users.items():
        if session == own_session:
            continue
        if user.get("channel_id") != channel_id and channel_id not in listeners.get(session, ()):
            continue
        position = snapshot.receiver(user.get("name"), now)
        if position is not None and distance_nm(station, position) <= radius:
            selected.append(session)
    return sorted(selected)


def fetch_feed(url):
    request = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (compatible; CanATIS/1.0)", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT) as response:
        payload = response.read(MAX_FEED_BYTES + 1)
    if len(payload) > MAX_FEED_BYTES:
        raise ValueError("feed exceeds routing size limit")
    return json.loads(payload)


class FeedReader:
    def __init__(self, url):
        self.url = url
        self.refs = 0
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self._snapshot = None
        self._subscribers = set()
        self.thread = threading.Thread(target=self._run, name="atis-feed", daemon=True)

    def snapshot(self):
        with self.lock:
            return self._snapshot

    def refresh(self):
        data = None
        try:
            data = fetch_feed(self.url)
            snapshot = FeedSnapshot(data)
            if not snapshot.fresh(time.time()):
                snapshot = None
        except Exception:
            snapshot = None
            log.debug("routing feed unavailable")
        with self.lock:
            self._snapshot = snapshot
            subscribers = tuple(self._subscribers)
        for callback in subscribers:
            try:
                callback(data if snapshot is not None else None)
            except Exception:
                log.debug("routing feed subscriber failed", exc_info=True)

    def subscribe(self, callback):
        with self.lock:
            self._subscribers.add(callback)

    def unsubscribe(self, callback):
        with self.lock:
            self._subscribers.discard(callback)

    def _run(self):
        while not self.stop_event.is_set():
            started = time.monotonic()
            self.refresh()
            self.stop_event.wait(max(0.01, POLL_INTERVAL - (time.monotonic() - started)))


_feeds = {}
_feeds_lock = threading.Lock()


def acquire_feed(url=DEFAULT_URL):
    url = str(url or DEFAULT_URL).strip()
    with _feeds_lock:
        reader = _feeds.get(url)
        if reader is None:
            reader = _feeds[url] = FeedReader(url)
            reader.thread.start()
        reader.refs += 1
        return reader


def release_feed(reader):
    with _feeds_lock:
        if _feeds.get(reader.url) is not reader:
            return
        reader.refs -= 1
        if reader.refs:
            return
        del _feeds[reader.url]
        reader.stop_event.set()
    if reader.thread is not threading.current_thread():
        reader.thread.join(timeout=FETCH_TIMEOUT + 1)


class ATISRouter:
    """Serialize raw membership, PCM enqueue, and the complete encoder/send operation."""

    def __init__(self, callsign, frequency, url=DEFAULT_URL, radius=DEFAULT_RANGE_NM, feed=None):
        self.callsign, self.frequency = callsign, frequency
        self.url, self.radius = url, valid_radius(radius)
        self.feed = feed
        self._owned_feed = False
        self._lock = threading.RLock()
        self._members = {}
        self._listeners = {}
        self._channels = {}
        self._synced = False
        self._disabled = False
        self._output = None
        self._queued_key = None
        self._programmed_key = None
        self.generation = 0
        self.mumble = None

    def attach(self, mumble):
        if self.feed is None:
            self.feed = acquire_feed(self.url)
            self._owned_feed = True
        self.mumble = mumble
        self._disabled = False
        init = mumble.init_connection
        dispatch = mumble.dispatch_control_message

        def initialize(instance):
            with self._lock:
                self._clear()
                init()
                self._members.clear()
                self._listeners.clear()
                self._channels.clear()
                self._synced = False
                self._install_output()

        def receive(instance, kind, body):
            with self._lock:
                self._observe(kind, body)
                return dispatch(kind, body)

        mumble.init_connection = types.MethodType(initialize, mumble)
        mumble.dispatch_control_message = types.MethodType(receive, mumble)
        with self._lock:
            self._clear()
            self._members.clear()
            self._listeners.clear()
            self._channels.clear()
            self._synced = False
            self._output = None
            self._programmed_key = None
            # Real pymumble constructs connection state only inside run()->init_connection().
            if all(hasattr(mumble, field) for field in ("users", "channels", "sound_output")):
                self._members = {s: {"name": u.get("name"), "channel_id": u.get("channel_id", 0)}
                                 for s, u in mumble.users.items()}
                self._channels = {c: {"name": v.get("name")} for c, v in mumble.channels.items()}
                self._synced = getattr(mumble, "connected", None) == 2
                self._install_output()

    def _install_output(self):
        output = self.mumble.sound_output
        output.clear_buffer()
        output.target = TARGET_ID
        original_send = output.send_audio
        self._output = output
        self._queued_key = self._programmed_key = None

        def send():
            with self._lock:
                if output is not self._output:
                    output.clear_buffer()
                    return
                key = self._route()
                if key != self._queued_key:
                    self._clear()
                self._program(key)
                if key:
                    original_send()

        output.send_audio = send

    def _clear(self):
        if self._output is not None:
            self._output.clear_buffer()
        self._queued_key = None
        self.generation += 1

    def _observe(self, kind, body):
        if kind not in (5, 6, 7, 8, 9):
            return
        from pymumble_py3 import mumble_pb2
        if kind == 5:
            self._synced = True
            return
        previous_route = self._route()
        if kind == 9:
            message = mumble_pb2.UserState()
            message.ParseFromString(body)
            if not message.HasField("session"):
                return
            session = message.session
            member = self._members.setdefault(session, {"channel_id": 0})
            for field in ("name", "channel_id"):
                if message.HasField(field):
                    member[field] = getattr(message, field)
            listening = self._listeners.setdefault(session, set())
            listening.update(message.listening_channel_add)
            listening.difference_update(message.listening_channel_remove)
        elif kind == 8:
            message = mumble_pb2.UserRemove()
            message.ParseFromString(body)
            self._members.pop(message.session, None)
            self._listeners.pop(message.session, None)
        elif kind == 7:
            message = mumble_pb2.ChannelState()
            message.ParseFromString(body)
            if message.HasField("channel_id") and message.HasField("name"):
                channel = {"name": message.name}
                if self._channels.get(message.channel_id) != channel:
                    self._channels[message.channel_id] = channel
        elif kind == 6:
            message = mumble_pb2.ChannelRemove()
            message.ParseFromString(body)
            self._channels.pop(message.channel_id, None)
            for member in self._members.values():
                if member.get("channel_id") == message.channel_id:
                    member["channel_id"] = 0
            for listening in self._listeners.values():
                listening.discard(message.channel_id)
        if self._route() != previous_route:
            self._clear()

    def _route(self, members=None):
        if (self._disabled or not self._synced or self.mumble is None
                or getattr(self.mumble, "connected", None) != 2):
            return ()
        matches = [c for c, channel in self._channels.items()
                   if channel.get("name") == f"FREQ_{self.frequency:06d}"]
        channel = matches[0] if len(matches) == 1 else None
        own = self.mumble.users.myself_session
        if self._members.get(own, {}).get("channel_id") != channel:
            return ()
        recipients = select_sessions(self.feed.snapshot(), self.callsign, self.frequency,
                                     self._members if members is None else members,
                                     self._listeners, channel, own,
                                     self.radius, time.time())
        return tuple((session, self._members[session].get("name")) for session in recipients)

    def _program(self, key):
        if key == self._programmed_key:
            return
        if getattr(self.mumble, "connected", None) != 2:
            return
        from pymumble_py3 import mumble_pb2
        target = mumble_pb2.VoiceTarget(id=TARGET_ID)
        if key:
            target.targets.add().session.extend(session for session, name in key)
        self.mumble.send_message(19, target)
        self._output.target = TARGET_ID
        self._programmed_key = key

    def begin_cycle(self):
        with self._lock:
            key = self._route()
            if not key:
                self._clear()
                return None
            if key != self._queued_key:
                self._clear()
                self._queued_key = key
            return self.generation

    def enqueue(self, pcm, generation=None):
        with self._lock:
            if self._output is None or not self._output.encoder_framesize:
                self._clear()
                return False
            key = self._route()
            if not key:
                self._clear()
                return False
            if key != self._queued_key:
                self._clear()
                self._queued_key = key
            if generation is not None and generation != self.generation:
                self._clear()
                return False
            self._output.add_sound(pcm)
            return True

    def discard(self):
        with self._lock:
            self._clear()

    def hears(self, user):
        with self._lock:
            session = user.get("session")
            member = self._members.get(session)
            if member is None or member.get("name") != user.get("name"):
                return False
            return bool(self._route({session: member}))

    def disable(self):
        with self._lock:
            self._disabled = True
            self._clear()

    def close(self):
        self.disable()
        if self._owned_feed:
            self._owned_feed = False
            release_feed(self.feed)
            self.feed = None
