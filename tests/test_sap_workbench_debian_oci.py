"""Pinned OCI reconstruction contracts; network and official payloads are fixed fixtures."""
import base64
from copy import deepcopy
import gzip
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import urllib.request

import pytest


PATH = Path(__file__).resolve().parents[1] / 'Scene/sap_workbench/deployment/fetch-debian-oci.py'
SPEC = importlib.util.spec_from_file_location('sap_debian_oci', PATH)
oci = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(oci)


def encoded(value):
    return json.dumps(value, separators=(',', ':')).encode() + b'\n'


def sha(data):
    return 'sha256:' + hashlib.sha256(data).hexdigest()


def descriptor(data, media_type, *, embedded=False):
    value = {'mediaType': media_type, 'digest': sha(data), 'size': len(data)}
    if embedded:
        value['data'] = base64.b64encode(data).decode()
    return value


def fixture(arch, *, mutate_config=None, mutate_manifest=None):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w') as archive:
        member = tarfile.TarInfo('etc/debian_version')
        text = b'fixed Debian OCI unit fixture\n'
        member.size = len(text)
        archive.addfile(member, io.BytesIO(text))
    rootfs = stream.getvalue()
    layer = gzip.compress(rootfs, mtime=0)
    platform = {'os': 'linux', 'architecture': arch}
    if arch == 'arm64':
        platform['variant'] = 'v8'
    config = {**platform, 'rootfs': {'type': 'layers', 'diff_ids': [sha(rootfs)]}}
    if mutate_config:
        mutate_config(config)
    config_bytes = encoded(config)
    manifest = {'schemaVersion': 2, 'mediaType': oci.MANIFEST_TYPE,
                'config': descriptor(config_bytes, oci.CONFIG_TYPE, embedded=True),
                'layers': [descriptor(layer, oci.LAYER_TYPE)]}
    if mutate_manifest:
        mutate_manifest(manifest)
    manifest_bytes = encoded(manifest)
    reference = descriptor(manifest_bytes, oci.MANIFEST_TYPE, embedded=True)
    reference['platform'] = platform
    index = {'schemaVersion': 2, 'mediaType': oci.INDEX_TYPE, 'manifests': [reference]}
    return {'index': index, 'manifest': manifest_bytes, 'config': config_bytes,
            'layer': layer, 'rootfs': rootfs, 'digest': reference['digest']}


class Response(io.BytesIO):
    def __init__(self, data, url, *, headers=None, status=200, final_url=None):
        super().__init__(data)
        self.headers = {'Content-Length': str(len(data))} if headers is None else headers
        self.url, self.status = final_url or url, status

    def getcode(self):
        return self.status

    def geturl(self):
        return self.url


class Network:
    def __init__(self, values):
        self.values, self.requests = values, []

    def open(self, request, *, timeout):
        self.requests.append((request.full_url, timeout, request.headers))
        value = self.values[request.full_url]
        if isinstance(value, BaseException):
            raise value
        if isinstance(value, tuple):
            return Response(value[0], request.full_url, **value[1])
        return Response(value, request.full_url)


def setup(monkeypatch, tmp_path, fixtures=None):
    fixtures = fixtures or {arch: fixture(arch) for arch in oci.COMMITS}
    versions = tmp_path / 'versions.json'
    versions.write_bytes(encoded({'base_images': {
        arch: 'debian:bookworm-20260918-slim@' + value['digest']
        for arch, value in fixtures.items()}}))
    monkeypatch.setattr(oci, 'VERSIONS', versions)
    network = Network({url: data for arch, value in fixtures.items() for url, data in (
        (oci._url(arch, 'index.json'), encoded(value['index'])),
        (oci._url(arch, 'blobs/rootfs.tar.gz'), value['layer']))})
    monkeypatch.setattr(oci, '_opener', lambda: network)
    return fixtures, network


@pytest.mark.parametrize('architecture', ['amd64', 'arm64', 'all'])
def test_reconstruction_keeps_original_blobs_and_digest_reference(monkeypatch, tmp_path, architecture):
    fixtures, network = setup(monkeypatch, tmp_path)
    output = tmp_path / 'new-archives'
    result = oci.fetch(architecture, output)
    arches = list(oci.COMMITS) if architecture == 'all' else [architecture]
    assert [row['architecture'] for row in result['archives']] == arches
    assert {item.name for item in output.iterdir()} == {arch + '.tar' for arch in arches}
    for row in result['archives']:
        value = fixtures[row['architecture']]
        archive_bytes = Path(row['path']).read_bytes()
        assert row['sha256'] == hashlib.sha256(archive_bytes).hexdigest()
        assert row['size'] == len(archive_bytes)
        with tarfile.open(row['path']) as archive:
            assert len(archive.getmembers()) == 5 and all(item.isfile() for item in archive.getmembers())
            index = json.load(archive.extractfile('index.json'))
            reference = index['manifests'][0]
            assert reference['digest'] == value['digest']
            assert reference['annotations'] == {
                'io.containerd.image.name': row['image'],
                'org.opencontainers.image.ref.name': row['image']}
            assert row['image'] == 'docker.io/library/debian@' + value['digest']
            for raw in (value['manifest'], value['config'], value['layer']):
                assert archive.extractfile('blobs/sha256/' + sha(raw)[7:]).read() == raw
            assert json.load(archive.extractfile('oci-layout')) == {'imageLayoutVersion': '1.0.0'}
    assert len(network.requests) == 2 * len(arches)
    assert all(0 < request[1] <= oci.SOCKET_TIMEOUT for request in network.requests)
    assert all('blobs/sha256/' not in request[0] for request in network.requests)
    assert not (output / 'etc').exists(), 'rootfs must never be extracted'


@pytest.mark.parametrize('kind', ['lock', 'manifest_size', 'manifest_digest', 'platform',
                                  'config_digest', 'config_platform', 'layer_media_type',
                                  'layer_size', 'duplicate_manifest', 'duplicate_key'])
def test_tampered_metadata_is_rejected_without_layer_download_or_archive(monkeypatch, tmp_path, kind):
    fixtures = {arch: fixture(arch) for arch in oci.COMMITS}
    value = fixtures['amd64']
    if kind == 'config_platform':
        value = fixtures['amd64'] = fixture('amd64', mutate_config=lambda x: x.update(architecture='arm64'))
    elif kind == 'layer_media_type':
        value = fixtures['amd64'] = fixture('amd64', mutate_manifest=lambda x: x['layers'][0].update(mediaType=oci.CONFIG_TYPE))
    elif kind == 'layer_size':
        value = fixtures['amd64'] = fixture('amd64', mutate_manifest=lambda x: x['layers'][0].update(size=oci.COMPRESSED_LIMIT + 1))
    elif kind == 'config_digest':
        value = fixtures['amd64'] = fixture('amd64', mutate_manifest=lambda x: x['config'].update(digest='sha256:' + '0' * 64))
    _, network = setup(monkeypatch, tmp_path, fixtures)
    index = deepcopy(value['index'])
    entry = index['manifests'][0]
    if kind == 'lock':
        entry['digest'] = 'sha256:' + '0' * 64
    elif kind == 'manifest_size':
        entry['size'] += 1
    elif kind == 'manifest_digest':
        entry['data'] = base64.b64encode(value['manifest'] + b' ').decode()
    elif kind == 'platform':
        entry['platform']['architecture'] = 'arm64'
    elif kind == 'duplicate_manifest':
        index['manifests'].append(deepcopy(entry))
    data = encoded(index)
    if kind == 'duplicate_key':
        data = data.replace(b'{"schemaVersion":2,', b'{"schemaVersion":2,"schemaVersion":2,', 1)
    network.values[oci._url('amd64', 'index.json')] = data
    output = tmp_path / 'failed'
    with pytest.raises(oci.FetchError):
        oci.fetch('amd64', output)
    assert len(network.requests) == 1 and not output.exists()


@pytest.mark.parametrize('kind', ['changed', 'truncated', 'overlong', 'invalid_gzip', 'diff_id', 'unpacked_limit'])
def test_layer_and_unpacked_integrity_failures_leave_no_usable_archive(monkeypatch, tmp_path, kind):
    fixtures = {arch: fixture(arch) for arch in oci.COMMITS}
    if kind == 'diff_id':
        fixtures['arm64'] = fixture('arm64', mutate_config=lambda x: x['rootfs'].update(diff_ids=['sha256:' + '0' * 64]))
    elif kind == 'invalid_gzip':
        bad_layer = b'not a gzip stream'
        fixtures['arm64'] = fixture('arm64', mutate_manifest=lambda x: x['layers'].__setitem__(
            0, descriptor(bad_layer, oci.LAYER_TYPE)))
        fixtures['arm64']['layer'] = bad_layer
    _, network = setup(monkeypatch, tmp_path, fixtures)
    url = oci._url('arm64', 'blobs/rootfs.tar.gz')
    original = network.values[url]
    if kind == 'changed':
        network.values[url] = original[:-1] + bytes([original[-1] ^ 1])
    elif kind == 'truncated':
        network.values[url] = (original[:-1], {'headers': {}})
    elif kind == 'overlong':
        network.values[url] = (original + b'x', {'headers': {}})
    elif kind == 'unpacked_limit':
        monkeypatch.setattr(oci, 'UNPACKED_LIMIT', 100)
    output = tmp_path / 'all-failed'
    with pytest.raises(oci.FetchError):
        oci.fetch('all', output)
    assert not output.exists(), 'even a previously verified architecture must not be published on failure'


@pytest.mark.parametrize('kind', ['existing_directory', 'existing_file', 'dangling_symlink'])
def test_existing_output_is_refused_before_network_and_preserved(monkeypatch, tmp_path, kind):
    _, network = setup(monkeypatch, tmp_path)
    output = tmp_path / 'existing'
    if kind == 'existing_directory':
        output.mkdir()
        (output / 'keep').write_text('user content')
    elif kind == 'existing_file':
        output.write_text('user content')
    else:
        output.symlink_to(tmp_path / 'missing')
    with pytest.raises(FileExistsError):
        oci.fetch('amd64', output)
    assert network.requests == []
    assert output.is_symlink() if kind == 'dangling_symlink' else output.exists()
    if kind == 'existing_directory':
        assert (output / 'keep').read_text() == 'user content'


def test_duplicate_publish_does_not_overwrite_or_delete_competing_file(monkeypatch, tmp_path):
    _, network = setup(monkeypatch, tmp_path)
    output = tmp_path / 'new'
    link = oci.os.link

    def conflict(source, target):
        if Path(target).name == 'arm64.tar':
            Path(target).write_text('competing file')
        return link(source, target)

    monkeypatch.setattr(oci.os, 'link', conflict)
    with pytest.raises(FileExistsError):
        oci.fetch('all', output)
    assert len(network.requests) == 4
    assert [file.name for file in output.iterdir()] == ['arm64.tar']
    assert (output / 'arm64.tar').read_text() == 'competing file'


@pytest.mark.parametrize('kind', ['redirect', 'status', 'metadata_limit', 'deadline', 'tls_error'])
def test_network_or_budget_failure_is_closed_and_cleans_output(monkeypatch, tmp_path, kind):
    _, network = setup(monkeypatch, tmp_path)
    url = oci._url('amd64', 'index.json')
    if kind == 'redirect':
        network.values[url] = (network.values[url], {'final_url': 'https://example.invalid/index.json'})
    elif kind == 'status':
        network.values[url] = (network.values[url], {'status': 206})
    elif kind == 'metadata_limit':
        network.values[url] = b'x' * (oci.METADATA_LIMIT + 1)
    elif kind == 'deadline':
        monkeypatch.setattr(oci, 'TOTAL_TIMEOUT', -1)
    else:
        network.values[url] = oci.urllib.error.URLError('fixed TLS fixture failure')
    output = tmp_path / 'failed'
    with pytest.raises((oci.FetchError, oci.urllib.error.URLError)):
        oci.fetch('amd64', output)
    assert not output.exists()


def test_cli_requires_explicit_architecture_and_output_before_any_download(monkeypatch, capsys):
    monkeypatch.setattr(oci, 'fetch', lambda *_: pytest.fail('unexpected download'))
    with pytest.raises(SystemExit) as error:
        oci.main([])
    assert error.value.code == 2
    assert '--output' in capsys.readouterr().err


@pytest.mark.parametrize('error,message', [
    (oci.FetchError('official artifact truncated'), 'official artifact truncated'),
    (FileExistsError('fixture-private-exception'), 'output already exists'),
    (oci.ssl.SSLCertVerificationError(1, 'fixture-private-exception'), 'TLS certificate validation failed'),
    (oci.urllib.error.URLError(oci.ssl.SSLCertVerificationError(1, 'fixture-private-exception')),
     'TLS certificate validation failed'),
    (oci.urllib.error.URLError('fixture-private-exception'), 'official artifact unavailable'),
    (oci.urllib.error.URLError(oci.ssl.SSLEOFError(1, 'fixture-private-exception')),
     'official artifact unavailable'),
    (OSError('fixture-private-exception'), 'output or artifact I/O failed'),
])
def test_cli_failure_diagnostics_are_distinct_and_never_echo_external_exception(
        monkeypatch, tmp_path, capsys, error, message):
    def failed_fetch(*_):
        raise error

    monkeypatch.setattr(oci, 'fetch', failed_fetch)
    assert oci.main(['--arch', 'amd64', '--output', str(tmp_path / 'new')]) == 1
    captured = capsys.readouterr()
    assert captured.out == ''
    assert captured.err == 'Debian OCI fetch failed: ' + message + '\n'
    assert 'fixture-private-exception' not in captured.err


def test_slow_chunked_response_cannot_extend_total_download_deadline(monkeypatch, tmp_path):
    _, network = setup(monkeypatch, tmp_path)
    clock = [0]
    monkeypatch.setattr(oci.time, 'monotonic', lambda: clock[0])
    url = oci._url('amd64', 'blobs/rootfs.tar.gz')
    original = network.values[url]
    open_response = network.open

    class SlowResponse(Response):
        def read1(self, size):
            clock[0] += oci.TOTAL_TIMEOUT + 1
            return super().read1(min(size, 1))

    def slow_open(request, *, timeout):
        if request.full_url == url:
            return SlowResponse(original, url)
        return open_response(request, timeout=timeout)

    monkeypatch.setattr(network, 'open', slow_open)
    output = tmp_path / 'slow-failed'
    with pytest.raises(oci.FetchError, match='time budget'):
        oci.fetch('amd64', output)
    assert not output.exists()


def test_fixed_sources_default_tls_and_redirect_handler_are_not_weakened(monkeypatch):
    assert set(oci.COMMITS) == {'amd64', 'arm64'}
    assert oci.COMMITS['amd64'] == '8f962b15d7884a90e17876a9303cbac909d119aa'
    assert oci.COMMITS['arm64'] == 'ca011a8b1c3b259e4cbbf83bf6841f1fd5f497c1'
    for arch in oci.COMMITS:
        assert oci._url(arch, 'blobs/rootfs.tar.gz').startswith(
            'https://raw.githubusercontent.com/debuerreotype/docker-debian-artifacts/' + oci.COMMITS[arch])
    with pytest.raises(oci.FetchError):
        oci._url('arm64', 'https://example.invalid')
    with pytest.raises(oci.FetchError, match='redirect'):
        oci.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://example.invalid')
    handlers = []
    monkeypatch.setattr(oci.urllib.request, 'build_opener', lambda *values: handlers.extend(values))
    oci._opener()
    context = next(value for value in handlers if isinstance(value, urllib.request.HTTPSHandler))._context
    assert context.check_hostname and context.verify_mode == oci.ssl.CERT_REQUIRED
