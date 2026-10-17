"""Geographic selection and actual pymumble sender-gate regressions."""

import copy
import importlib.util
import logging
import pathlib
import struct
import sys
import threading
import time
import types
import unittest
from unittest import mock

for _name in ("opuslib", "opuslib.api", "opuslib.api.decoder",
              "opuslib.api.encoder", "opuslib.api.info", "opuslib.exceptions"):
    sys.modules.setdefault(_name, mock.MagicMock())


NOW = 1000.0


def entry(cid, callsign, lat=35.0, lon=139.0, frequency="128.800"):
    return {"cid": str(cid), "callsign": callsign, "latitude": lat,
            "longitude": lon, "frequency": frequency, "send_time": NOW,
            "position_time": NOW, "facility": 0}


def fixture():
    return {"general": {"update_timestamp": NOW},
            "pilots": [entry(1001, "CAN1"), entry(1002, "CAN2", 34.0, 135.0)],
            "controllers": [], "atis": [entry(900, "RJTT_ATIS"),
                                           entry(900, "RJOO_ATIS", 34.0, 135.0)]}


def users():
    return {11: {"session": 11, "name": "1001", "channel_id": 7},
            12: {"session": 12, "name": "1002", "channel_id": 7},
            13: {"session": 13, "name": "1003", "channel_id": 7},
            99: {"session": 99, "name": "900_atis128800_RJTT_ATIS", "channel_id": 7}}


class RoutingTestCase(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec("atisrouting"),
                             "Geographic ATIS routing is not implemented")
        import atisrouting
        self.routing = atisrouting


class SelectionTest(RoutingTestCase):
    def select(self, data=None, sessions=None, listeners=None, now=NOW, radius=100):
        snapshot = self.routing.FeedSnapshot(fixture() if data is None else data)
        return self.routing.select_sessions(snapshot, "RJTT_ATIS", 128800,
                                            users() if sessions is None else sessions,
                                            listeners or {}, 7, 99, radius, now)

    def test_same_frequency_airports_and_voice_only_observer(self):
        self.assertEqual(self.select(), [11])

    def test_station_live_override_is_authoritative(self):
        data = fixture()
        data["atis"][0].update(latitude=34.0, longitude=135.0)
        self.assertEqual(self.select(data), [12])

    def test_wrong_frequency_and_listening_on_secondary_radio(self):
        sessions = users()
        sessions[11]["channel_id"] = 8
        self.assertEqual(self.select(sessions=sessions), [])
        self.assertEqual(self.select(sessions=sessions, listeners={11: {7}}), [11])

    def test_boundary_in_nautical_miles_and_zero_coordinates(self):
        data = fixture()
        data["atis"][0].update(latitude=0, longitude=0)
        data["pilots"][0].update(latitude=0, longitude=1)
        self.assertEqual(self.select(data, radius=60.1), [11])
        self.assertEqual(self.select(data, radius=60.0), [])

    def test_feed_and_coordinate_expiry_and_future_skew(self):
        self.assertEqual(self.select(now=NOW + 21), [])
        for section, field in (("pilots", "position_time"), ("atis", "send_time")):
            for value in (NOW - 91, NOW + 6, float("nan"), None):
                with self.subTest(section=section, value=value):
                    data = fixture()
                    data[section][0][field] = value
                    self.assertEqual(self.select(data), [])
        for value in (NOW - 21, NOW + 6, None, float("nan"), True):
            data = fixture()
            data["general"]["update_timestamp"] = value
            self.assertEqual(self.select(data), [])

    def test_missing_pilot_coordinate_timestamp_does_not_use_send_time(self):
        data = fixture()
        del data["pilots"][0]["position_time"]
        self.assertEqual(self.select(data), [])

    def test_invalid_coordinates_radii_and_station_frequency(self):
        for field, value in (("latitude", None), ("latitude", 91),
                             ("longitude", float("inf")), ("longitude", -181)):
            data = fixture()
            data["pilots"][0][field] = value
            self.assertEqual(self.select(data), [])
        for radius in (0, -1, float("nan"), float("inf"), None, True):
            self.assertEqual(self.select(radius=radius), [])
        data = fixture()
        data["atis"][0]["frequency"] = "128.805"
        self.assertEqual(self.select(data), [])

    def test_duplicates_count_even_when_invalid_and_positioned_fsd_obs(self):
        data = fixture()
        data["controllers"].append(entry(1001, "RJTT_OBS", lat=None))
        self.assertEqual(self.select(data), [])
        data = fixture()
        data["pilots"] = data["pilots"][1:]
        data["controllers"].append(entry(1001, "RJTT_OBS"))
        self.assertEqual(self.select(data), [11])
        data["atis"].append(entry(900, "RJTT_ATIS", lat=None))
        self.assertEqual(self.select(data), [])

    def test_nearby_atis_peer_can_yield_but_legacy_is_unknown(self):
        data = fixture()
        data["atis"].append(entry(901, "RJTT_D_ATIS"))
        sessions = users()
        sessions[20] = {"name": "901_atis128800_RJTT_D_ATIS", "channel_id": 7}
        sessions[21] = {"name": "900_atis128800_RJOO_ATIS", "channel_id": 7}
        sessions[22] = {"name": "900_atis128800", "channel_id": 7}
        self.assertEqual(self.select(data, sessions), [11, 20])


class FeedTest(RoutingTestCase):
    def test_reader_is_shared_and_final_release_stops_it(self):
        with mock.patch.object(self.routing.time, "time", return_value=NOW), \
                mock.patch.object(self.routing, "fetch_feed", return_value=fixture()) as fetch:
            first = self.routing.acquire_feed("https://synthetic.invalid/feed")
            second = self.routing.acquire_feed("https://synthetic.invalid/feed")
            try:
                self.assertIs(first, second)
                first.thread.join(timeout=0.02)
                self.assertTrue(first.thread.is_alive())
                self.routing.release_feed(first)
                self.assertFalse(first.stop_event.is_set())
                first.refresh()
                self.assertIsNotNone(first.snapshot())
                fetch.side_effect = ValueError("malformed response")
                first.refresh()
                self.assertIsNone(first.snapshot())
            finally:
                self.routing.release_feed(second)
            self.assertFalse(first.thread.is_alive())

    def test_reader_metadata_callback_does_not_retain_full_feed(self):
        received = []
        reader = self.routing.FeedReader("https://synthetic.invalid/feed")
        with mock.patch.object(self.routing.time, "time", return_value=NOW), \
                mock.patch.object(self.routing, "fetch_feed", return_value=fixture()):
            reader.subscribe(received.append)
            reader.refresh()
            self.assertEqual(received[0]["atis"][0]["callsign"], "RJTT_ATIS")
            reader.unsubscribe(received.append)
            reader.refresh()
            self.assertEqual(len(received), 1)


class FakeFeed:
    def __init__(self, routing):
        self.data = fixture()
        self.routing = routing

    def snapshot(self):
        return self.routing.FeedSnapshot(self.data) if self.data else None


class Wire:
    def __init__(self):
        self.messages = []
        self.audio = []

    def send(self, packet):
        kind, size = struct.unpack("!HL", packet[:6])
        body = packet[6:]
        assert size == len(body)
        self.messages.append((kind, body, threading.get_ident()))
        if kind == 1:
            self.audio.append(body[0] & 31)
        return len(packet)


class Encoder:
    def __init__(self):
        self.popped = None
        self.resume = None

    def encode(self, pcm, samples):
        if self.popped:
            self.popped.set()
            assert self.resume.wait(2)
        return b"encoded"


class FakeMumble:
    def __init__(self):
        self.Log = logging.getLogger("test")
        self.positional = None
        self.udp_active = False
        self.connected = 2
        self.control_socket = Wire()
        self.init_connection()

    def init_connection(self):
        from pymumble_py3.soundoutput import SoundOutput
        self.sound_output = SoundOutput(self, 0.02, 50000)
        self.sound_output.encoder = Encoder()
        self.sound_output.encoder_framesize = 0.02
        self.sound_output.codec_type = 4
        class Users(dict):
            myself_session = 99
            @property
            def myself(self):
                return self.get(self.myself_session)
        self.users = Users(users())
        self.channels = {7: {"name": "FREQ_128800"}, 8: {"name": "FREQ_120000"}}

    def dispatch_control_message(self, kind, body):
        return None

    def send_message(self, kind, message):
        body = message.SerializeToString()
        self.control_socket.send(struct.pack("!HL", kind, len(body)) + body)


class GateFixture(RoutingTestCase):
    def setUp(self):
        super().setUp()
        self.clock = mock.patch.object(self.routing.time, "time", return_value=NOW)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.feed = FakeFeed(self.routing)
        self.mumble = FakeMumble()
        self.router = self.routing.ATISRouter("RJTT_ATIS", 128800, feed=self.feed)
        self.router.attach(self.mumble)
        self.addCleanup(self.router.close)

    def send(self):
        self.mumble.sound_output.sequence_last_time = 0
        self.mumble.sound_output.send_audio()

    def delta(self, session=11, add=(), remove=(), channel=None, name=None):
        from pymumble_py3 import mumble_pb2
        message = mumble_pb2.UserState(session=session)
        message.listening_channel_add.extend(add)
        message.listening_channel_remove.extend(remove)
        if channel is not None:
            message.channel_id = channel
        if name is not None:
            message.name = name
        self.mumble.dispatch_control_message(9, message.SerializeToString())


class GateTest(GateFixture):
    def test_only_nonzero_whisper_and_registration_precedes_audio(self):
        self.assertTrue(self.router.enqueue(b"\x00" * 1920))
        self.send()
        self.assertEqual(self.mumble.control_socket.audio, [2])
        self.assertEqual([kind for kind, body, thread in self.mumble.control_socket.messages], [19, 1])
        from pymumble_py3 import mumble_pb2
        target = mumble_pb2.VoiceTarget()
        target.ParseFromString(self.mumble.control_socket.messages[0][1])
        self.assertEqual(target.id, 2)
        self.assertEqual(list(target.targets[0].session), [11])

    def test_route_change_and_expiry_drop_buffered_tails(self):
        self.router.enqueue(b"\x00" * 1920)
        self.feed.data["pilots"][0]["longitude"] = 135.0
        self.send()
        self.assertEqual(self.mumble.control_socket.audio, [])
        self.assertEqual(self.mumble.sound_output.get_buffer_size(), 0)
        self.assertEqual(self.mumble.sound_output.target, 2)
        self.assertFalse(self.router.enqueue(b"\x00" * 1920))

    def test_listener_add_remove_add_and_session_reuse(self):
        self.delta(channel=8)
        self.assertFalse(self.router.enqueue(b"\x00" * 1920))
        for add, remove, eligible in (([7], [], True), ([], [7], False), ([7], [], True)):
            self.delta(add=add, remove=remove)
            self.assertEqual(self.router.enqueue(b"\x00" * 1920), eligible)
        from pymumble_py3 import mumble_pb2
        self.mumble.dispatch_control_message(8, mumble_pb2.UserRemove(session=11).SerializeToString())
        self.delta(channel=8, name="1001")
        self.assertFalse(self.router.enqueue(b"\x00" * 1920))

    def test_channel_removal_clears_listeners_and_disables(self):
        self.delta(channel=8, add=[7])
        from pymumble_py3 import mumble_pb2
        self.mumble.dispatch_control_message(6, mumble_pb2.ChannelRemove(channel_id=7).SerializeToString())
        self.assertFalse(self.router.enqueue(b"\x00" * 1920))

    def test_deleted_channel_reuse_does_not_restore_old_membership(self):
        from pymumble_py3 import mumble_pb2
        self.mumble.dispatch_control_message(6, mumble_pb2.ChannelRemove(channel_id=7).SerializeToString())
        self.mumble.dispatch_control_message(7, mumble_pb2.ChannelState(
            channel_id=7, name="FREQ_128800").SerializeToString())
        self.delta(99, channel=7)
        self.assertFalse(self.router.enqueue(b"\x00" * 1920))

    def test_pending_codec_does_not_crash_or_enqueue(self):
        self.mumble.sound_output.encoder_framesize = None
        with mock.patch.object(self.mumble.sound_output, "add_sound") as add_sound:
            self.assertFalse(self.router.enqueue(b"\x00" * 1920))
        add_sound.assert_not_called()

    def test_reconnect_wraps_new_output_and_discards_old_tail(self):
        self.router.enqueue(b"\x00" * 1920)
        self.mumble.init_connection()
        self.send()
        self.assertEqual(self.mumble.control_socket.audio, [])
        self.assertEqual(self.mumble.sound_output.target, 2)
        self.assertFalse(self.router.enqueue(b"\x00" * 1920))
        from pymumble_py3 import mumble_pb2
        self.mumble.dispatch_control_message(7, mumble_pb2.ChannelState(
            channel_id=7, name="FREQ_128800").SerializeToString())
        self.delta(99, channel=7, name="900_atis128800_RJTT_ATIS")
        self.delta(11, channel=7, name="1001")
        self.mumble.dispatch_control_message(5, b"")
        self.assertTrue(self.router.enqueue(b"\x00" * 1920))

    def test_disable_waits_for_popped_frame_and_never_retargets_it(self):
        output = self.mumble.sound_output
        popped, resume, disabled = threading.Event(), threading.Event(), threading.Event()
        output.encoder.popped, output.encoder.resume = popped, resume
        self.router.enqueue(b"\x00" * 1920)
        sender = threading.Thread(target=self.send)
        sender.start()
        self.assertTrue(popped.wait(2))
        closer = threading.Thread(target=lambda: (self.router.disable(), disabled.set()))
        closer.start()
        self.assertFalse(disabled.wait(0.05))
        resume.set()
        sender.join(2)
        closer.join(2)
        self.assertEqual(self.mumble.control_socket.audio, [2])
        self.assertTrue(disabled.is_set())
        self.assertEqual(output.get_buffer_size(), 0)
        self.assertFalse(self.router.enqueue(b"\x00" * 1920))

    def test_registration_occurs_on_sender_not_enqueue_thread(self):
        self.router.enqueue(b"\x00" * 1920)
        self.assertEqual(self.mumble.control_socket.messages, [])
        sender = threading.Thread(target=self.send)
        sender.start()
        sender.join(2)
        self.assertEqual({thread for kind, body, thread in self.mumble.control_socket.messages}, {sender.ident})

    def test_cycle_does_not_enqueue_tail_after_membership_changes(self):
        generation = self.router.begin_cycle()
        self.assertTrue(self.router.enqueue(b"\x00" * 1920, generation))
        self.delta(channel=8)
        self.delta(channel=7)
        self.assertFalse(self.router.enqueue(b"\x00" * 1920, generation))
        self.assertEqual(self.mumble.sound_output.get_buffer_size(), 0)

    def test_unrelated_membership_and_channel_churn_preserves_cycle(self):
        from pymumble_py3 import mumble_pb2
        generation = self.router.begin_cycle()
        self.assertTrue(self.router.enqueue(b"\x00" * 1920, generation))
        self.delta(12, channel=8)
        self.mumble.dispatch_control_message(7, mumble_pb2.ChannelState(
            channel_id=20, name="FREQ_120005").SerializeToString())
        self.mumble.dispatch_control_message(6, mumble_pb2.ChannelRemove(channel_id=20).SerializeToString())
        self.mumble.dispatch_control_message(8, mumble_pb2.UserRemove(session=12).SerializeToString())
        self.assertEqual(self.router.generation, generation)
        self.assertTrue(self.router.enqueue(b"\x00" * 1920, generation))
        self.send()
        self.assertEqual(self.mumble.control_socket.audio, [2])

    def test_helper_copies_are_identical(self):
        path = pathlib.Path(__file__).resolve()
        root = path.parents[2] if path.parent.name == "ATIS" else path.parents[1]
        self.assertEqual((root / "atis/atisrouting.py").read_bytes(),
                         (root / "server/ATIS/atisrouting.py").read_bytes())


class RealStartupTest(RoutingTestCase):
    def run_startup(self, reconnect):
        from pymumble_py3 import mumble_pb2
        from pymumble_py3 import mumble as library
        from pymumble_py3 import soundoutput

        def framed(kind, message):
            payload = message.SerializeToString()
            return struct.pack("!HL", kind, len(payload)) + payload

        class Transport(Wire):
            def __init__(self):
                super().__init__()
                self.incoming = b"".join((
                    framed(7, mumble_pb2.ChannelState(channel_id=7, name="FREQ_128800")),
                    framed(9, mumble_pb2.UserState(session=99, channel_id=7,
                                                  name="900_atis128800_RJTT_ATIS")),
                    framed(9, mumble_pb2.UserState(session=11, channel_id=7, name="1001")),
                    framed(5, mumble_pb2.ServerSync(session=99, max_bandwidth=50000)),
                    framed(21, mumble_pb2.CodecVersion(alpha=0, beta=0, prefer_alpha=False, opus=True))))

            def settimeout(self, timeout):
                pass

            def setblocking(self, blocking):
                pass

            def connect(self, address):
                pass

            def close(self):
                pass

            def recv(self, size):
                payload, self.incoming = self.incoming, b""
                return payload

        encoders = []
        class RecordingEncoder(Encoder):
            def __init__(self, *args):
                super().__init__()
                self.frames = []
                encoders.append(self)

            def encode(self, pcm, samples):
                self.frames.append(pcm)
                return super().encode(pcm, samples)

        mumble = library.Mumble("synthetic.invalid", "900_atis128800_RJTT_ATIS",
                                reconnect=reconnect)
        self.assertFalse(any(hasattr(mumble, name) for name in ("users", "channels", "sound_output")))
        router = self.routing.ATISRouter("RJTT_ATIS", 128800, feed=FakeFeed(self.routing))
        self.addCleanup(router.close)
        try:
            router.attach(mumble)
        except AttributeError as error:
            self.fail(f"Routing eagerly reads uninitialized pymumble state: {error}")
        transports = [Transport(), Transport()] if reconnect else [Transport()]
        outputs = []
        frame, stale_tail, fresh_frame = [bytes([value, 0]) * 960 for value in (1, 2, 3)]
        selects = 0

        def select(readable, writable, exceptional, timeout):
            nonlocal selects
            selects += 1
            self.assertLess(selects, 12, "Actual pymumble startup never produced routed audio")
            wire = mumble.control_socket
            wire = getattr(wire, "_sock", wire)
            if wire.incoming:
                outputs.append(mumble.sound_output)
                self.assertEqual(mumble.sound_output.target, 2)
                self.assertFalse(router.enqueue(frame), "Unsynchronized connection accepted PCM")
                return ([mumble.control_socket], [], [])
            if not wire.audio:
                self.assertTrue(router.enqueue(frame if wire is transports[0] else fresh_frame))
                return ([], [], [])
            if reconnect and wire is transports[0]:
                self.assertTrue(router.enqueue(stale_tail))
                return ([], [], [mumble.control_socket])
            mumble.reconnect = False
            mumble.stop()
            return ([], [], [])

        with mock.patch.object(self.routing.time, "time", return_value=NOW), \
                mock.patch.object(library.socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("127.0.0.1", 0))]), \
                mock.patch.object(library.socket, "socket", side_effect=transports), \
                mock.patch.object(library.ssl, "wrap_socket", side_effect=lambda socket, **kwargs: socket, create=True), \
                mock.patch.object(library.select, "select", side_effect=select), \
                mock.patch.object(library, "PYMUMBLE_CONNECTION_RETRY_INTERVAL", 0), \
                mock.patch.object(soundoutput.opuslib, "Encoder", side_effect=RecordingEncoder):
            mumble.start()
            mumble.join(2)
            if mumble.is_alive():
                mumble.reconnect = False
                mumble.stop()
                mumble.join(2)
            self.assertFalse(mumble.is_alive())
        self.assertEqual([wire.audio for wire in transports], [[2], [2]] if reconnect else [[2]])
        self.assertEqual([encoder.frames for encoder in encoders],
                         [[frame], [fresh_frame]] if reconnect else [[frame]])
        self.assertEqual(outputs[0].get_buffer_size(), 0)
        if reconnect:
            self.assertIsNot(outputs[0], outputs[1])
        for wire in transports:
            kinds = [kind for kind, body, thread in wire.messages]
            self.assertLess(kinds.index(19), kinds.index(1))

    def test_fresh_real_mumble_constructor_and_startup(self):
        self.run_startup(False)

    def test_real_reconnect_initializes_and_wraps_replacement_output(self):
        self.run_startup(True)


class DaemonConsumerTest(GateFixture):
    def test_daemon_failed_start_closes_event_loop(self):
        import mumble
        broadcaster = mumble.ATISBroadcaster.__new__(mumble.ATISBroadcaster)
        broadcaster.user = "900_atis128800_RJTT_ATIS"
        broadcaster.connect_to_server = lambda: False
        broadcaster._stop_mumble = mock.MagicMock()
        broadcaster.loop = mock.MagicMock()
        broadcaster.run()
        broadcaster.loop.close.assert_called_once()

    def test_manager_rebinds_changed_frequency(self):
        import mumble
        manager = mumble.ATISManager()
        old = mock.MagicMock()
        old.channel_name = "FREQ_128800"
        old.is_alive.side_effect = [True, False]
        manager.broadcasters["RJTT_ATIS"] = old
        with mock.patch.object(mumble, "ATISBroadcaster") as constructor:
            manager._reconcile([{"callsign": "RJTT_ATIS", "frequency": "128.805", "text_atis": ["synthetic"]}])
        self.assertEqual(constructor.call_args.kwargs["frequency"], "128.805")
        old.stop.assert_called_once()
        self.assertIs(manager.broadcasters["RJTT_ATIS"], constructor.return_value)

    def test_daemon_transmit_uses_gate_and_rejects_missing_positions(self):
        import mumble
        broadcaster = mumble.ATISBroadcaster.__new__(mumble.ATISBroadcaster)
        broadcaster.mumble = self.mumble
        broadcaster._router = self.router
        broadcaster.running = True
        broadcaster.chunk_size = 1920
        broadcaster.check_channel_silence = lambda: True
        with mock.patch.object(mumble.time, "sleep", side_effect=lambda timeout: self.send()):
            self.assertTrue(broadcaster.broadcast_audio(b"\x00" * 1920))
        self.assertEqual(self.mumble.control_socket.audio, [2])
        self.feed.data = None
        self.assertFalse(broadcaster.broadcast_audio(b"\x00" * 1920))
        self.assertEqual(self.mumble.sound_output.get_buffer_size(), 0)

    def test_daemon_quiet_detection_drains_but_ignores_distant_audio(self):
        import mumble
        broadcaster = mumble.ATISBroadcaster.__new__(mumble.ATISBroadcaster)
        broadcaster.mumble = self.mumble
        broadcaster._router = self.router
        broadcaster.user = "900_atis128800_RJTT_ATIS"
        broadcaster.silence_duration = 1
        broadcaster.last_sound_time = NOW - 10
        for session in (11, 12):
            user = type("User", (dict,), {})(users()[session])
            user.sound = mock.MagicMock()
            user.sound.is_sound.return_value = session == 12
            self.mumble.users[session] = user
        broadcaster._on_sound(self.mumble.users[12], types.SimpleNamespace(pcm=b"distant"))
        self.assertTrue(broadcaster.check_channel_silence())
        broadcaster._on_sound(self.mumble.users[11], types.SimpleNamespace(pcm=b"nearby"))
        self.assertFalse(broadcaster.check_channel_silence())
        self.mumble.users[12].sound.get_sound.assert_called()

    def test_daemon_routing_configuration(self):
        import mumble
        with mock.patch.dict("os.environ", {"ATIS_RANGE_NM": "75", "ATIS_DATAFEED_URL": "https://synthetic.invalid/data"}):
            self.assertEqual(mumble.serverconf.atis_range_nm(), 75)
            self.assertEqual(mumble.serverconf.atis_datafeed_url(), "https://synthetic.invalid/data")


if __name__ == "__main__":
    unittest.main()
