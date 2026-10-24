"""Mongo fixture isolation and lab ownership, without SSH or a running database."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('qa_kvm_fixture', Path(__file__).with_name('kvm-fixture.py'))
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


class KvmFixtureTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.environment = patch.dict(os.environ, {'XDG_STATE_HOME': self.directory.name})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.config = {
            'namespace': 'qa-fixture', 'mongoReplicaSet': True,
            'lab': {'stateDirectory': 'owned-lab/test', 'networkCidr': '192.168.173.0/24'},
            'databases': {'demo': 'fixture_demo', 'real': 'fixture_real',
                          'bootstrap': 'fixture_bootstrap', 'replicaSet': 'fixture'},
        }
        self.transport = fixture.KvmMongo(self.config)
        self.transport.vm.mkdir(parents=True)
        (self.transport.vm / 'workloads.json').write_text(json.dumps({'namespaceUid': 'owned-uid'}))
        self.namespace = {'metadata': {'name': 'qa-fixture', 'uid': 'owned-uid',
                                      'labels': {'app.kubernetes.io/part-of': 'qa-fixture'}}}
        self.stateful = True
        self.commands = []

    def ssh(self, command, **options):
        self.commands.append((command, options))
        kube = shlex.split(command[-1])
        if kube[4:] == ['get', 'namespaces', '-o', 'json']:
            output = json.dumps({'items': [self.namespace] if self.namespace else []})
        elif 'statefulset/mongodb' in kube:
            output = '{}' if self.stateful else ''
        elif 'exec' in kube:
            output = 'Mongo diagnostic line\n{"ready": true}\n'
        else:
            self.fail('Unexpected external operation: ' + repr(kube))
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr='')

    def run_fixture(self):
        with patch.object(fixture.subprocess, 'run', side_effect=self.ssh):
            return self.transport.run_fixture('fixture_demo', 'print(JSON.stringify({ready:true}));')

    def test_owned_replica_preserves_fixture_and_ssh_security(self):
        self.assertEqual(self.run_fixture(), {'ready': True})
        self.assertEqual(len(self.commands), 3)
        command, options = self.commands[-1]
        self.assertEqual(command[:-1], [
            'ssh', '-F', '/dev/null', '-i', str(self.transport.vm / 'id_ed25519'),
            '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes', '-o', 'ConnectTimeout=5',
            '-o', 'StrictHostKeyChecking=yes',
            '-o', 'UserKnownHostsFile=' + str(self.transport.vm / 'known_hosts'),
            'myscoutee@192.168.173.10'])
        self.assertEqual(shlex.split(command[-1]), [
            'sudo', 'k3s', 'kubectl', '--request-timeout=30s', '-n', 'qa-fixture',
            'exec', 'pod/mongodb-0', '--', 'mongosh', '--host',
            'fixture/mongodb:27017,mongodb-secondary:27017', '--quiet', '--eval',
            "db.getSiblingDB('admin').auth(process.env.MONGO_INITDB_ROOT_USERNAME,process.env.MONGO_INITDB_ROOT_PASSWORD);\n"
            'db = db.getSiblingDB("fixture_demo");\nprint(JSON.stringify({ready:true}));'])
        self.assertEqual(options, dict(input=None, text=True, capture_output=True, check=True, timeout=120))

    def test_single_member_deployment_keeps_existing_target(self):
        self.stateful = False
        self.config['mongoReplicaSet'] = False
        self.run_fixture()
        args = shlex.split(self.commands[-1][0][-1])
        self.assertIn('deployment/mongodb', args)
        self.assertNotIn('--host', args)

    def test_wrong_database_and_unbounded_input_never_contact_lab(self):
        with patch.object(fixture.subprocess, 'run') as run:
            for database, script in [('fixture_real', 'print(1)'), ('e2e_db', 'print(1)'),
                                     ('fixture_demo', ''), ('fixture_demo', 'x' * (1024 * 1024 + 1))]:
                with self.subTest(database=database, size=len(script)), self.assertRaises(ValueError):
                    self.transport.run_fixture(database, script)
            run.assert_not_called()

    def test_changed_uid_or_label_never_execute_fixture(self):
        for field in ['uid', 'labels']:
            with self.subTest(field=field):
                original = copy.deepcopy(self.namespace)
                self.namespace['metadata'][field] = 'foreign' if field == 'uid' else {}
                with self.assertRaises(RuntimeError):
                    self.run_fixture()
                self.assertEqual(len(self.commands), 1)
                self.commands.clear()
                self.namespace = original

    def test_missing_receipt_never_adopts_existing_namespace(self):
        (self.transport.vm / 'workloads.json').unlink()
        with self.assertRaisesRegex(RuntimeError, 'identity missing or changed'):
            self.run_fixture()
        self.assertEqual(len(self.commands), 1)

    def test_missing_namespace_never_creates_workloads(self):
        self.namespace = None
        for recorded in [True, False]:
            with self.subTest(recorded=recorded):
                if not recorded:
                    (self.transport.vm / 'workloads.json').unlink()
                with self.assertRaises(RuntimeError):
                    self.run_fixture()
                self.assertEqual(len(self.commands), 1)
                self.commands.clear()

    def test_runtime_config_rejects_unsafe_state_and_database_alias(self):
        path = Path(self.directory.name) / 'runtime.json'
        path.write_text(json.dumps(self.config))
        self.assertEqual(fixture.read_runtime(path), self.config)
        for state in ['../foreign', '/absolute']:
            value = copy.deepcopy(self.config)
            value['lab']['stateDirectory'] = state
            path.write_text(json.dumps(value))
            with self.assertRaises(ValueError):
                fixture.read_runtime(path)
        value = copy.deepcopy(self.config)
        value['databases']['demo'] = value['databases']['real']
        path.write_text(json.dumps(value))
        with self.assertRaises(ValueError):
            fixture.read_runtime(path)


if __name__ == '__main__':
    unittest.main()
