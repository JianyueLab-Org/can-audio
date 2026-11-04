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

    def test_calibrate_does_not_need_a_connection(self):
        # 有没有麦克风都行：没有时是 error 事件，有时先来 quiet；命令本身都立即回 null
        process = subprocess.Popen([sys.executable, '-u', str(Path(__file__).with_name('bridge.py'))],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding='utf-8')
        try:
            process.stdin.write(json.dumps({'id': 1, 'command': 'calibrate',
                                            'data': {'input': None, 'denoise': False}}) + '\n')
            process.stdin.flush()
            time.sleep(1.5)
            output, error = process.communicate('\n'.join(json.dumps(item) for item in [
                {'id': 2, 'command': 'calibrate_cancel'},
                {'id': 3, 'command': 'quit'},
            ]) + '\n', timeout=10)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()
        self.assertEqual(process.returncode, 0, error)
        messages = [json.loads(line) for line in output.splitlines()]
        replies = {msg['id']: msg for msg in messages if 'id' in msg}
        self.assertIsNone(replies[1]['result'])
        self.assertIsNone(replies[2]['result'])
        stages = [msg['stage'] for msg in messages if msg.get('event') == 'calibration']
        self.assertTrue(stages, output)
        self.assertIn(stages[0], ('quiet', 'error'))

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


if __name__ == '__main__':
    unittest.main()
