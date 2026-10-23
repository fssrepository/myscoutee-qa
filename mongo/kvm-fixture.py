#!/usr/bin/env python3
"""Run a manual-QA fixture in an existing owned KVM lab's DEMO Mongo database.

Project runtime JSON and private lab receipts are inputs. This package does not
import another checkout, initialize infrastructure or contain product fixtures.
"""
import argparse
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import subprocess
import sys


def read_runtime(path):
    config = json.loads(Path(path).read_text())
    state = PurePosixPath(config['lab']['stateDirectory'])
    if state.is_absolute() or not state.parts or any(
            not re.fullmatch(r'[a-zA-Z0-9_-]+', part) for part in state.parts):
        raise ValueError('Lab stateDirectory must be a safe relative project path')
    network = ipaddress.ip_network(config['lab']['networkCidr'])
    private_ranges = [ipaddress.ip_network(value)
                      for value in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')]
    if network.version != 4 or network.prefixlen != 24 or not any(
            network.subnet_of(value) for value in private_ranges):
        raise ValueError('Lab network must be a private IPv4 /24')
    for value in config['databases'].values():
        if not re.fullmatch(r'[a-z][a-z0-9_-]{0,62}', value):
            raise ValueError('Invalid lab database/replica-set name')
    if len({config['databases'][key] for key in ('real', 'demo', 'bootstrap')}) != 3:
        raise ValueError('REAL, DEMO and bootstrap databases must be distinct')
    return config


class KvmMongo:
    def __init__(self, config):
        self.config = config
        self.namespace = config['namespace']
        self.vm = (Path(os.environ.get('XDG_STATE_HOME', str(Path.home() / '.local/state')))
                   / config['lab']['stateDirectory'] / 'vm')
        self.ip = str(ipaddress.ip_network(config['lab']['networkCidr'])[10])

    def kube(self, *args):
        command = shlex.join(['sudo', 'k3s', 'kubectl', '--request-timeout=30s', *args])
        return subprocess.run([
            'ssh', '-F', '/dev/null', '-i', str(self.vm / 'id_ed25519'),
            '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes', '-o', 'ConnectTimeout=5',
            '-o', 'StrictHostKeyChecking=yes',
            '-o', 'UserKnownHostsFile=' + str(self.vm / 'known_hosts'),
            'myscoutee@' + self.ip, command],
            input=None, text=True, capture_output=True, check=True, timeout=120)

    def require_namespace(self):
        namespaces = json.loads(self.kube('get', 'namespaces', '-o', 'json').stdout)['items']
        current = next((item for item in namespaces
                        if item['metadata']['name'] == self.namespace), None)
        receipt = self.vm / 'workloads.json'
        inventory = json.loads(receipt.read_text()) if receipt.exists() else {}
        if current:
            if current['metadata']['uid'] != inventory.get('namespaceUid'):
                raise RuntimeError('Namespace identity missing or changed; refusing to adopt: '
                                   + self.namespace)
            if current['metadata'].get('labels', {}).get('app.kubernetes.io/part-of') != self.namespace:
                raise RuntimeError('Namespace ownership label changed.')
        elif inventory:
            raise RuntimeError('Recorded namespace disappeared; review state before recreating workloads.')
        else:
            raise RuntimeError('Owned lab namespace absent; no fixture was executed.')

    def run_fixture(self, database, script):
        if database != self.config['databases']['demo']:
            raise ValueError('KVM fixtures may target only the configured DEMO database.')
        if not script.strip() or len(script) > 1024 * 1024:
            raise ValueError('Expected a bounded fixture script on stdin.')
        self.require_namespace()
        current = self.kube('-n', self.namespace, 'get', 'statefulset/mongodb',
                            '--ignore-not-found', '-o', 'json')
        target = 'pod/mongodb-0' if current.stdout.strip() else 'deployment/mongodb'
        host = (['--host', self.config['databases']['replicaSet']
                 + '/mongodb:27017,mongodb-secondary:27017']
                if self.config.get('mongoReplicaSet') else [])
        prefix = "db.getSiblingDB('admin').auth(process.env.MONGO_INITDB_ROOT_USERNAME,process.env.MONGO_INITDB_ROOT_PASSWORD);\n"
        result = self.kube('-n', self.namespace, 'exec', target, '--', 'mongosh', *host,
                           '--quiet', '--eval', prefix
                           + 'db = db.getSiblingDB(' + json.dumps(database) + ');\n' + script)
        return json.loads(result.stdout.strip().splitlines()[-1])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-config', type=Path, required=True)
    parser.add_argument('--database')
    parser.add_argument('--print-demo-database', action='store_true')
    args = parser.parse_args()
    config = read_runtime(args.runtime_config)
    if args.print_demo_database:
        print(config['databases']['demo'])
        return
    if args.database != config['databases']['demo']:
        parser.error('KVM fixtures may target only the configured DEMO database.')
    script = sys.stdin.read(1024 * 1024 + 1)
    print(json.dumps(KvmMongo(config).run_fixture(args.database, script)))


if __name__ == '__main__':
    try:
        main()
    except subprocess.CalledProcessError as error:
        # Never include the complete fixture command or data in a transport error.
        raise SystemExit(error.stderr.strip() or f'KVM fixture failed (exit {error.returncode}).')
