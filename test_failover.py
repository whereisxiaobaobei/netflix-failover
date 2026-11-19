"""Offline transaction tests. No Docker, networking, or production file access."""
import copy
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
spec = importlib.util.spec_from_file_location('netflix_failover', HERE / 'netflix-failover.py')
failover = importlib.util.module_from_spec(spec)
spec.loader.exec_module(failover)


def config():
    return {
        'outbounds': [
            {'tag': 'direct', 'protocol': 'freedom'},
            {'tag': 'direct-ipv6', 'protocol': 'freedom'},
            {'tag': 'ByteVirt_TW_HiNet_NAT_Proxy', 'protocol': 'shadowsocks'},
            {'tag': 'NiiHost_TW_HiNet_NAT_Proxy', 'protocol': 'shadowsocks'},
        ],
        'routing': {
            'balancers': [
                {'tag': 'Netflix', 'selector': ['direct'],
                 'fallbackTag': 'ByteVirt_TW_HiNet_NAT_Proxy', 'strategy': {'type': 'leastPing'}},
                {'tag': 'Twitter', 'selector': ['ByteVirt_TW_HiNet_NAT_Proxy'],
                 'fallbackTag': 'NiiHost_TW_HiNet_NAT_Proxy', 'strategy': {'type': 'leastPing'}},
            ],
            'rules': [{'type': 'field', 'domain': ['geosite:netflix'], 'balancerTag': 'Netflix'}],
        },
    }


class ConfigurationTests(unittest.TestCase):
    def test_exchange_twice_preserves_exact_configuration(self):
        c = config()
        before = copy.deepcopy(c)
        swapped = failover.candidate(c)
        self.assertEqual(c, before)
        self.assertEqual(failover.candidate(swapped), before)
        self.assertEqual(swapped['routing']['balancers'][0]['selector'], ['ByteVirt_TW_HiNet_NAT_Proxy'])
        self.assertEqual(swapped['routing']['balancers'][0]['fallbackTag'], 'direct')
        expected = copy.deepcopy(c)
        expected['routing']['balancers'][0] = swapped['routing']['balancers'][0]
        self.assertEqual(swapped, expected)

    def test_prefix_expansion_includes_all_potential_members(self):
        primary, fallback, a, b = failover.groups(config())
        self.assertEqual(primary, 'direct')
        self.assertEqual(a, ['direct', 'direct-ipv6'])
        self.assertEqual(b, [fallback])
        swapped = failover.candidate(config())
        _, _, _, restored = failover.groups(swapped)
        self.assertEqual(restored, ['direct', 'direct-ipv6'])

    def test_prefix_overlap_is_rejected(self):
        c = config()
        c['routing']['balancers'][0]['fallbackTag'] = 'direct-ipv6'
        with self.assertRaises(RuntimeError):
            failover.groups(c)

    def test_duplicate_tags_are_rejected(self):
        c = config()
        c['outbounds'].append(copy.deepcopy(c['outbounds'][0]))
        with self.assertRaises(RuntimeError):
            failover.groups(c)


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='netflix-failover-test-')
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.cfg = root / 'config.json'
        self.state, self.runtime = root / 'state', root / 'run'
        self.state.mkdir()
        self.runtime.mkdir()
        self.original = (json.dumps(config()) + '\n').encode()
        self.cfg.write_bytes(self.original)
        self.di = {'Image': 'local-image-for-test', 'Mounts': []}
        paths = patch.multiple(failover, CONFIG=self.cfg, STATE=self.state, RUNTIME=self.runtime)
        paths.start()
        self.addCleanup(paths.stop)
        self.run = patch.object(failover, 'run').start()
        self.restart = patch.object(failover, 'restart').start()
        self.verify_mounted = patch.object(failover, 'verify_mounted_config').start()
        self.inspect = patch.object(failover, 'inspect', return_value=self.di).start()
        self.probe = patch.object(failover, 'Probe').start()
        self.addCleanup(patch.stopall)
        self.probe.return_value.__enter__.return_value.group.return_value = {'state': 'unlocked'}

    def apply(self):
        return failover.apply(self.original, failover.candidate(config()), self.di,
                              ['ByteVirt_TW_HiNet_NAT_Proxy'], {})

    def test_success_keeps_backup_and_removes_pending_journal(self):
        backup = self.apply()
        self.assertEqual(Path(backup).read_bytes(), self.original)
        self.assertEqual(json.loads(self.cfg.read_bytes()), failover.candidate(config()))
        self.assertFalse((self.state / 'pending.json').exists())
        self.restart.assert_called_once()
        self.verify_mounted.assert_called_once_with(self.cfg.read_bytes())
        self.assertFalse((self.runtime / 'candidate.json').exists())

    def test_validation_failure_never_writes_production(self):
        self.run.side_effect = RuntimeError('invalid candidate')
        with self.assertRaisesRegex(RuntimeError, 'invalid candidate'):
            self.apply()
        self.assertEqual(self.cfg.read_bytes(), self.original)
        self.assertEqual(list(self.state.iterdir()), [])
        self.restart.assert_not_called()
        self.assertFalse((self.runtime / 'candidate.json').exists())

    def test_existing_concurrent_change_is_preserved(self):
        changed = self.original + b' '
        self.cfg.write_bytes(changed)
        with self.assertRaisesRegex(RuntimeError, 'configuration changed'):
            self.apply()
        self.assertEqual(self.cfg.read_bytes(), changed)
        self.assertEqual(list(self.state.iterdir()), [])
        self.restart.assert_not_called()

    def test_change_during_pending_journal_write_is_preserved(self):
        real_write_json = failover.write_json
        changed = self.original + b'  '

        def write_and_change(path, data):
            real_write_json(path, data)
            if path.name == 'pending.json':
                self.cfg.write_bytes(changed)

        with patch.object(failover, 'write_json', side_effect=write_and_change):
            with self.assertRaisesRegex(RuntimeError, 'configuration changed'):
                self.apply()
        self.assertEqual(self.cfg.read_bytes(), changed)
        self.assertFalse((self.state / 'pending.json').exists())
        self.restart.assert_not_called()

    def test_restart_failure_restores_original_configuration(self):
        self.restart.side_effect = [RuntimeError('restart failed'), None]
        with self.assertRaisesRegex(RuntimeError, 'restart failed'):
            self.apply()
        self.assertEqual(self.cfg.read_bytes(), self.original)
        self.assertEqual(self.restart.call_count, 2)
        self.assertFalse((self.state / 'pending.json').exists())
        self.assertEqual(len(list(self.state.glob('config-*.json'))), 1)

    def test_post_change_unknown_rolls_back(self):
        self.probe.return_value.__enter__.return_value.group.return_value = {'state': 'unknown'}
        with self.assertRaisesRegex(RuntimeError, 'post-change check'):
            self.apply()
        self.assertEqual(self.cfg.read_bytes(), self.original)
        self.assertEqual(self.restart.call_count, 2)
        self.assertFalse((self.state / 'pending.json').exists())

    def test_rollback_refuses_external_change(self):
        original_backup = self.state / 'backup.json'
        original_backup.write_bytes(self.original)
        candidate_bytes = json.dumps(failover.candidate(config())).encode()
        tx = {'backup': str(original_backup), 'original_sha256': failover.digest(self.original),
              'candidate_sha256': failover.digest(candidate_bytes), 'mode': 0o600,
              'uid': self.cfg.stat().st_uid, 'gid': self.cfg.stat().st_gid}
        changed = self.original + b'   '
        self.cfg.write_bytes(changed)
        with self.assertRaisesRegex(RuntimeError, 'external configuration change'):
            failover.rollback(tx)
        self.assertEqual(self.cfg.read_bytes(), changed)
        self.restart.assert_not_called()

    def test_rollback_refuses_damaged_backup(self):
        original_backup = self.state / 'backup.json'
        original_backup.write_bytes(b'damaged backup')
        tx = {'backup': str(original_backup), 'original_sha256': failover.digest(self.original),
              'candidate_sha256': 'unused', 'mode': 0o600,
              'uid': self.cfg.stat().st_uid, 'gid': self.cfg.stat().st_gid}
        with self.assertRaisesRegex(RuntimeError, 'backup integrity'):
            failover.rollback(tx)
        self.assertEqual(self.cfg.read_bytes(), self.original)
        self.restart.assert_not_called()


class MainTests(unittest.TestCase):
    BYTE = 'ByteVirt_TW_HiNet_NAT_Proxy'
    NII = 'NiiHost_TW_HiNet_NAT_Proxy'

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='netflix-main-test-')
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.cfg, self.settings = root / 'config.json', root / 'settings.json'
        self.state, self.runtime = root / 'state', root / 'run'
        self.settings.write_text(json.dumps({'initial_primary': self.BYTE, 'alternate': self.NII}))
        self.cfg.write_text(json.dumps(config()))
        paths = patch.multiple(failover, CONFIG=self.cfg, SETTINGS=self.settings,
                               STATE=self.state, RUNTIME=self.runtime)
        paths.start()
        self.addCleanup(paths.stop)
        self.di = {'Image': 'offline-test-image', 'State': {'Running': True},
                   'HostConfig': {'NetworkMode': 'host'},
                   'Mounts': [{'Destination': '/etc/xray', 'Source': str(self.cfg.parent)}]}
        self.run = patch.object(failover, 'run', return_value=SimpleNamespace(stdout='')).start()
        self.inspect = patch.object(failover, 'inspect', return_value=self.di).start()
        self.probe_class = patch.object(failover, 'Probe').start()
        self.probe = self.probe_class.return_value.__enter__.return_value
        self.apply_mock = patch.object(failover, 'apply', return_value='/offline/backup.json').start()
        self.addCleanup(patch.stopall)

    def main(self, *args):
        with patch.object(sys, 'argv', ['netflix-failover.py', *args]):
            with contextlib.redirect_stdout(io.StringIO()):
                result = failover.main()
        return result, json.loads((self.state / 'latest.json').read_text())

    def set_pair(self, primary, fallback):
        c = config()
        b = c['routing']['balancers'][0]
        b['selector'], b['fallbackTag'] = [primary], fallback
        self.cfg.write_text(json.dumps(c))

    def assert_applied_pair(self, primary, fallback):
        self.apply_mock.assert_called_once()
        candidate = self.apply_mock.call_args.args[1]
        b = failover.balancer(candidate)
        self.assertEqual(b['selector'], [primary])
        self.assertEqual(b['fallbackTag'], fallback)
        self.assertEqual(self.apply_mock.call_args.args[3], [primary])
        self.assertTrue(self.apply_mock.call_args.args[5])
        self.assertEqual(candidate['routing']['balancers'][1], config()['routing']['balancers'][1])

    def test_initialize_promotes_unlocked_niihost_when_bytevrt_restricted(self):
        self.probe.confirmed.side_effect = [{'state': 'restricted'}, {'state': 'unlocked'}]
        result, report = self.main('--initialize')
        self.assertEqual(result, 0)
        self.assertEqual(report['action'], 'initialized')
        self.assertEqual(report['new_primary'], self.NII)
        self.assertEqual(report['new_fallback'], self.BYTE)
        self.assert_applied_pair(self.NII, self.BYTE)
        self.assertEqual(self.probe.confirmed.call_args_list,
                         [unittest.mock.call([self.BYTE]), unittest.mock.call([self.NII])])

    def test_initialize_keeps_bytevirt_as_requested_when_it_unlocks(self):
        self.probe.confirmed.return_value = {'state': 'unlocked'}
        result, report = self.main('--initialize')
        self.assertEqual(result, 0)
        self.assertEqual(report['action'], 'initialized')
        self.assert_applied_pair(self.BYTE, self.NII)
        self.probe.confirmed.assert_called_once_with([self.BYTE])

    def test_initialize_check_all_never_applies_configuration(self):
        before = self.cfg.read_bytes()
        self.probe.confirmed.side_effect = [{'state': 'restricted'}, {'state': 'unlocked'}]
        result, report = self.main('--initialize', '--check-all')
        self.assertEqual(result, 0)
        self.assertTrue(report['check_only'])
        self.assertEqual(report['action'], 'keep')
        self.apply_mock.assert_not_called()
        self.assertEqual(self.cfg.read_bytes(), before)
        self.assertEqual(self.probe.confirmed.call_count, 2)

    def test_normal_niihost_unlock_does_not_probe_bytevirt_or_apply(self):
        self.set_pair(self.NII, self.BYTE)
        before = self.cfg.read_bytes()
        self.probe.confirmed.return_value = {'state': 'unlocked'}
        result, report = self.main()
        self.assertEqual(result, 0)
        self.assertEqual(report['primary'], self.NII)
        self.assertEqual(report['action'], 'keep')
        self.probe.confirmed.assert_called_once_with([self.NII])
        self.apply_mock.assert_not_called()
        self.assertEqual(self.cfg.read_bytes(), before)
        self.assertNotIn('fallback_result', report)

    def test_normal_external_pair_change_stops_without_probing_or_applying(self):
        before = self.cfg.read_bytes()
        result, report = self.main()
        self.assertEqual(result, 1)
        self.assertEqual(report['action'], 'error_keep_or_rollback')
        self.assertIn('no automatic edit', report['error'])
        self.probe_class.assert_not_called()
        self.apply_mock.assert_not_called()
        self.assertEqual(self.cfg.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
