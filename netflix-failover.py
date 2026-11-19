#!/usr/bin/env python3
"""Hourly, conservative Netflix catalog failover for the existing Xray balancer."""
import argparse
import copy
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from netflix_probe import classify_page, aggregate_title_results, group_state, decision

CONFIG = Path('/opt/xray/config/config.json')
SETTINGS = Path('/etc/netflix-failover.json')
STATE = Path('/var/lib/netflix-failover')
RUNTIME = Path('/run/netflix-failover')
CONTAINER = 'xray'
LABEL = 'local.netflix-failover'


def run(args, timeout=60, check=True):
    p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if check and p.returncode:
        # Docker/Xray output can contain credentials. Keep it out of the journal.
        raise RuntimeError('command failed: ' + args[0] + ' (exit ' + str(p.returncode) + ')')
    return p


def digest(data):
    return hashlib.sha256(data).hexdigest()


def atomic(path, data, mode=0o600, owner=None):
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            os.fchmod(f.fileno(), mode)
            if owner:
                os.fchown(f.fileno(), *owner)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
        dfd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def write_json(path, data):
    atomic(path, (json.dumps(data, indent=2, ensure_ascii=False) + '\n').encode())


def inspect():
    return json.loads(run(['docker', 'inspect', CONTAINER]).stdout)[0]


def balancer(config):
    found = [b for b in config.get('routing', {}).get('balancers', []) if b.get('tag') == 'Netflix']
    if len(found) != 1:
        raise RuntimeError('expected exactly one Netflix balancer')
    return found[0]


def groups(config):
    b = balancer(config)
    tags = [o.get('tag') for o in config.get('outbounds', []) if o.get('tag')]
    selectors = b.get('selector', [])
    fallback = b.get('fallbackTag')
    if len(selectors) != 1 or selectors[0] not in tags or fallback not in tags:
        raise RuntimeError('Netflix needs one existing outbound tag as selector and an existing fallback')
    primary = selectors[0]
    a = [t for t in tags if t.startswith(primary)]
    # If promoted, fallback becomes a selector prefix too. Check every possible member.
    z = [t for t in tags if t.startswith(fallback)]
    if set(a) & set(z) or len(tags) != len(set(tags)):
        raise RuntimeError('ambiguous or overlapping Netflix outbound groups')
    return primary, fallback, a, z


def candidate(config):
    new = copy.deepcopy(config)
    b = balancer(new)
    b['selector'], b['fallbackTag'] = [b['fallbackTag']], b['selector'][0]
    return new


def mounts(di):
    # Only use the production asset mount, never mount Docker's socket.
    for m in di.get('Mounts', []):
        if m['Destination'] == '/usr/local/share/xray':
            return ['-v', m['Source'] + ':/usr/local/share/xray:ro']
    return []


def docker_base(di, path):
    return ['docker', 'run', '--network', 'host', '--read-only', '--cap-drop', 'ALL',
            '--security-opt', 'no-new-privileges', '--user', '0:0',
            '-v', str(path) + ':/etc/xray/config.json:ro'] + mounts(di)


class Probe:
    def __init__(self, config, tags, di, options):
        self.config, self.tags, self.di, self.options = config, tags, di, options
        self.name = 'netflix-probe-' + uuid.uuid4().hex[:12]
        self.path = RUNTIME / 'probe.json'
        self.ports = {}

    def __enter__(self):
        sockets = []
        try:
            for tag in self.tags:
                s = socket.socket()
                sockets.append(s)
                s.bind(('127.0.0.1', 0))
                self.ports[tag] = s.getsockname()[1]
        finally:
            for s in sockets:
                s.close()
        c = {'log': {'loglevel': 'none'}, 'inbounds': [],
             'outbounds': copy.deepcopy(self.config['outbounds']), 'routing': {'rules': []}}
        if self.config.get('dns'):
            c['dns'] = copy.deepcopy(self.config['dns'])
        for i, (tag, port) in enumerate(self.ports.items()):
            itag = 'nf-probe-' + str(i)
            c['inbounds'].append({'tag': itag, 'listen': '127.0.0.1', 'port': port,
                'protocol': 'socks', 'settings': {'auth': 'noauth', 'udp': False}})
            c['routing']['rules'].append({'type': 'field', 'inboundTag': [itag], 'outboundTag': tag})
        write_json(self.path, c)
        try:
            run(docker_base(self.di, self.path) + ['-d', '--name', self.name, '--label', LABEL + '=1',
                '--entrypoint', '/usr/local/bin/xray', self.di['Image'], 'run', '-c', '/etc/xray/config.json'])
            deadline = time.monotonic() + 12
            for port in self.ports.values():
                while True:
                    try:
                        with socket.create_connection(('127.0.0.1', port), .2):
                            break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise RuntimeError('isolated probe failed to start')
                        time.sleep(.2)
            running = json.loads(run(['docker', 'inspect', self.name]).stdout)[0]['State']['Running']
            if not running:
                raise RuntimeError('isolated probe exited during startup')
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *_):
        run(['docker', 'rm', '-f', self.name], check=False)
        self.path.unlink(missing_ok=True)

    def title(self, tag, title):
        tid, expected = str(title['id']), title['name']
        fd, filename = tempfile.mkstemp(prefix='page-', dir=RUNTIME)
        os.close(fd)
        try:
            p = run(['curl', '--disable', '-sS', '-L', '--max-redirs', '4', '--proto', '=https',
                '--proto-redir', '=https', '--connect-timeout', '8', '--max-time', '25',
                '--max-filesize', '5000000', '--noproxy', '',
                '--socks5-hostname', '127.0.0.1:' + str(self.ports[tag]),
                '-A', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0.0.0 Safari/537.36',
                '-H', 'Accept-Language: en-US,en;q=0.9', '-H', 'Cache-Control: no-cache',
                '-o', filename, '-w', '%{http_code}\n%{url_effective}',
                'https://www.netflix.com/title/' + tid], timeout=30, check=False)
            parts = p.stdout.splitlines()
            status = int(parts[0]) if parts and parts[0].isdigit() else 0
            url = parts[1] if len(parts) > 1 else ''
            body = Path(filename).read_text(errors='replace')
            result = classify_page(body, status, url, tid, expected) if p.returncode == 0 else 'unknown'
            return {'id': tid, 'state': result, 'http': status, 'curl': p.returncode,
                    'region': url.split('/')[3] if url.startswith('https://www.netflix.com/') else None}
        finally:
            Path(filename).unlink(missing_ok=True)

    def member(self, tag):
        control = self.title(tag, self.options['control'])
        licensed = [self.title(tag, t) for t in self.options['licensed_titles']]
        state = aggregate_title_results(control['state'], [t['state'] for t in licensed])
        return {'tag': tag, 'state': state, 'control': control, 'licensed': licensed}

    def group(self, tags):
        # Separate local ports keep all requests tied to an exact outbound tag.
        with ThreadPoolExecutor(max_workers=4) as pool:
            members = list(pool.map(self.member, tags))
        return {'state': group_state([m['state'] for m in members]), 'members': members}

    def confirmed(self, tags):
        rounds = []
        for i in range(2):
            if i:
                time.sleep(3)
            rounds.append(self.group(tags))
        states = [r['state'] for r in rounds]
        return {'state': states[0] if states[0] == states[1] else 'unknown', 'rounds': rounds}


def verify_running(before_started=None):
    time.sleep(3)
    first = inspect()
    if not first['State']['Running'] or (before_started and first['State']['StartedAt'] == before_started):
        raise RuntimeError('production container did not restart')
    time.sleep(3)
    second = inspect()
    if not second['State']['Running'] or first['State']['StartedAt'] != second['State']['StartedAt']:
        raise RuntimeError('production container is unstable')
    if second['State'].get('Health', {}).get('Status') == 'unhealthy':
        raise RuntimeError('production container is unhealthy')
    return second


def restart():
    started = inspect()['State']['StartedAt']
    run(['docker', 'restart', '--time', '10', CONTAINER], timeout=50)
    verify_running(started)
    # Distroless image: invoke Xray directly, never assume it contains a shell.
    run(['docker', 'exec', '--workdir', '/home/nonroot', CONTAINER, '/usr/local/bin/xray',
         'run', '-test', '-c', '/etc/xray/config.json'])


def verify_mounted_config(expected):
    p = subprocess.run(['docker', 'cp', CONTAINER + ':/etc/xray/config.json', '-'],
                       capture_output=True, timeout=30)
    if p.returncode:
        raise RuntimeError('cannot verify production configuration mount')
    with tarfile.open(fileobj=io.BytesIO(p.stdout)) as archive:
        files = [m for m in archive.getmembers() if m.isfile()]
        if len(files) != 1:
            raise RuntimeError('unexpected configuration archive')
        data = archive.extractfile(files[0]).read()
    if data != expected:
        raise RuntimeError('production container configuration differs from the candidate')


def rollback(tx):
    current = CONFIG.read_bytes()
    if digest(current) not in [tx['candidate_sha256'], tx['original_sha256']]:
        raise RuntimeError('pending transaction conflicts with an external configuration change')
    original = Path(tx['backup']).read_bytes()
    if digest(original) != tx['original_sha256']:
        raise RuntimeError('backup integrity check failed')
    atomic(CONFIG, original, tx['mode'], (tx['uid'], tx['gid']))
    restart()
    verify_mounted_config(original)
    (STATE / 'pending.json').unlink()


def apply(original, new, di, target_tags, options, require_unlocked=True):
    data = (json.dumps(new, indent=2, ensure_ascii=False) + '\n').encode()
    testpath = RUNTIME / 'candidate.json'
    write_json(testpath, new)
    try:
        run(docker_base(di, testpath) + ['--rm', '--entrypoint', '/usr/local/bin/xray', di['Image'],
            'run', '-test', '-c', '/etc/xray/config.json'])
    finally:
        testpath.unlink(missing_ok=True)
    if CONFIG.read_bytes() != original:
        raise RuntimeError('configuration changed during probing; retry next hour')
    stat = CONFIG.stat()
    backup = STATE / ('config-' + time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8] + '.json')
    atomic(backup, original)
    tx = {'backup': str(backup), 'original_sha256': digest(original), 'candidate_sha256': digest(data),
          'mode': stat.st_mode & 0o777, 'uid': stat.st_uid, 'gid': stat.st_gid}
    write_json(STATE / 'pending.json', tx)
    # Repeat the comparison after backup/journal I/O, immediately before replacing.
    if CONFIG.read_bytes() != original:
        (STATE / 'pending.json').unlink()
        raise RuntimeError('configuration changed before commit; retry next hour')
    try:
        atomic(CONFIG, data, tx['mode'], (tx['uid'], tx['gid']))
        restart()
        verify_mounted_config(data)
        if CONFIG.read_bytes() != data:
            raise RuntimeError('configuration changed during restart')
        # Probe the newly promoted exact outbounds again after the production restart.
        if require_unlocked:
            with Probe(new, target_tags, inspect(), options) as probe:
                if probe.group(target_tags)['state'] != 'unlocked':
                    raise RuntimeError('promoted group failed post-change check')
        (STATE / 'pending.json').unlink()
    except BaseException:
        rollback(tx)
        raise
    return str(backup)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--check-all', action='store_true', help='probe both groups without changing production')
    parser.add_argument('--initialize', action='store_true', help='initialize the user-requested ByteVirt/NiiHost pair')
    args = parser.parse_args()
    os.umask(0o077)
    for p in [STATE, RUNTIME]:
        p.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = (RUNTIME / 'lock').open('a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print('Another Netflix check is running; skipped.', flush=True)
        return
    report = {'time': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'action': 'keep'}
    try:
        pending = STATE / 'pending.json'
        if pending.exists():
            rollback(json.loads(pending.read_text()))
            report['recovered_interrupted_switch'] = True
        stale = run(['docker', 'ps', '-aq', '--filter', 'label=' + LABEL + '=1']).stdout.split()
        if stale:
            run(['docker', 'rm', '-f'] + stale)
        options = json.loads(SETTINGS.read_text())
        original = CONFIG.read_bytes()
        config = json.loads(original)
        if args.initialize:
            config = copy.deepcopy(config)
            b0 = balancer(config)
            b0['selector'] = [options['initial_primary']]
            b0['fallbackTag'] = options['alternate']
        primary, fallback, a, b = groups(config)
        if set([primary, fallback]) != set([options['initial_primary'], options['alternate']]) or len(a) != 1 or len(b) != 1:
            raise RuntimeError('Netflix is no longer the configured ByteVirt/NiiHost pair; no automatic edit')
        report.update(primary=primary, fallback=fallback, check_only=args.check_all, initializing=args.initialize)
        di = inspect()
        if not di['State']['Running'] or di['HostConfig']['NetworkMode'] != 'host':
            raise RuntimeError('production Xray must be running in host network mode')
        expected = [m for m in di.get('Mounts', [])
                    if m['Destination'] == '/etc/xray' and Path(m['Source']) == CONFIG.parent]
        if not expected:
            raise RuntimeError('unexpected production configuration mount')
        with Probe(config, list(dict.fromkeys(a + b)), di, options) as probe:
            report['primary_result'] = probe.confirmed(a)
            ps = report['primary_result']['state']
            if args.check_all or ps == 'restricted':
                report['fallback_result'] = probe.confirmed(b)
        bs = report.get('fallback_result', {}).get('state', 'not_tested')
        new = config
        if decision(ps, bs) == 'switch' and not args.check_all:
            new = candidate(config)
            report['new_primary'], report['new_fallback'] = fallback, primary
        if not args.check_all and new != json.loads(original):
            target_tags = groups(new)[2]
            selected_state = bs if new != config else ps
            report['backup'] = apply(original, new, di, target_tags, options, selected_state == 'unlocked')
            report['action'] = 'initialized' if args.initialize else 'switched'
        report['reason'] = ps + '/' + bs
    except Exception as e:
        report['action'] = 'error_keep_or_rollback'
        report['error'] = str(e) if isinstance(e, RuntimeError) else type(e).__name__
        write_json(STATE / 'latest.json', report)
        print(json.dumps(report, ensure_ascii=False), flush=True)
        return 1
    write_json(STATE / 'latest.json', report)
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0


if __name__ == '__main__':
    # Turn systemd's stop request into an exception so the probe gets cleaned up.
    def terminate(*_):
        raise RuntimeError('interrupted')
    signal.signal(signal.SIGTERM, terminate)
    sys.exit(main())
