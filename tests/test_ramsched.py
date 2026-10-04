import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from importlib.machinery import SourceFileLoader

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAUNCHER = os.path.join(ROOT, 'ramsched')
FAKE_COMPILER = os.path.join(ROOT, 'tests', 'fake_compiler.py')
ramsched = SourceFileLoader('ramsched', LAUNCHER).load_module()
GB = 1 << 30



class LauncherTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = self.directory.name
        self.environment = dict(
            os.environ,
            RAMSCHED_DIR=os.path.join(self.path, 'state'),
            RAMSCHED_HISTORY=os.path.join(self.path, 'state', 'history.json'),
            RAMSCHED_BUDGET='0.5',
            RAMSCHED_HEADROOM='off',
            RAMSCHED_DEFAULT='0.2',
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
            return json.load(file)['jobs']

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
        self.assertEqual(self.start(10, 0.1, 'off.o', RAMSCHED='off').wait(), 0)
        self.assertFalse(os.path.exists(os.path.join(self.path, 'state')))

    def test_fails_open_when_state_is_unusable(self):
        blocker = os.path.join(self.path, 'not-a-directory')
        open(blocker, 'w').close()
        process = self.start(10, 0.1, 'open.o', RAMSCHED_DIR=blocker)
        self.assertEqual(process.wait(), 0)
        self.assertTrue(os.path.exists(os.path.join(self.path, 'open.o')))

    def test_fails_open_when_state_directory_is_shared(self):
        state = os.path.join(self.path, 'state')
        os.makedirs(state, mode=0o777)
        os.chmod(state, 0o777)
        process = self.start(10, 0.1, 'shared.o')
        self.assertEqual(process.wait(), 0)
        self.assertTrue(os.path.exists(os.path.join(self.path, 'shared.o')))
        self.assertFalse(os.path.exists(os.path.join(state, 'ledger.json')))

    def test_never_kills_a_reused_process_group(self):
        bystander = subprocess.Popen(['sleep', '30'], process_group=0)
        try:
            state = os.path.join(self.path, 'state')
            os.makedirs(state, mode=0o700)
            stale = {'jobs': {
                str(bystander.pid): {
                    'state': 'paused', 'reserved': 1 << 20, 'want': 1 << 20, 'since': 0, 'admitted': 0,
                    'launcher_start': 1, 'process_group': bystander.pid, 'group_start': 1,
                },
            }}
            with open(os.path.join(state, 'ledger.json'), 'w') as file:
                json.dump(stale, file)
            self.assertEqual(self.start(10, 0.1, 'reuse.o').wait(), 0)
            self.assertIsNone(bystander.poll())
            self.assertIn('pruned', [event[0] for event in self.events()])
        finally:
            bystander.kill()
            bystander.wait()

    def headroom_environment(self, available, headroom, pause_below, kill_below):
        """Available memory is read from a file the test controls."""
        self.available_file = os.path.join(self.path, 'available')
        self.set_available(available)
        return dict(
            RAMSCHED_TEST_AVAILABLE=self.available_file,
            RAMSCHED_BUDGET='',
            RAMSCHED_HEADROOM=str(headroom),
            RAMSCHED_PAUSE_BELOW=str(pause_below) if pause_below else 'off',
            RAMSCHED_KILL_BELOW=str(kill_below) if kill_below else 'off',
        )

    def set_available(self, gigabytes):
        with open(self.available_file + '.new', 'w') as file:
            file.write(str(gigabytes))
        os.replace(self.available_file + '.new', self.available_file)

    def test_headroom_holds_back_what_does_not_fit(self):
        environment = self.headroom_environment(10, 9, None, None)
        processes = [self.start(50, 1.5, f'room{index}.o', RAMSCHED_DEFAULT='0.4', **environment)
                     for index in range(4)]
        for process in processes:
            self.assertEqual(process.wait(), 0)
        admits = [event for event in self.events() if event[0] == 'admit']
        self.assertEqual([event[3] for event in admits].count('immediate'), 2)

    def test_headroom_counts_reservations_not_yet_used(self):
        environment = self.headroom_environment(10, 9, None, None)
        first = self.start(10, 3, 'unused1.o', RAMSCHED_DEFAULT='0.8', **environment)
        self.wait_for_event('admit')
        second = self.start(10, 0.5, 'unused2.o', RAMSCHED_DEFAULT='0.8', **environment)
        time.sleep(1.5)
        self.assertEqual(len([event for event in self.events() if event[0] == 'admit']), 1)
        self.assertEqual(first.wait(), 0)
        self.assertEqual(second.wait(), 0)

    def test_pressure_pauses_all_but_the_oldest(self):
        environment = self.headroom_environment(10, 4, 3, None)
        first = self.start(50, 6, 'calm1.o', RAMSCHED_DEFAULT='0.2', **environment)
        time.sleep(0.5)
        second = self.start(50, 6, 'calm2.o', RAMSCHED_DEFAULT='0.2', **environment)
        time.sleep(1)
        self.set_available(2.5)
        self.wait_for_event('pause')
        time.sleep(1)
        self.assertEqual(self.ledger()[str(first.pid)]['state'], 'running')
        self.set_available(10)
        self.assertEqual(first.wait(), 0)
        self.assertEqual(second.wait(), 0)
        pauses = [event for event in self.events() if event[0] == 'pause']
        self.assertEqual([(event[1], event[-1]) for event in pauses], [(str(second.pid), 'pressure')])
        self.assertIn('resume', [event[0] for event in self.events()])

    def test_pressure_kills_the_newest_and_requeues_it(self):
        environment = self.headroom_environment(10, 4, 3, 2)
        first = self.start(50, 6, 'kill1.o', RAMSCHED_DEFAULT='0.2', **environment)
        time.sleep(0.5)
        second = self.start(50, 6, 'kill2.o', RAMSCHED_DEFAULT='0.2', **environment)
        time.sleep(0.5)
        third = self.start(50, 6, 'kill3.o', RAMSCHED_DEFAULT='0.2', **environment)
        time.sleep(1)
        self.set_available(1.5)
        self.wait_for_event('kill')
        self.set_available(10)
        for process in (first, second, third):
            self.assertEqual(process.wait(), 0)
        kills = [event for event in self.events() if event[0] == 'kill']
        self.assertEqual([(event[1], event[-1]) for event in kills], [(str(third.pid), 'pressure')])
        self.assertIn('requeue', [event[0] for event in self.events()])

    def test_kernel_pressure_pauses_and_critical_kills(self):
        environment = self.headroom_environment(10, 4, 3, 2)
        first = self.start(50, 6, 'kernel1.o', RAMSCHED_DEFAULT='0.2', **environment)
        time.sleep(0.5)
        second = self.start(50, 6, 'kernel2.o', RAMSCHED_DEFAULT='0.2', **environment)
        time.sleep(0.5)
        third = self.start(50, 6, 'kernel3.o', RAMSCHED_DEFAULT='0.2', **environment)
        time.sleep(1)
        pressure = self.available_file + '.pressure'
        with open(pressure, 'w') as file:
            file.write('pause')
        self.wait_for_event('pause')
        with open(pressure, 'w') as file:
            file.write('kill')
        self.wait_for_event('kill')
        os.remove(pressure)
        for process in (first, second, third):
            self.assertEqual(process.wait(), 0)
        events = self.events()
        self.assertNotIn(str(first.pid), [event[1] for event in events if event[0] in ('pause', 'kill')])
        self.assertEqual([event[1] for event in events if event[0] == 'kill'], [str(third.pid)])

    def test_a_lone_compile_is_never_paused_for_pressure(self):
        environment = self.headroom_environment(1, 4, 3, 2)
        self.assertEqual(self.start(50, 1.5, 'alone.o', RAMSCHED_DEFAULT='0.2', **environment).wait(), 0)
        self.assertFalse([event for event in self.events() if event[0] in ('pause', 'kill')])

    @unittest.skipUnless(os.environ.get('RAMSCHED_REAL_PRESSURE'), 'set RAMSCHED_REAL_PRESSURE=1')
    def test_real_memory_pressure_pauses(self):
        """A real program eating memory; depends on the machine, so not run in CI."""
        available = ramsched.available_memory() / GB
        environment = dict(RAMSCHED_BUDGET='', RAMSCHED_HEADROOM=str(available - 0.6),
                           RAMSCHED_PAUSE_BELOW=str(available - 1.0), RAMSCHED_KILL_BELOW='off')
        first = self.start(200, 8, 'real1.o', RAMSCHED_DEFAULT='0.25', **environment)
        time.sleep(0.5)
        second = self.start(200, 8, 'real2.o', RAMSCHED_DEFAULT='0.25', **environment)
        time.sleep(1.5)
        hog = subprocess.Popen([sys.executable, FAKE_COMPILER, '2500', '1.5', '4',
                                '-o', os.path.join(self.path, 'hog.o')])
        self.wait_for_event('pause')
        hog.wait()
        self.assertEqual(first.wait(), 0)
        self.assertEqual(second.wait(), 0)

    def test_passes_the_compiler_exit_code_through(self):
        command = [sys.executable, LAUNCHER, sys.executable, '-c', 'raise SystemExit(3)']
        self.assertEqual(subprocess.run(command, cwd=self.path, env=self.environment).returncode, 3)


if __name__ == '__main__':
    unittest.main()
