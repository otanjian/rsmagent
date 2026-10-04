#!/usr/bin/env python3
"""Final-image smoke test; fails explicitly when Docker/OS prerequisites lack.

Use a disposable candidate image. No push, production credentials, or existing
instance volumes. Layer/content acceptance is deliberately separate from local
unit tests, which cannot prove Docker's build behavior.
"""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import urllib.request


def docker(*args):
    return subprocess.check_output(['docker', *args], text=True, timeout=60).strip()


def write_deployment_env(path, image_id, revision, source_tree):
    """Publish a nonsecret Compose image pin only after this image passes."""
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', image_id):
        raise RuntimeError('candidate image ID is not an immutable SHA256 ID')
    if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', revision):
        raise RuntimeError('candidate revision is not a Git commit ID')
    if not re.fullmatch(r'[0-9a-f]{64}', source_tree):
        raise RuntimeError('candidate source tree digest is invalid')
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix='.' + path.name + '.', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(f'RSMAGENT_IMAGE_ID={image_id[7:]}\n'
                         f'COW_BUILD_REVISION={revision}\nCOW_SOURCE_TREE={source_tree}\n')
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('image')
    parser.add_argument('--deployment-env', type=Path,
                        help='atomically write the checked image pin for Compose')
    args = parser.parse_args()
    if docker('info', '--format', '{{.OSType}}') != 'linux':
        raise RuntimeError('Linux Docker engine required')
    info = json.loads(docker('image', 'inspect', args.image))[0]
    if info.get('Os') != 'linux':
        raise RuntimeError('Linux final image required')
    user = info['Config'].get('User', '').partition(':')[0].strip()
    if not user or user == 'root' or user.isdecimal() and int(user) == 0:
        raise RuntimeError('final image is configured to run as root')
    # Resolve a mutable tag once. Scanning, startup and the result must all
    # describe this exact content-addressed image even if its tag is moved.
    image_id = info['Id']
    revision = info['Config'].get('Labels', {}).get('org.opencontainers.image.revision', '')
    if not revision or revision == 'unknown':
        raise RuntimeError('candidate revision label is missing')
    source_tree = info['Config'].get('Labels', {}).get('org.opencontainers.image.source-tree', '')
    if len(source_tree) != 64 or any(char not in '0123456789abcdef' for char in source_tree):
        raise RuntimeError('candidate source tree digest label is missing')
    subprocess.run([sys.executable, str(Path(__file__).with_name('check-image-content.py')), image_id], check=True)
    name = None
    # The image's declared volumes are initialized by its own non-root user.
    # Anonymous volumes are removed with the disposable container below.
    try:
        name = docker('create', '--init', '--tmpfs', '/home/agent/.cow:uid=10001,gid=10001,mode=700',
                      '--tmpfs', '/home/agent/cow:uid=10001,gid=10001,mode=700',
                      '-e', 'CHANNEL_TYPE=web', '-e', 'WEB_HOST=0.0.0.0',
                      '-e', 'COW_CREDENTIAL_MASTER_KEY=' + '1' * 64,
                      '-p', '127.0.0.1::9899', image_id)
        docker('start', name)
        if docker('exec', name, 'id', '-u') == '0':
            raise RuntimeError('final image actually runs as root')
        port = docker('port', name, '9899/tcp').rsplit(':', 1)[1]
        deadline = time.monotonic() + 60
        ready = False
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen('http://127.0.0.1:' + port + '/api/ready', timeout=2) as response:
                    ready = json.load(response).get('ready') is True
                if ready:
                    break
            except Exception:
                pass
            time.sleep(1)
        if not ready:
            raise RuntimeError('final image never became ready; inspect database/directory/native-isolation prerequisites')
        docker('exec', name, 'python', '-c',
               'from common.readiness import probe_execution; assert probe_execution(), "native isolation probe failed"')
        docker('exec', name, 'python', '-m', 'pip', 'check')
    finally:
        if name:
            docker('rm', '-f', '-v', name)
    if args.deployment_env:
        write_deployment_env(args.deployment_env, image_id, revision, source_tree)
    print(json.dumps({'image_id': image_id, 'repo_digests': info.get('RepoDigests', []),
                      'revision': revision, 'source_tree_sha256': source_tree,
                      'readiness': True, 'isolation_probe': True}))


if __name__ == '__main__':
    try:
        main()
    except (OSError, subprocess.SubprocessError, RuntimeError) as error:
        print('FAIL/NOT VERIFIED: ' + str(error))
        raise SystemExit(2)
