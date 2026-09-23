"""Validate the local scenario-detail snapshot and the pages rendered from it, offline.

Two things are being defended here:

* the snapshot is complete — every catalog scenario has all three pages, every
  block is a declared primitive, and no block smuggles in a tag outside the
  inline whitelist;
* nothing was dropped — the reviewed platform copy in the text baseline must
  still show up in the rendered page, segment by segment.
"""
from __future__ import annotations

import json
import re
import sys
from html import unescape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

from webhelp.site import (SCENARIO_INLINE_TAGS, SCENARIO_KINDS, SCENARIO_NOTE_TONES,  # noqa: E402
                          SCENARIO_PRIMITIVES, HelpView, link_allowed, site_config)

PAGE_KEYS = ('nav.scenarios', 'scenario_doc', 'scenario_doc.toc', 'scenario_doc.tabs_label',
             'scenario_doc.notice', 'scenario_doc.source', 'scenario_doc.back',
             'scenario_doc.prev', 'scenario_doc.next',
             'scenario_doc.not_found', 'scenario_doc.not_found_desc')
KIND_KEYS = tuple('scenario_doc.kinds.' + kind for kind in SCENARIO_KINDS)
#: 正文区域由模板生成时允许出现的标签；此外只允许快照登记的行内/列表标签。
BODY_TAGS = frozenset({'h2', 'h3', 'h4', 'p', 'div', 'span', 'aside', 'section', 'article', 'ol', 'ul', 'li',
                       'table', 'thead', 'tbody', 'tr', 'th', 'td', 'nav', 'a', 'button', 'b', 'strong', 'br'})
_TAG = re.compile(r'</?([a-zA-Z][a-zA-Z0-9]*)\b[^>]*>')
_SCRIPT = re.compile(r'<script\b.*?</script>', re.S | re.I)
_ATTR = re.compile(r'<[a-zA-Z][^>]*?\s(on[a-zA-Z]+|style)\s*=', re.S)

SNAPSHOT_FIELDS = ('source', 'extracted_at', 'extractor', 'kinds', 'primitives', 'catalog', 'items')


def read(root, name):
    return json.loads((root / name).read_text(encoding='utf-8'))


def lookup(document, dotted):
    value = document
    for part in dotted.split('.'):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def squash(text):
    return re.sub(r'\s+', ' ', text).strip()


def tags_in(value):
    return {tag.lower() for tag in _TAG.findall(str(value))}


def walk_blocks(blocks):
    for block in blocks or []:
        if not isinstance(block, dict):
            yield block
            continue
        yield block
        if block.get('type') == 'demo_steps':
            for item in block.get('items', []):
                yield from walk_blocks(item.get('blocks', []))


def string_leaves(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for entry in value:
            yield from string_leaves(entry)
    elif isinstance(value, dict):
        for entry in value.values():
            yield from string_leaves(entry)


def flatten_page(page):
    """Derived text of a page: every string in the structure, tags removed, squashed."""
    joined = ''.join(string_leaves({key: value for key, value in page.items() if key != 'text'}))
    return squash(unescape(re.sub(r'<[^>]*>', '', joined)))


def visible_text(html):
    return squash(unescape(re.sub(r'<[^>]*>', '', _SCRIPT.sub(' ', html))))


def validate_blocks(label, blocks, errors):
    for index, block in enumerate(walk_blocks(blocks)):
        what = '%s 第 %d 块' % (label, index + 1)
        if not isinstance(block, dict):
            errors.append('%s 不是对象' % what)
            continue
        kind = block.get('type')
        if kind not in SCENARIO_PRIMITIVES:
            errors.append('%s 的原语未登记: %s' % (what, kind))
            continue
        if kind == 'note' and block.get('tone') not in SCENARIO_NOTE_TONES:
            errors.append('%s 的提示语气未登记: %s' % (what, block.get('tone')))
        if not block:
            errors.append('%s 是空块' % what)
        for value in string_leaves({key: entry for key, entry in block.items() if key != 'type'}):
            extra = tags_in(value) - SCENARIO_INLINE_TAGS
            if extra:
                errors.append('%s 含未登记的标签: %s' % (what, sorted(extra)))


def validate_document(root=ROOT):
    """Snapshot-only checks: shape, coverage, whitelist and the text-loss invariant."""
    root = Path(root)
    errors = []
    try:
        snapshot = read(root, 'scenario_docs.json')
    except (OSError, ValueError) as exc:
        return ['无法读取 scenario_docs.json: %s' % exc]

    config = site_config(root)
    for field in SNAPSHOT_FIELDS:
        if not snapshot.get(field):
            errors.append('scenario_docs.json 缺少 %s' % field)
    if errors:
        return errors

    if not link_allowed(snapshot['source'], config):
        errors.append('快照来源不在站外链接白名单: ' + str(snapshot['source']))
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', str(snapshot.get('extracted_at', ''))):
        errors.append('scenario_docs.json 的 extracted_at 不是 YYYY-MM-DD')
    if list(snapshot['kinds']) != list(SCENARIO_KINDS):
        errors.append('scenario_docs.json 的 kinds 与页面分页不一致: %s' % snapshot['kinds'])
    declared = set(snapshot['primitives'])
    if declared != SCENARIO_PRIMITIVES:
        errors.append('scenario_docs.json 的原语清单与渲染器不一致: %s'
                      % sorted(declared ^ SCENARIO_PRIMITIVES))

    catalog, items = snapshot['catalog'], snapshot['items']
    if set(catalog) != set(items):
        errors.append('目录与详情条目不一致: %s' % sorted(set(catalog) ^ set(items)))
    for slug, entry in sorted(items.items()):
        if slug not in catalog:
            errors.append('%s 不在目录里' % slug)
            continue
        if set(entry) != set(SCENARIO_KINDS):
            errors.append('%s 的分页不完整: %s' % (slug, sorted(entry)))
        for kind in SCENARIO_KINDS:
            page = entry.get(kind)
            label = '%s/%s' % (slug, kind)
            if not isinstance(page, dict):
                errors.append('%s 缺少页面' % label)
                continue
            if page.get('kind') != kind:
                errors.append('%s 的 kind 字段与分页不一致' % label)
            if not page.get('title'):
                errors.append('%s 缺少标题' % label)
            blocks = page.get('blocks') or []
            if not blocks:
                errors.append('%s 没有正文' % label)
            validate_blocks(label, blocks, errors)
            if any(re.search(r'workbuddy|codebuddy\.work|install_skill\.py', value, re.I)
                   for value in string_leaves(page)):
                errors.append('%s 仍含外部客户端或安装说明' % label)
            baseline = page.get('text') or []
            if not baseline:
                errors.append('%s 没有来源文本基线，无法验证是否丢文本' % label)
            flat = flatten_page(page)
            for segment in baseline:
                if segment not in flat:
                    errors.append('%s 丢文本: %s' % (label, segment[:48]))

    for lang in ('zh', 'en'):
        try:
            pack = read(root, 'lang/' + lang + '.json')
        except (OSError, ValueError) as exc:
            errors.append('无法读取 lang/%s.json: %s' % (lang, exc))
            continue
        for dotted in PAGE_KEYS + KIND_KEYS:
            if not str(lookup(pack, dotted) or '').strip():
                errors.append('%s 缺少页面文案 %s' % (lang, dotted))
    return errors


def validate_page(slug, kind, lang, root=ROOT, errors=None):
    """Render one page and check its links, tags and text coverage."""
    errors = [] if errors is None else errors
    snapshot = read(Path(root), 'scenario_docs.json')
    baseline = snapshot['items'][slug][kind].get('text') or []
    label = '%s/%s/%s' % (slug, kind, lang)

    view = HelpView('scenario_doc', lang, {'p': slug, 'k': kind}, origin='http://localhost:9899')
    if not view.ok:
        errors.append('%s 未渲染成正文页' % label)
        return errors
    html = view.render('header') + view.render() + view.render('footer')
    config = site_config(Path(root))

    for target in re.findall(r'(?:href|src)="([^"]+)"', html):
        if not link_allowed(target, config):
            errors.append('%s 出现未登记的站外地址: %s' % (label, target))
    if _ATTR.search(html):
        errors.append('%s 出现内联事件或内联样式' % label)

    stripped = _SCRIPT.sub(' ', html)
    match = re.search(r'<div class="doc-body sdoc-body">(.*?)(?:<nav class="doc-pager"|</article>)', stripped, re.S)
    if match is None:
        errors.append('%s 找不到正文区域' % label)
    else:
        extra = tags_in(match.group(1)) - SCENARIO_INLINE_TAGS - BODY_TAGS
        if extra:
            errors.append('%s 正文出现未登记的标签: %s' % (label, sorted(extra)))

    text = visible_text(html)
    for segment in baseline:
        if segment not in text:
            errors.append('%s 渲染页丢文本: %s' % (label, segment[:48]))
    for required in (view.scenario_doc_meta(slug).get('name', ''),):
        if required and required not in text:
            errors.append('%s 渲染页缺少场景名: %s' % (label, required))
    if lang == 'en':
        marker = str(lookup(read(Path(root), 'lang/en.json'), 'scenario_doc.kinds.' + kind) or '')
        if 'lang="en"' not in html or (marker and marker not in html):
            errors.append('%s 缺少英文外壳文案' % label)
    elif 'lang="zh-CN"' not in html:
        errors.append('%s 缺少中文外壳标记' % label)
    return errors


def validate(render=True, limit=0):
    errors = validate_document()
    if not render or errors:
        return errors
    snapshot = read(ROOT, 'scenario_docs.json')
    slugs = sorted(snapshot['items'])
    if limit:
        slugs = slugs[:limit]
    for slug in slugs:
        for kind in SCENARIO_KINDS:
            for lang in ('zh',):
                validate_page(slug, kind, lang, errors=errors)
    for slug in slugs[:3]:
        validate_page(slug, 'tasks', 'en', errors=errors)
    return errors


if __name__ == '__main__':
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    failures = validate(limit=limit)
    print('\n'.join('FAIL ' + message for message in failures[:40])
          if failures else 'OK 场景详情快照完整、无外部依赖、渲染后不丢文本')
    raise SystemExit(bool(failures))
