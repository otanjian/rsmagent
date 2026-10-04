"""Build-time pinned upstream assets. Never imports SAP or starts a browser."""
import base64
import hashlib
import json
import shutil
import stat
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath


def download(spec, destination):
    if not spec['url'].startswith('https://'):
        raise ValueError('HTTPS artifact URL required')
    digest = hashlib.sha256() if 'sha256' in spec else hashlib.md5(usedforsecurity=False)
    size = 0
    with urllib.request.urlopen(spec['url'], timeout=60) as response, destination.open('wb') as output:
        while chunk := response.read(1024 * 1024):
            output.write(chunk)
            digest.update(chunk)
            size += len(chunk)
    expected = spec.get('sha256') or base64.b64decode(spec['md5_base64'], validate=True).hex()
    if digest.hexdigest() != expected or ('size' in spec and size != spec['size']):
        destination.unlink(missing_ok=True)
        raise ValueError('pinned artifact checksum or size changed')


def relative_path(name, prefix):
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts or not path.parts or path.parts[0] != prefix:
        raise ValueError('unsafe archive path')
    return Path(*path.parts[1:])


def unpack(archive, destination, prefix, *, zipped=False):
    destination.mkdir(parents=True, exist_ok=True)
    if zipped:
        with zipfile.ZipFile(archive) as source:
            for member in source.infolist():
                target = destination / relative_path(member.filename, prefix)
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                mode = member.external_attr >> 16
                if stat.S_ISLNK(mode):
                    raise ValueError('archive symlink is not supported')
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.open(member) as entry, target.open('wb') as output:
                    shutil.copyfileobj(entry, output)
                target.chmod((mode & 0o777) or 0o644)
    else:
        with tarfile.open(archive, 'r:gz') as source:
            for member in source:
                target = destination / relative_path(member.name, prefix)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                elif member.isfile():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with source.extractfile(member) as entry, target.open('wb') as output:
                        shutil.copyfileobj(entry, output)
                    target.chmod(member.mode & 0o777)
                elif member.issym():
                    # Upstream websockify ships two same-directory aliases.
                    # Accept only a single sibling name, never a parent path,
                    # absolute path, hard link or directory traversal chain.
                    link = PurePosixPath(member.linkname)
                    if link.is_absolute() or len(link.parts) != 1 or link.parts[0] in {'.', '..'}:
                        raise ValueError('unsafe archive link')
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.symlink_to(member.linkname)
                else:
                    raise ValueError('archive hard links and special files are not supported')


def main(architecture):
    versions = json.loads(Path(__file__).with_name('browser-versions.json').read_text())
    if architecture not in versions['chrome']['archives']:
        raise ValueError('only amd64 and arm64 browser images are pinned')
    with tempfile.TemporaryDirectory(prefix='sap-browser-build-') as temporary:
        for name, spec, zipped in [
            ('chrome', versions['chrome']['archives'][architecture], True),
            ('novnc', versions['novnc'], False),
            ('websockify', versions['websockify'], False),
        ]:
            archive = Path(temporary) / name
            download(spec, archive)
            unpack(archive, Path('/opt') / name, spec['prefix'], zipped=zipped)
    for binary in ['chrome', 'chrome_crashpad_handler']:
        (Path('/opt/chrome') / binary).chmod(0o755)


if __name__ == '__main__':
    main(sys.argv[1])
