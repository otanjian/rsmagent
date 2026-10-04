#!/usr/bin/env python3
"""Inspect every saved image layer and config, including deleted intermediate data."""
import argparse
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile

MARKER = b'RSMAGENT_SYNTHETIC_BUILD_SECRET_20261002'
FORBIDDEN = ['config.json', 'identity.db', '.env', 'run.log', '.preview_secret',
             'backups/instance.zip', 'agent/.env', 'skills/probe/config.json',
             'skills/probe/private.key', 'docker/agent-mail-data/secrets.json']


def scan(path):
    with tarfile.open(path) as archive:
        manifest = json.load(archive.extractfile('manifest.json'))
        for item in manifest:
            config = archive.extractfile(item['Config']).read()
            if MARKER in config:
                raise ValueError('synthetic secret in image metadata')
            for layer in item['Layers']:
                with tarfile.open(fileobj=archive.extractfile(layer), mode='r|*') as contents:
                    for member in contents:
                        name = member.name.removeprefix('./')
                        if name in {'app/' + file for file in FORBIDDEN}:
                            raise ValueError('instance path in an image layer: ' + name)
                        if not member.isfile():
                            continue
                        stream = contents.extractfile(member)
                        tail = b''
                        while chunk := stream.read(1024 * 1024):
                            data = tail + chunk
                            if MARKER in data:
                                raise ValueError('synthetic secret in an image layer')
                            tail = data[-len(MARKER):]
    return len(manifest)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('image')
    args = parser.parse_args()
    image_id = json.loads(subprocess.check_output(
        ['docker', 'image', 'inspect', args.image], text=True, timeout=60))[0]['Id']
    with tempfile.TemporaryDirectory(prefix='rsmagent-layers-') as directory:
        path = Path(directory) / 'image.tar'
        subprocess.run(['docker', 'save', '-o', str(path), image_id], check=True)
        scan(path)
        history = subprocess.check_output(['docker', 'history', '--no-trunc', image_id], timeout=60)
        if MARKER in history:
            raise ValueError('synthetic secret in image history')
    print('PASS: all saved layers, metadata and history scanned')


if __name__ == '__main__':
    main()
