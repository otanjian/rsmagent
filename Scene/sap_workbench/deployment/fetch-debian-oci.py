#!/usr/bin/env python3
"""Fetch the pinned official Debian OCI base; never invoke Docker or extract rootfs."""
import argparse
import base64
import binascii
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import ssl
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import zlib


VERSIONS = Path(__file__).with_name('browser-versions.json')
COMMITS = {
    'amd64': '8f962b15d7884a90e17876a9303cbac909d119aa',
    'arm64': 'ca011a8b1c3b259e4cbbf83bf6841f1fd5f497c1',
}
INDEX_TYPE = 'application/vnd.oci.image.index.v1+json'
MANIFEST_TYPE = 'application/vnd.oci.image.manifest.v1+json'
CONFIG_TYPE = 'application/vnd.oci.image.config.v1+json'
LAYER_TYPE = 'application/vnd.oci.image.layer.v1.tar+gzip'
SHA256 = re.compile(r'sha256:[0-9a-f]{64}\Z')
LOCK = re.compile(r'debian:bookworm-20260918-slim@(sha256:[0-9a-f]{64})\Z')
METADATA_LIMIT = 128 * 1024
COMPRESSED_LIMIT = 40 * 1024 * 1024
UNPACKED_LIMIT = 150 * 1024 * 1024
CHUNK = 64 * 1024
SOCKET_TIMEOUT = 15
TOTAL_TIMEOUT = 360


class FetchError(ValueError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, url):
        raise FetchError('official artifact redirect refused')


def _opener():
    return urllib.request.build_opener(
        NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context()))


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise FetchError('official artifact time budget exceeded')
    return remaining


def _json(data):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise FetchError('duplicate metadata key')
            result[key] = value
        return result

    def constant(_):
        raise FetchError('invalid metadata number')

    try:
        value = json.loads(data.decode('utf-8'), object_pairs_hook=pairs,
                           parse_constant=constant)
    except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError) as error:
        raise FetchError('invalid official metadata JSON') from error
    if not isinstance(value, dict):
        raise FetchError('invalid official metadata object')
    return value


def _locks():
    with VERSIONS.open('rb') as source:
        data = source.read(METADATA_LIMIT + 1)
    if len(data) > METADATA_LIMIT:
        raise FetchError('versions metadata too large')
    images = _json(data).get('base_images')
    if not isinstance(images, dict) or set(images) != set(COMMITS):
        raise FetchError('unsupported base image lock')
    result = {}
    for arch, image in images.items():
        match = LOCK.fullmatch(image) if isinstance(image, str) else None
        if match is None:
            raise FetchError('unsupported base image lock')
        result[arch] = match.group(1)
    return result


def _url(arch, resource):
    # Only these immutable official artifact paths are reachable from this CLI.
    if arch not in COMMITS or resource not in {'index.json', 'blobs/rootfs.tar.gz'}:
        raise FetchError('unsupported official artifact')
    return ('https://raw.githubusercontent.com/debuerreotype/docker-debian-artifacts/'
            + COMMITS[arch] + '/bookworm/slim/oci/' + resource)


def _download(opener, url, target, *, limit, deadline, expected_size=None):
    request = urllib.request.Request(url, headers={
        'Accept-Encoding': 'identity', 'User-Agent': 'sap-workbench-debian-oci/1'})
    digest, total = hashlib.sha256(), 0
    with opener.open(request, timeout=min(SOCKET_TIMEOUT, _remaining(deadline))) as response:
        if response.getcode() != 200 or response.geturl() != url:
            raise FetchError('official artifact response refused')
        length = response.headers.get('Content-Length')
        if length is not None:
            if not re.fullmatch(r'[0-9]{1,10}', length):
                raise FetchError('invalid official artifact length')
            length = int(length)
            if length > limit or expected_size is not None and length != expected_size:
                raise FetchError('official artifact size mismatch')
        while True:
            _remaining(deadline)
            # read1 returns after one underlying read, permitting total-deadline
            # checks even if a peer supplies less than a full chunk at a time.
            chunk = response.read1(min(CHUNK, limit - total + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > limit or expected_size is not None and total > expected_size:
                raise FetchError('official artifact exceeds size budget')
            target.write(chunk)
            digest.update(chunk)
        _remaining(deadline)
    if ((length is not None and total != length)
            or (expected_size is not None and total != expected_size)):
        raise FetchError('official artifact truncated')
    return 'sha256:' + digest.hexdigest(), total


def _descriptor(value, media_type, limit):
    if (not isinstance(value, dict) or value.get('mediaType') != media_type
            or not isinstance(value.get('digest'), str)
            or not SHA256.fullmatch(value['digest'])
            or type(value.get('size')) is not int or not 0 < value['size'] <= limit):
        raise FetchError('invalid official artifact descriptor')
    return value


def _embedded(value):
    data = value.get('data')
    if not isinstance(data, str) or len(data) > 4 * ((METADATA_LIMIT + 2) // 3):
        raise FetchError('missing embedded official artifact')
    try:
        raw = base64.b64decode(data, validate=True)
    except (ValueError, binascii.Error) as error:
        raise FetchError('invalid embedded official artifact') from error
    if (len(raw) != value['size']
            or 'sha256:' + hashlib.sha256(raw).hexdigest() != value['digest']):
        raise FetchError('embedded official artifact digest mismatch')
    return raw


def _platform(value, arch):
    expected = {'os': 'linux', 'architecture': arch}
    if arch == 'arm64':
        expected['variant'] = 'v8'
    return value == expected


def _metadata(data, arch, locked_digest):
    index = _json(data)
    if (type(index.get('schemaVersion')) is not int or index['schemaVersion'] != 2
            or index.get('mediaType') != INDEX_TYPE
            or not isinstance(index.get('manifests'), list) or len(index['manifests']) != 1):
        raise FetchError('unsupported official OCI index')
    descriptor = _descriptor(index['manifests'][0], MANIFEST_TYPE, METADATA_LIMIT)
    if descriptor['digest'] != locked_digest or not _platform(descriptor.get('platform'), arch):
        raise FetchError('official base image lock or platform mismatch')
    manifest_raw = _embedded(descriptor)
    manifest = _json(manifest_raw)
    if (type(manifest.get('schemaVersion')) is not int or manifest['schemaVersion'] != 2
            or manifest.get('mediaType') != MANIFEST_TYPE
            or not isinstance(manifest.get('layers'), list) or len(manifest['layers']) != 1):
        raise FetchError('unsupported official OCI manifest')
    config_descriptor = _descriptor(manifest.get('config'), CONFIG_TYPE, METADATA_LIMIT)
    config_raw = _embedded(config_descriptor)
    config = _json(config_raw)
    platform = {key: config[key] for key in ('os', 'architecture', 'variant') if key in config}
    rootfs = config.get('rootfs')
    if (not _platform(platform, arch) or not isinstance(rootfs, dict)
            or rootfs.get('type') != 'layers' or not isinstance(rootfs.get('diff_ids'), list)
            or len(rootfs['diff_ids']) != 1 or not isinstance(rootfs['diff_ids'][0], str)
            or not SHA256.fullmatch(rootfs['diff_ids'][0])):
        raise FetchError('official config platform or rootfs mismatch')
    layer = _descriptor(manifest['layers'][0], LAYER_TYPE, COMPRESSED_LIMIT)
    return descriptor, manifest_raw, config_descriptor, config_raw, layer, rootfs['diff_ids'][0]


def _diff_id(layer_path, expected, deadline):
    digest, total = hashlib.sha256(), 0
    try:
        with gzip.open(layer_path, 'rb') as source:
            while True:
                _remaining(deadline)
                chunk = source.read(min(CHUNK, UNPACKED_LIMIT - total + 1))
                if not chunk:
                    break
                total += len(chunk)
                if total > UNPACKED_LIMIT:
                    raise FetchError('unpacked rootfs exceeds size budget')
                digest.update(chunk)
    except (EOFError, gzip.BadGzipFile, OSError, zlib.error) as error:
        raise FetchError('invalid or truncated gzip rootfs') from error
    _remaining(deadline)
    if 'sha256:' + digest.hexdigest() != expected:
        raise FetchError('unpacked rootfs digest mismatch')


def _archive(path, descriptor, manifest, config_descriptor, config, layer, layer_path):
    reference = 'docker.io/library/debian@' + descriptor['digest']
    output_descriptor = {key: descriptor[key] for key in
                         ('mediaType', 'digest', 'size', 'platform', 'data')}
    output_descriptor['annotations'] = {
        'io.containerd.image.name': reference, 'org.opencontainers.image.ref.name': reference}
    index = json.dumps({'schemaVersion': 2, 'mediaType': INDEX_TYPE,
                        'manifests': [output_descriptor]}, separators=(',', ':')).encode() + b'\n'
    with tarfile.open(path, 'x', format=tarfile.USTAR_FORMAT) as archive:
        for name, raw in [('oci-layout', b'{"imageLayoutVersion":"1.0.0"}\n'),
                          ('index.json', index),
                          ('blobs/sha256/' + descriptor['digest'][7:], manifest),
                          ('blobs/sha256/' + config_descriptor['digest'][7:], config)]:
            member = tarfile.TarInfo(name)
            member.size, member.mode = len(raw), 0o644
            archive.addfile(member, io.BytesIO(raw))
        member = tarfile.TarInfo('blobs/sha256/' + layer['digest'][7:])
        member.size, member.mode = layer['size'], 0o644
        with layer_path.open('rb') as source:
            archive.addfile(member, source)
    return reference


def fetch(arch, output):
    if arch not in {*COMMITS, 'all'}:
        raise FetchError('unsupported architecture')
    locks, output = _locks(), Path(output).absolute()
    # Reserve a new directory before any network call, including dangling links.
    output.mkdir(mode=0o700, exist_ok=False)
    published = []
    try:
        deadline, opener = time.monotonic() + TOTAL_TIMEOUT, _opener()
        with tempfile.TemporaryDirectory(prefix='.debian-oci-', dir=output) as temporary:
            stage, results = Path(temporary), []
            for architecture in COMMITS if arch == 'all' else (arch,):
                metadata = io.BytesIO()
                _download(opener, _url(architecture, 'index.json'), metadata,
                          limit=METADATA_LIMIT, deadline=deadline)
                descriptor, manifest, config_descriptor, config, layer, diff_id = _metadata(
                    metadata.getvalue(), architecture, locks[architecture])
                layer_path = stage / (architecture + '.layer')
                with layer_path.open('xb') as target:
                    digest, _ = _download(opener, _url(architecture, 'blobs/rootfs.tar.gz'), target,
                                          limit=COMPRESSED_LIMIT, deadline=deadline,
                                          expected_size=layer['size'])
                if digest != layer['digest']:
                    raise FetchError('compressed rootfs digest mismatch')
                _diff_id(layer_path, diff_id, deadline)
                archive_path = stage / (architecture + '.tar')
                reference = _archive(archive_path, descriptor, manifest, config_descriptor,
                                     config, layer, layer_path)
                _remaining(deadline)
                with archive_path.open('rb') as source:
                    checksum = hashlib.file_digest(source, 'sha256').hexdigest() if hasattr(
                        hashlib, 'file_digest') else _file_digest(source)
                _remaining(deadline)
                results.append({'architecture': architecture, 'path': str(output / archive_path.name),
                                'image': reference, 'sha256': checksum, 'size': archive_path.stat().st_size})
            # Publish only after every requested architecture is fully verified.
            # link is exclusive: a competing file is never overwritten.
            for result in results:
                target = Path(result['path'])
                os.link(stage / target.name, target)
                stat = target.stat()
                published.append((target, stat.st_dev, stat.st_ino))
        return {'archives': results}
    except BaseException:
        for target, device, inode in published:
            try:
                stat = target.lstat()
                if (stat.st_dev, stat.st_ino) == (device, inode):
                    target.unlink()
            except FileNotFoundError:
                pass
        try:
            output.rmdir()  # Never delete a competing file or directory content.
        except OSError:
            pass
        raise


def _file_digest(source):
    digest = hashlib.sha256()
    for chunk in iter(lambda: source.read(CHUNK), b''):
        digest.update(chunk)
    return digest.hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--arch', choices=['amd64', 'arm64', 'all'], required=True)
    parser.add_argument('--output', type=Path, required=True,
                        help='new directory in an existing parent; existing targets refused')
    args = parser.parse_args(argv)
    try:
        print(json.dumps(fetch(args.arch, args.output), sort_keys=True))
    except (FetchError, OSError, urllib.error.URLError) as error:
        if isinstance(error, FetchError):
            message = str(error)
        elif isinstance(error, FileExistsError):
            message = 'output already exists'
        elif (isinstance(error, ssl.SSLCertVerificationError)
              or isinstance(error, urllib.error.URLError)
              and isinstance(error.reason, ssl.SSLCertVerificationError)):
            message = 'TLS certificate validation failed'
        elif isinstance(error, urllib.error.URLError):
            message = 'official artifact unavailable'
        else:
            message = 'output or artifact I/O failed'
        print('Debian OCI fetch failed: ' + message, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
