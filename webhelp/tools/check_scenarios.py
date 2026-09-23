"""Validate the published scenario catalog, its bilingual copy and the rendered page offline."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

from webhelp.site import HelpView, link_allowed, site_config  # noqa: E402

REQUIRED_FIELDS = ('slug', 'name', 'group', 'pain')
PAGE_KEYS = ('scenarios', 'nav.scenarios', 'meta.title_scenarios')


def read(root, name):
    return json.loads((root / name).read_text(encoding='utf-8'))


def key_paths(node, prefix=''):
    if not isinstance(node, dict):
        return {prefix}
    paths = set()
    for key, value in node.items():
        paths |= key_paths(value, prefix + '.' + key)
    return paths


def lookup(document, dotted):
    value = document
    for part in dotted.split('.'):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def validate(root=ROOT, page=None):
    """Return the list of problems; an empty list means the snapshot and page agree.

    ``page`` lets callers validate a specific rendered document instead of the
    live one, so the data checks can run against a temporary snapshot.
    """
    root = Path(root)
    errors = []
    try:
        document = read(root, 'scenarios.json')
    except (OSError, ValueError) as exc:
        return ['无法读取 scenarios.json: %s' % exc]

    config = site_config(root)
    groups = document.get('groups', [])
    items = document.get('items', [])
    known = {group.get('id') for group in groups}

    if not document.get('source'):
        errors.append('scenarios.json 缺少 source（快照来源）')
    elif not link_allowed(document['source'], config):
        errors.append('快照来源不在站外链接白名单: ' + str(document['source']))
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', str(document.get('captured_at', ''))):
        errors.append('scenarios.json 的 captured_at 不是 YYYY-MM-DD')
    if document.get('deep_link'):
        errors.append('场景应通过平台智能体入口使用，不应配置外部客户端深链')
    if not groups:
        errors.append('scenarios.json 没有声明分组')
    if not items:
        errors.append('scenarios.json 没有场景条目')

    declared = {item.get('group') for item in items}
    for group in groups:
        if group.get('id') not in declared:
            errors.append('分组没有场景: ' + str(group.get('id')))

    seen = set()
    for index, item in enumerate(items):
        label = str(item.get('slug') or ('第 %d 条' % (index + 1)))
        for field in REQUIRED_FIELDS:
            if not str(item.get(field, '')).strip():
                errors.append('%s 缺少字段 %s' % (label, field))
        if item.get('group') not in known:
            errors.append('%s 的分组未声明: %s' % (label, item.get('group')))
        if item.get('slug') in seen:
            errors.append('场景 slug 重复: ' + label)
        seen.add(item.get('slug'))
        if item.get('detail_url') and not link_allowed(item['detail_url'], config):
            errors.append('%s 的做法页不在白名单: %s' % (label, item['detail_url']))
        for field in ('desc', 'case_hint', 'prompt'):
            if '</' in str(item.get(field, '')):
                errors.append('%s 的 %s 含裸 </（会破坏内嵌数据）' % (label, field))
            if re.search(r'workbuddy|codebuddy\.work|install_skill\.py', str(item.get(field, '')), re.I):
                errors.append('%s 的 %s 仍含外部客户端或安装说明' % (label, field))

    packs = {}
    for lang in ('zh', 'en'):
        packs[lang] = read(root, 'lang/' + lang + '.json')
        for dotted in PAGE_KEYS:
            if not str(lookup(packs[lang], dotted) or '').strip():
                errors.append('%s 缺少页面文案 %s' % (lang, dotted))
        if not isinstance(lookup(packs[lang], 'scenarios'), dict):
            errors.append(lang + ' 缺少 scenarios 文案块')
    if all(isinstance(lookup(packs[lang], 'scenarios'), dict) for lang in ('zh', 'en')):
        left = key_paths(lookup(packs['zh'], 'scenarios'), 'scenarios')
        right = key_paths(lookup(packs['en'], 'scenarios'), 'scenarios')
        errors += ['en 缺少文案键 ' + key for key in sorted(left - right)]
        errors += ['zh 缺少文案键 ' + key for key in sorted(right - left)]
    for group in groups:
        for lang in ('zh', 'en'):
            if not str(lookup(packs[lang], 'scenarios.groups.' + str(group.get('id'))) or '').strip():
                errors.append('%s 缺少分组文案 scenarios.groups.%s' % (lang, group.get('id')))

    html = page if page is not None else HelpView('scenarios').render()
    rendered = len(re.findall(r'feature-card[^"]*scenario-card', html))
    if rendered != len(items):
        errors.append('页面渲染条目数 %d 与数据文件条目数 %d 不一致' % (rendered, len(items)))
    expected_buttons = len(items)
    buttons = len(re.findall(r'data-scenario-try="', html))
    if buttons != expected_buttons:
        errors.append('一键体验入口数 %d 与场景数 %d 不一致' % (buttons, expected_buttons))
    for target in re.findall(r'(?:href|src)="([^"]+)"', html):
        if not link_allowed(target, config):
            errors.append('页面出现未登记的站外地址: ' + target)
    for target in re.findall(r'(?:src|<link[^>]*href)="([^"]+)"', html):
        if not target.startswith('/'):
            errors.append('页面引用了站外资源，加载会产生站外请求: ' + target)
    for prompt in (item.get('prompt', '') for item in items):
        if prompt and prompt in html:
            errors.append('提示词正文出现在页面可见内容里')
            break

    payload = re.search(r'data-scenario-data>(.*?)</script>', html, re.S)
    if not payload:
        errors.append('页面缺少内嵌场景数据块')
        return errors
    if '</' in payload.group(1):
        errors.append('内嵌场景数据块含裸 </，会提前结束脚本')
    try:
        parsed = json.loads(payload.group(1))
    except ValueError as exc:
        errors.append('内嵌场景数据块不是合法 JSON: %s' % exc)
        return errors
    agents = parsed.get('agents') or {}
    if not isinstance(agents, dict) or len(agents) != len(items):
        errors.append('内嵌数据块的智能体映射条数与场景不一致')
    if not str(parsed.get('message') or '').strip():
        errors.append('内嵌数据块缺少初始对话 message')
    if parsed.get('open_path') != '/':
        errors.append('内嵌数据块的 open_path 应为控制台根路径 /')
    return errors


if __name__ == '__main__':
    failures = validate()
    print('\n'.join('FAIL ' + message for message in failures)
          if failures else 'OK 场景快照、双语文案、白名单与页面渲染一致')
    raise SystemExit(bool(failures))
