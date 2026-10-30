"""验证真实无界面进程的协议及清理，不访问语音服务器。"""
import json
from pathlib import Path
import subprocess
import sys
import time
import unittest


class BridgeTest(unittest.TestCase):
    def test_periodic_status_without_a_voice_session(self):
        process = subprocess.Popen([sys.executable, '-u', str(Path(__file__).with_name('bridge.py'))],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding='utf-8')
        try:
            time.sleep(2)
            output, error = process.communicate('{"id":1,"command":"quit"}\n', timeout=10)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()
        self.assertEqual(process.returncode, 0, error)
        statuses = [message for line in output.splitlines()
                    if (message := json.loads(line)).get('event') == 'connection']
        self.assertTrue(statuses, output)
        self.assertFalse(statuses[-1]['connected'])
        self.assertIsNone(statuses[-1]['latency_ms'])

    def test_devices_errors_and_shutdown(self):
        process = subprocess.Popen([sys.executable, '-u', str(Path(__file__).with_name('bridge.py'))],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding='utf-8')
        commands = '\n'.join(json.dumps(item) for item in [
            {'id': 1, 'command': 'devices'},
            {'id': 2, 'command': 'does_not_exist'},
            {'id': 3, 'command': 'ptt', 'data': {'active': False}},
            {'id': 4, 'command': 'quit'},
        ]) + '\n'
        output, error = process.communicate(commands, timeout=20)
        self.assertEqual(process.returncode, 0, error)
        messages = [json.loads(line) for line in output.splitlines()]
        self.assertEqual(messages[0], {'event': 'ready'})
        replies = {msg['id']: msg for msg in messages if 'id' in msg}
        self.assertIsInstance(replies[1]['result'], list)
        self.assertIn('error', replies[2])
        self.assertIsNone(replies[3]['result'])

    def test_sync_plan_is_what_csharp_sends(self):
        from syncplan import SyncPlan
        plan = SyncPlan.from_dict({'rx': [118000, 121700], 'tx': [118000], 'xc': [],
                                   'selected': 118000, 'locked': [118000],
                                   'volumes': {'118000': 80}})
        self.assertEqual(plan.rx_frequencies(), [118000, 121700])
        self.assertEqual(plan.tx_frequencies(), [118000])
        self.assertEqual(plan.selected_khz, 118000)
        self.assertEqual(plan.locked_khz, {118000})
        self.assertEqual({r.frequency_khz: r.effective_volume() for r in plan},
                         {118000: 80, 121700: 100})

    def test_range_audio_command_requires_connection(self):
        process = subprocess.Popen([sys.executable, '-u', str(Path(__file__).with_name('bridge.py'))],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding='utf-8')
        try:
            output, error = process.communicate('\n'.join(json.dumps(item) for item in [
                {'id': 1, 'command': 'range_audio', 'data': {'1003': False}},
                {'id': 2, 'command': 'quit'},
            ]) + '\n', timeout=10)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()
        self.assertEqual(process.returncode, 0, error)
        replies = {message['id']: message for message in map(json.loads, output.splitlines())
                   if 'id' in message}
        self.assertIn('error', replies[1])
        self.assertIn('尚未连接', replies[1]['error'])
        self.assertNotIn(2, replies)

    def test_invalid_range_audio_payloads_preserve_previous_policy(self):
        # 在独立进程运行真正的命令循环，只替换联网和声卡初始化。
        script = '''
from unittest.mock import patch
import bridge
from voice import VoiceClient
client = VoiceClient("host", "1000", "pw")
original = client.set_range_audio_policy
def apply(policy):
    if policy == {}:
        bridge.emit(id=998, result=client._range_audio_policy)
    return original(policy)
client.set_range_audio_policy = apply
with patch("voice.VoiceClient", return_value=client), \\
     patch.object(client, "connect", return_value=True), \\
     patch.object(client, "disconnect"):
    bridge.main()
bridge.emit(id=999, result=client._range_audio_policy)
'''
        invalid = [[], False, 0, "", None, ["1001", False], {"1001": 1}, {"": False}]
        for payload in invalid + ['missing']:
            with self.subTest(payload=payload):
                request = {'id': 3, 'command': 'range_audio'}
                if payload != 'missing':
                    request['data'] = payload
                requests = [
                    {'id': 1, 'command': 'connect', 'data': {
                        'host': 'host', 'cid': '1000', 'password': 'pw', 'port': 64738}},
                    {'id': 2, 'command': 'range_audio', 'data': {'1001': False}},
                    request,
                    {'id': 4, 'command': 'range_audio', 'data': {}},
                    {'id': 5, 'command': 'quit'},
                ]
                process = subprocess.run([sys.executable, '-c', script],
                    cwd=Path(__file__).parent, input='\n'.join(map(json.dumps, requests)) + '\n',
                    capture_output=True, text=True, encoding='utf-8', timeout=10)
                self.assertEqual(process.returncode, 0, process.stderr)
                replies = {msg['id']: msg for msg in map(json.loads, process.stdout.splitlines()) if 'id' in msg}
                self.assertIn('error', replies[3])
                self.assertEqual(replies[998]['result'], {'1001': False})
                self.assertEqual(replies[4]['result'], {'entries': 0})
                self.assertEqual(replies[999]['result'], {})


class FakeAudio:
    """Windows 上的样子：同一个耳机按 MME、DirectSound、WASAPI、WDM-KS 各出现一次。"""
    HOSTS = ['MME', 'Windows DirectSound', 'Windows WASAPI', 'Windows WDM-KS']
    DEVICES = [
        (0, '麦克风 (Arctis 7 Chat)', 1, 0),
        (0, '耳机 (Arctis 7 Chat)', 0, 2),
        (1, '麦克风 (Arctis 7 Chat)', 1, 0),
        (2, '麦克风 (Arctis 7 Chat)', 1, 0),
        (2, '耳机 (Arctis 7 Chat)', 0, 2),
        (3, '麦克风 (Arctis 7 Chat)', 1, 0),
    ]

    def get_default_host_api_info(self):
        return {'index': 0, 'name': self.HOSTS[0]}

    def get_device_count(self):
        return len(self.DEVICES)

    def get_device_info_by_index(self, index):
        host, name, inputs, outputs = self.DEVICES[index]
        return {'hostApi': host, 'name': name,
                'maxInputChannels': inputs, 'maxOutputChannels': outputs}


class DeviceListTest(unittest.TestCase):
    def test_only_the_default_host_api_is_listed(self):
        from audiodevices import list_devices
        self.assertEqual(list_devices(FakeAudio()), [
            {'index': 0, 'name': '麦克风 (Arctis 7 Chat)', 'host_api': 'MME', 'input': True, 'output': False},
            {'index': 1, 'name': '耳机 (Arctis 7 Chat)', 'host_api': 'MME', 'input': False, 'output': True},
        ])

    def test_names_are_unique_so_a_name_lookup_cannot_land_on_wdm_ks(self):
        from audiodevices import list_devices
        names = [(d['name'], d['input']) for d in list_devices(FakeAudio())]
        self.assertEqual(len(names), len(set(names)))


if __name__ == '__main__':
    unittest.main()
