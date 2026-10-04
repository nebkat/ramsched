import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAUNCHER = os.path.join(ROOT, 'compile-memory')
FAKE_COMPILER = os.path.join(ROOT, 'tests', 'fake_compiler.py')


class LauncherTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = self.directory.name
        self.environment = dict(
            os.environ,
            COMPILE_MEMORY_DIR=os.path.join(self.path, 'state'),
            COMPILE_MEMORY_HISTORY=os.path.join(self.path, 'state', 'history.json'),
            COMPILE_MEMORY_BUDGET='0.5',
            COMPILE_MEMORY_DEFAULT='0.2',
        )

    def tearDown(self):
        self.directory.cleanup()

    def start(self, megabytes, seconds, name, **environment):
        command = [sys.executable, LAUNCHER, sys.executable, FAKE_COMPILER,
                   str(megabytes), str(seconds), '-o', name]
        return subprocess.Popen(command, cwd=self.path, env=dict(self.environment, **environment))

    def events(self):
        with open(os.path.join(self.path, 'state', 'events.log')) as file:
            return [line.rstrip('\n').split('\t')[1:] for line in file]

    def ledger(self):
        with open(os.path.join(self.path, 'state', 'ledger.json')) as file:
            return json.load(file)

    def wait_for_event(self, name, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                matches = [event for event in self.events() if event[0] == name]
                if matches:
                    return matches
            except OSError:
                pass
            time.sleep(0.1)
        self.fail(f'no {name} event within {timeout}s')

    def test_admits_only_what_fits_the_budget(self):
        processes = [self.start(120, 2, f'out{index}.o') for index in range(4)]
        for process in processes:
            self.assertEqual(process.wait(), 0)
        admits = [event for event in self.events() if event[0] == 'admit']
        self.assertEqual(len(admits), 4)
        self.assertEqual(sum(event[3] == 'immediate' for event in admits), 2)
        self.assertEqual(self.ledger(), {})
        for index in range(4):
            self.assertTrue(os.path.exists(os.path.join(self.path, f'out{index}.o')))

    def test_learns_peaks_into_history(self):
        self.assertEqual(self.start(120, 1, 'learned.o').wait(), 0)
        with open(os.path.join(self.path, 'state', 'history.json')) as file:
            history = json.load(file)
        (key, entry), = history.items()
        self.assertTrue(key.endswith('/learned.o'))
        self.assertGreater(entry['peaks'][0], 100 << 20)

    def test_deadlock_kills_one_and_both_finish(self):
        first = self.start(330, 3, 'big1.o')
        time.sleep(0.3)
        second = self.start(330, 3, 'big2.o')
        self.assertEqual(first.wait(), 0)
        self.assertEqual(second.wait(), 0)
        names = [event[0] for event in self.events()]
        self.assertIn('pause', names)
        self.assertEqual(names.count('kill'), 1)
        self.assertEqual(names.count('finish'), 2)
        self.assertEqual(self.ledger(), {})

    def test_interrupt_reaches_the_compiler(self):
        process = self.start(100, 30, 'interrupted.o')
        self.wait_for_event('admit')
        time.sleep(0.5)
        process.send_signal(signal.SIGINT)
        self.assertEqual(process.wait(timeout=10), 128 + signal.SIGINT)
        self.assertEqual(self.ledger(), {})
        self.assertFalse(os.path.exists(os.path.join(self.path, 'interrupted.o')))

    def test_orphaned_paused_compiler_is_reaped(self):
        first = self.start(330, 4, 'orphan1.o')
        time.sleep(0.3)
        second = self.start(330, 4, 'orphan2.o')
        paused_key = self.wait_for_event('pause')[0][1]
        orphan_group = self.ledger()[paused_key]['process_group']
        os.kill(int(paused_key), signal.SIGKILL)
        survivor = second if int(paused_key) == first.pid else first
        self.assertEqual(survivor.wait(timeout=30), 0)
        (first if survivor is second else second).wait()
        self.assertIn('pruned', [event[0] for event in self.events()])
        with self.assertRaises(ProcessLookupError):
            os.killpg(orphan_group, 0)

    def test_off_passes_straight_through(self):
        self.assertEqual(self.start(10, 0.1, 'off.o', COMPILE_MEMORY_BUDGET='off').wait(), 0)
        self.assertFalse(os.path.exists(os.path.join(self.path, 'state')))

    def test_fails_open_when_state_is_unusable(self):
        blocker = os.path.join(self.path, 'not-a-directory')
        open(blocker, 'w').close()
        process = self.start(10, 0.1, 'open.o', COMPILE_MEMORY_DIR=blocker)
        self.assertEqual(process.wait(), 0)
        self.assertTrue(os.path.exists(os.path.join(self.path, 'open.o')))

    def test_passes_the_compiler_exit_code_through(self):
        command = [sys.executable, LAUNCHER, sys.executable, '-c', 'raise SystemExit(3)']
        self.assertEqual(subprocess.run(command, cwd=self.path, env=self.environment).returncode, 3)


if __name__ == '__main__':
    unittest.main()
