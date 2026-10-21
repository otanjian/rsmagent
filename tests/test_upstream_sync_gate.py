"""The synchronization ledger must not silently lose unresolved behavior."""
import copy
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("sync_manifest", ROOT / "scripts/upstream_sync_manifest.py")
manifest_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(manifest_module)


def ledger():
    return manifest_module.load_manifest(ROOT / "docs/design/upstream-sync/capabilities.json")


def test_manifest_references_real_entrypoints_and_carries_all_gaps():
    data = ledger()
    assert not manifest_module.validate_manifest(data, ROOT)
    assert {item["id"] for item in data["capabilities"]} >= {f"GAP-{n:03}" for n in range(1, 8)}


def test_manifest_rejects_missing_owner_and_fake_completion():
    data = copy.deepcopy(ledger())
    item = data["capabilities"][0]
    item["owner"] = ""
    item["verification"] = "passed"
    item.pop("candidate", None)
    errors = manifest_module.validate_manifest(data, ROOT)
    assert any("owner required" in error for error in errors)
    assert any("passed requires" in error for error in errors)


def test_manifest_rejects_unreviewed_exclusion_and_dead_entry():
    data = copy.deepcopy(ledger())
    data["capabilities"][0].update(disposition="product-excluded", entrypoints=["not-an-entry.js"])
    errors = manifest_module.validate_manifest(data, ROOT)
    assert any("review evidence" in error for error in errors)
    assert any("missing or non-repository" in error for error in errors)


def gate():
    import sys
    sys.path.insert(0, str(ROOT / 'scripts'))
    spec = importlib.util.spec_from_file_location('upstream_gate', ROOT / 'scripts/check-upstream-sync.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_deleted_history_and_source_only_edits_fail():
    module = gate()
    baseline = ledger()
    candidate = copy.deepcopy(baseline)
    candidate['capabilities'].pop(0)
    assert 'historical capability removed' in ' '.join(module.history_errors(candidate, baseline))
    candidate = copy.deepcopy(baseline)
    candidate['capabilities'][0]['source']['sha'] = 'a' * 40
    assert 'source changed without new evidence' in ' '.join(module.history_errors(candidate, baseline))


def test_skip_xfail_missing_and_zero_collection_are_not_passes(tmp_path):
    module = gate()
    result = tmp_path / 'pytest.xml'
    for body in ('', '<testcase name="required"><skipped type="pytest.xfail"/></testcase>',
                 '<testcase name="required"><skipped/></testcase>',
                 '<testcase name="required"><failure/></testcase>'):
        result.write_text('<testsuite>' + body + '</testsuite>')
        assert module.junit_errors(result, ['required'])
    result.write_text('<testsuite><testcase name="required"/></testsuite>')
    assert not module.junit_errors(result, ['required'])
    for output in ('', 'ok 1 - required # SKIP missing browser', 'ok 1 - required # TODO', 'not ok 1 - required'):
        assert module.tap_errors(output, ['required'])
    assert not module.tap_errors('ok 1 - required\n', ['required'])
    assert module.browser_errors(tmp_path / 'absent.json', ['required'])


def test_browser_subset_and_missing_assets_fail(tmp_path):
    import json
    module = gate()
    report = tmp_path / 'browser.json'
    report.write_text(json.dumps({'scenarios':[{'name':'other','passed':True}]}))
    assert module.browser_errors(report, ['required'])
    report.write_text(json.dumps({'scenarios':[{'name':'required','passed':True}],
                                 'unexpectedRoutes':[{'pathname':'/assets/missing.js'}]}))
    assert module.browser_errors(report, ['required'])


def test_actual_assembly_detects_removed_route_script_and_style(tmp_path):
    """Probe a disposable assembly; never mutate the developer's checkout."""
    import shutil
    module = gate()
    root = tmp_path / 'candidate'
    root.mkdir()
    for path in ROOT.iterdir():
        if path.name in {'channel', 'docs'}:
            shutil.copytree(path, root / path.name, ignore=shutil.ignore_patterns('__pycache__'))
        elif path.name != '.git':
            (root / path.name).symlink_to(path, target_is_directory=path.is_dir())
    # The probe script has ROOT based on its own path, so copy it instead of
    # executing a symlink that would resolve back to the original candidate.
    (root / 'scripts').unlink()
    shutil.copytree(ROOT / 'scripts', root / 'scripts', ignore=shutil.ignore_patterns('__pycache__'))
    changes = [
        ('channel/web/chat.html', '<script defer src="/assets/js/core/router.js"></script>', ''),
        ('channel/web/static/css/markdown.css', 'overflow-wrap: anywhere', 'overflow-wrap: normal'),
        ('channel/web/route_registry.py', 'RouteEntry(path, "ChatHandler",', 'RouteEntry(path + "/missing", "ChatHandler",'),
    ]
    assert not module.runtime_errors(root)
    for rel, before, after in changes:
        path = root / rel
        original = path.read_text()
        assert before in original
        try:
            path.write_text(original.replace(before, after))
            assert module.runtime_errors(root), rel
        finally:
            path.write_text(original)

    # Git object reads use the real fixed commits; only the disposable candidate
    # filesystem changes. Forgetting to update source_sha must still be caught.
    original_command = module.command
    module.command = lambda args, _root, *rest, **kw: original_command(args, ROOT, *rest, **kw)
    data = ledger()
    added = root / 'channel/web/static/js/core/unclassified.js'
    added.write_text('// a newly merged but unclassified module')
    try:
        assert any('unclassified upstream module' in e for e in module.source_errors(root, data, data['source_sha'], None))
    finally:
        added.unlink()
    changed = root / 'channel/web/static/js/core/router.js'
    original = changed.read_text()
    try:
        changed.write_text(original + '\n// unreviewed upstream change\n')
        assert any('reference module differs' in e for e in module.source_errors(root, data, data['source_sha'], None))
    finally:
        changed.write_text(original)
        module.command = original_command


def test_candidate_digest_includes_uncommitted_source_but_not_installed_dependencies(tmp_path):
    import subprocess
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    (tmp_path / 'channel').mkdir()
    source = tmp_path / 'channel/runtime.js'
    source.write_text('first')
    (tmp_path / '.gitignore').write_text('node_modules/\n')
    module = gate()
    before = module.candidate_digest(tmp_path)
    source.write_text('second')
    assert module.candidate_digest(tmp_path) != before
    # Shared authorization, install inputs and the workflow are part of the
    # candidate even when none of the Web modules changed.
    for name in ('common/authorization.py', 'requirements.txt',
                 'desktop/package-lock.json', '.gitea/workflows/check.yml'):
        before = module.candidate_digest(tmp_path)
        other = tmp_path / name
        other.parent.mkdir(parents=True, exist_ok=True)
        other.write_text('candidate input')
        assert module.candidate_digest(tmp_path) != before
    current = module.candidate_digest(tmp_path)
    (tmp_path / 'tests/browser/node_modules').mkdir(parents=True)
    (tmp_path / 'tests/browser/node_modules/dependency.js').write_text('installed')
    assert module.candidate_digest(tmp_path) == current
    evidence = tmp_path / 'openspec/changes/example/evidence/report.json'
    evidence.parent.mkdir(parents=True)
    evidence.write_text('{}')
    assert module.candidate_digest(tmp_path) == current


def test_integration_review_is_bound_to_source_candidate_and_real_consumers(tmp_path):
    import hashlib
    module = gate()
    for name in ('review.md', 'runtime.py', 'test.py'):
        (tmp_path / name).write_text('evidence')
    content = b'reviewed implementation'
    review = {'path': 'module.js', 'owner': 'maintainers', 'evidence': ['review.md'],
              'integration': {'source_blob': 'upstream', 'candidate_sha256': hashlib.sha256(content).hexdigest(),
                              'reason': 'IAM adapter', 'consumers': ['runtime.py'], 'tests': ['test.py']}}
    data = {'source_reviews': [review]}
    assert module.reviewed_integration(tmp_path, data, 'module.js', 'upstream', content)
    assert not module.reviewed_integration(tmp_path, data, 'module.js', 'changed-source', content)
    assert not module.reviewed_integration(tmp_path, data, 'module.js', 'upstream', content + b'changed')
    (tmp_path / 'runtime.py').unlink()
    assert not module.reviewed_integration(tmp_path, data, 'module.js', 'upstream', content)


def test_brand_gate_allows_only_internal_host_predicate(tmp_path):
    module = gate()
    root = tmp_path / 'candidate'; root.mkdir()
    dist = tmp_path / module.BRAND_WEB_DIST; dist.mkdir(parents=True)
    bundle = dist / 'app.js'
    bundle.write_text('location.hostname.includes("opencode.ai")')
    assert not module.brand_errors(root)
    for value in ('https://opencode.ai/help', 'OpenCode', '"opencode.ai"'):
        bundle.write_text(value)
        assert module.brand_errors(root)
