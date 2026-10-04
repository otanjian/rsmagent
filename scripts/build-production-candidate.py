#!/usr/bin/env python3
"""Build the exact working tree with synthetic excluded markers, then inspect it.

Uses git's file inventory, not a recursive copy of the developer's instance.
No push or deployment. Requires an operational Linux Docker builder.
"""
import argparse
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def prepare_context(root, context, files, forbidden, marker):
    tree = hashlib.sha256()
    for raw in sorted(set(files)):
        if not raw:
            continue
        relative = Path(os.fsdecode(raw))
        source = root / relative
        if not source.is_file():
            continue
        target = context / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target, follow_symlinks=False)
        # Hash what Docker will receive, not a source file that an editor may
        # have changed immediately after the copy.
        payload = os.readlink(target).encode() if target.is_symlink() else target.read_bytes()
        tree.update(raw + b'\0' + hashlib.sha256(payload).digest())
    for name in forbidden:
        target = context / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink():
            target.unlink()  # Never follow a copied link while seeding markers.
        target.write_bytes(marker)
    return tree.hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--tag', required=True)
    parser.add_argument('--iidfile', type=Path, help='write the scanned image ID for subsequent checks')
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location('image_content', root / 'scripts/check-image-content.py')
    checks = importlib.util.module_from_spec(spec); spec.loader.exec_module(checks)
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
    files = subprocess.check_output(['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard'], cwd=root).split(b'\0')
    with tempfile.TemporaryDirectory(prefix='rsmagent-build-') as directory:
        context = Path(directory)
        digest = prepare_context(root, context, files, checks.FORBIDDEN, checks.MARKER)
        iidfile = context / '.candidate-image-id'
        subprocess.run(['docker', 'build', '--no-cache', '--build-arg', 'COW_BUILD_REVISION=' + revision,
                        '--build-arg', 'COW_SOURCE_TREE=' + digest,
                        '--iidfile', str(iidfile),
                        '-f', 'docker/Dockerfile.latest', '-t', args.tag, '.'], cwd=context, check=True)
        image_id = iidfile.read_text().strip()
    if not image_id.startswith('sha256:') or len(image_id) != 71 or any(char not in '0123456789abcdef' for char in image_id[7:]):
        raise RuntimeError('builder did not return a valid immutable image ID')
    subprocess.run([sys.executable, str(root / 'scripts/check-image-content.py'), image_id], check=True)
    if args.iidfile:
        args.iidfile.write_text(image_id + '\n')
    print(json.dumps({'revision': revision, 'source_tree_sha256': digest, 'image': args.tag, 'image_id': image_id}))


if __name__ == '__main__':
    main()
