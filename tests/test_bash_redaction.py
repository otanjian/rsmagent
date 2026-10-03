"""Secrets remain filtered across actual process reads and progress polls."""
import os
import re
import shlex
import subprocess
import sys
import time

import pytest

from agent.tools.bash import background, launcher
from agent.tools.bash.bash import Bash
from agent.tools.bash.redaction import StreamRedactor


def test_every_chunk_boundary_matches_longest_literal_redaction():
    values = ['SCOPED', 'SCOPED-CREDENTIAL-LONG', '凭据-合成', 'abc', 'bcd']
    raw = 'plain SCOPED-CREDENTIAL-LONG / SCOPED / 凭据-合成 / abcd / tail'.encode()
    pattern = re.compile(b'|'.join(re.escape(v.encode()) for v in sorted(values, key=lambda v: len(v.encode()), reverse=True)))
    expected = pattern.sub(b'[REDACTED]', raw)
    for width in range(1, len(raw) + 1):
        redactor = StreamRedactor(values)
        chunks = [redactor.feed(raw[n:n + width]) for n in range(0, len(raw), width)]
        chunks.append(redactor.feed(b'', final=True))
        assert b''.join(chunks) == expected
        assert not redactor.pending


def test_plain_progress_is_immediate_and_incomplete_prefix_is_retained():
    redactor = StreamRedactor(['SYNTHETIC-SECRET'])
    assert redactor.feed(b'working...') == b'working...'
    assert redactor.feed(b'SYNTHETIC-SEC') == b''
    assert redactor.feed(b'RET done') == b'[REDACTED] done'
    assert redactor.feed(b'SYNTHETIC-no-match') == b'SYNTHETIC-no-match'
    assert redactor.feed(b'SYNTHETIC-') == b''
    assert redactor.feed(b'', final=True) == b'SYNTHETIC-'


def test_output_decoder_fallback_is_filtered_before_buffering(monkeypatch):
    from agent.tools.bash import redaction
    monkeypatch.setattr(redaction, '_fallback_encoding', lambda: 'gbk')
    secret = '合成凭据'
    redactor = StreamRedactor([secret])
    encoded = secret.encode('gbk')
    assert redactor.feed(encoded[:-1]) == b''
    assert redactor.feed(encoded[-1:] + b' done') == b'[REDACTED] done'


@pytest.fixture
def scoped_process(monkeypatch):
    secret = 'SYNTHETIC-SHARED-PREFIX-FOR-STREAM-CREDENTIAL'
    def launch(*args, **kwargs):
        process = subprocess.Popen(*args, **kwargs)
        # Real processes; only credential resolution is replaced by synthetic
        # values, with the shorter prefix deliberately registered first.
        process.execution_secrets = {'SHORT': secret[:9], 'LONG': secret}
        return process
    monkeypatch.setattr(launcher, 'popen', launch)
    return secret


@pytest.mark.skipif(sys.platform == 'win32', reason='POSIX subprocess integration')
@pytest.mark.parametrize('stream', ['stdout', 'stderr'])
def test_foreground_progress_never_exposes_partial_secret(tmp_path, scoped_process, stream):
    secret = scoped_process
    code = f'''import sys, time
stream = sys.{stream}
stream.write('safe-progress\\n'); stream.flush()
stream.write({secret[:-4]!r}); stream.flush()
time.sleep(0.4)
stream.write({secret[-4:]!r} + '\\nfinished'); stream.flush()
time.sleep(0.2)
'''
    tool = Bash({'cwd': str(tmp_path)})
    tool._PROGRESS_INTERVAL = 0.05
    tool._PROGRESS_MAX_BYTES = 32  # The unfiltered credential is longer than the tail.
    progress = []
    tool.progress_callback = progress.append
    result = tool._run_streaming([sys.executable, '-c', code], 5,
                                 {'PATH': os.environ.get('PATH', '/usr/bin:/bin')}, {})
    assert result.returncode == 0
    assert any('safe-progress' in snapshot for snapshot in progress)
    assert progress
    for output in progress + [result.stdout, result.stderr]:
        assert secret[:9] not in output
        assert secret[-10:] not in output
    assert getattr(result, stream) == 'safe-progress\n[REDACTED]\nfinished'


@pytest.mark.skipif(sys.platform == 'win32', reason='POSIX subprocess integration')
def test_background_poll_and_command_metadata_are_redacted(tmp_path, scoped_process):
    secret = scoped_process
    code = f'''import sys, time
secret = {secret!r}
sys.stdout.write('safe-progress\\n'); sys.stdout.flush()
sys.stdout.write(secret[:-4]); sys.stdout.flush()
time.sleep(0.4)
sys.stdout.write(secret[-4:] + '\\nfinished'); sys.stdout.flush()
time.sleep(0.2)
'''
    command = shlex.quote(sys.executable) + ' -c ' + shlex.quote(code)
    try:
        job_id = background.start(command, str(tmp_path), {'PATH': '/usr/bin:/bin'})
        output = ''
        observed_running_progress = False
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = background.read(job_id)
            output += state['output']
            assert secret[:9] not in state['command']
            assert secret[:9] not in output and secret[-10:] not in output
            observed_running_progress |= state['running'] and 'safe-progress' in output
            assert all(secret[:9] not in item['command'] for item in background.list_jobs())
            if not state['running']:
                break
            time.sleep(0.02)
        else:
            pytest.fail('background command did not finish')
        assert observed_running_progress
        assert output == 'safe-progress\n[REDACTED]\nfinished'
    finally:
        background.reset()
