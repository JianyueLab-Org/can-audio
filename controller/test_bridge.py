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


if __name__ == '__main__':
    unittest.main()
