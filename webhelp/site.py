"""Request-local help views; HTML templates and JSON content have no PHP dependency."""
from __future__ import annotations

import json
import re
import sys
from datetime import date
from functools import lru_cache
from html import escape, unescape
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

import web

PAGES = frozenset({'index', 'features', 'enterprise', 'scenarios', 'scenario_doc', 'quickstart', 'manual',
                   'architecture', 'about', 'doc'})

#: 场景详情页的三个子页面；清单顺序即页面上的标签顺序。
SCENARIO_KINDS = ('intro', 'tasks', 'demo')
SCENARIO_DOC_KIND = 'intro'


def resource_root() -> Path:
    if getattr(sys, 'frozen', False):
        return Path(sys._MEIPASS) / 'webhelp'
    return Path(__file__).resolve().parent


def read_json(relative: str):
    return json.loads((resource_root() / relative).read_text(encoding='utf-8'))


def lookup(value, key, default=None):
    for part in key.split('.'):
        if not isinstance(value, dict) or part not in value:
            return default
        value = value[part]
    return value


def e(value):
    return escape(str(value), quote=True)


#: 详情快照里允许出现的标签：行内强调、换行与列表。渲染前再过滤一次，
#: 快照即使被改写也带不进脚本或外部资源。
SCENARIO_INLINE_TAGS = frozenset({'b', 'strong', 'br', 'ul', 'ol', 'li'})
SCENARIO_NOTE_TONES = frozenset({'info', 'warn', 'verdict', 'think', 'prompt'})
SCENARIO_PRIMITIVES = frozenset({'heading', 'prose', 'note', 'cta', 'pains', 'compare', 'cards', 'steps',
                                 'demo_steps', 'outputs', 'files', 'score', 'tasks', 'table', 'chips', 'neutral'})
_TAG_PATTERN = re.compile(r'</?([a-zA-Z][a-zA-Z0-9]*)\b[^>]*>')


def scenario_html(value) -> str:
    """放行快照允许的标签，其余标签去掉标记保留文字。"""
    def keep(match):
        tag = match.group(1).lower()
        if tag not in SCENARIO_INLINE_TAGS:
            return ''
        return '</%s>' % tag if match.group(0).startswith('</') else '<%s>' % tag
    return _TAG_PATTERN.sub(keep, str(value or ''))


def plain(value) -> str:
    """取纯文本：去标记、还原实体、压平空白，用于标题与 meta 描述。"""
    text = unescape(_TAG_PATTERN.sub(' ', str(value or '')))
    return re.sub(r'\s+', ' ', text).strip()


@lru_cache(maxsize=32)
def _template(root: str, name: str):
    path = Path(root) / 'templates' / (name + '.html')
    return web.template.Template(path.read_text(encoding='utf-8'), filename=str(path))


@lru_cache(maxsize=4)
def scenario_document(root: str):
    """Platform scenario catalog; see tools/check_scenarios.py."""
    return json.loads((Path(root) / 'scenarios.json').read_text(encoding='utf-8'))


@lru_cache(maxsize=4)
def scenario_document_pages(root: str):
    """Platform scenario guides and demos; see tools/check_scenario_docs.py.

    The file is a build-time artefact, so a missing or corrupt snapshot must not
    surface as a stack trace: callers treat an empty document as "no content".
    """
    try:
        return json.loads((Path(root) / 'scenario_docs.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


@lru_cache(maxsize=4)
def site_config(root: str = ''):
    """站点配置（含站外链接白名单）。"""
    return json.loads((Path(root or resource_root()) / 'config.json').read_text(encoding='utf-8'))


def link_allowed(target: str, config=None) -> bool:
    """站内地址、无协议的相对地址，或 config.external_links 登记的协议/主机。"""
    value = (target or '').strip()
    if not value or value.startswith(('#', '/', '?')):
        return True
    parsed = urlsplit(value)
    if not parsed.scheme:
        return True
    links = (config or site_config()).get('external_links', {})
    if parsed.scheme in links.get('schemes', []):
        return True
    return parsed.hostname in links.get('hosts', [])


class HelpView:
    """Each request owns its language, query, metadata and content helpers."""

    def __init__(self, page='index', lang='zh', query=None, base='/help', origin=''):
        self.page = page
        self.lang = lang if lang in ('zh', 'en') else 'zh'
        self.query = dict(query or {})
        self.base = base.rstrip('/')
        self.origin = origin.rstrip('/')
        self.config = read_json('config.json')
        self.contents = read_json('content.json')
        self.translations = read_json('lang/' + self.lang + '.json')
        self.defaults = self.translations if self.lang == 'zh' else read_json('lang/zh.json')
        self.icons = read_json('icons.json')
        docs = read_json('docs/manifest.json')['docs']
        self.docs = dict(sorted(docs.items(), key=lambda entry: entry[1].get('order', 0)))
        self.slug = self.query.get('p', '').strip()
        kind = self.query.get('k', '').strip()
        # 空 kind 走默认分页；非法 kind 保持原样，交给 scenario_page_exists 判成 404。
        self.kind = kind or SCENARIO_DOC_KIND
        self.ok = self.page != 'doc' or self.doc_exists(self.slug)
        if self.page == 'scenario_doc':
            self.ok = self.scenario_page_exists(self.slug, self.kind)
        self.brand = self.cfg('brand', {})
        title_key = self.cfg('page_titles', {}).get(page, 'meta.title_home')
        self.title = self.t(title_key) + ' · ' + self.brand['name']
        lead_keys = {'features': 'capabilities.subtitle', 'enterprise': 'enterprise.banner_desc',
                     'quickstart': 'quickstart.subtitle', 'manual': 'manual.lead',
                     'scenarios': 'scenarios.lead',
                     'architecture': 'architecture.subtitle', 'about': 'about.lead'}
        self.description = self.t(lead_keys.get(page, 'meta.description'))
        if page == 'doc':
            self.title = ((self.doc_title(self.slug) + ' · ' + self.t('doc.breadcrumb'))
                          if self.ok else self.t('doc.not_found')) + ' · ' + self.brand['name']
            self.description = self.doc_lead(self.slug) if self.ok else self.t('doc.not_found_desc')
        if page == 'scenario_doc':
            page_doc = self.scenario_page(self.slug, self.kind)
            name = self.scenario_doc_meta(self.slug).get('name') or self.slug
            self.title = ((name + ' · ' + self.t('scenario_doc.kinds.' + self.kind))
                          if self.ok else self.t('scenario_doc.not_found')) + ' · ' + self.brand['name']
            self.description = self.plain(page_doc.get('lead', '')) if self.ok else self.t('scenario_doc.not_found_desc')

    def render(self, name=None):
        return str(_template(str(resource_root()), name or self.page)(self))

    def cfg(self, key, default=None):
        return lookup(self.config, key, default)

    def content(self, key, default=None):
        return self.contents.get(key, [] if default is None else default)

    def translated(self, key, default=None):
        return lookup(self.translations, key, lookup(self.defaults, key, default))

    def t(self, key, fallback=''):
        value = self.translated(key)
        return str(value) if value and not isinstance(value, (list, dict)) else (fallback or key)

    def t_opt(self, key):
        value = self.translated(key)
        return str(value) if value and not isinstance(value, (list, dict)) else ''

    def t_list(self, key):
        value = self.translated(key, [])
        return list(value.values()) if isinstance(value, dict) else value if isinstance(value, list) else []

    def t_map(self, key):
        value = self.translated(key, {})
        return value if isinstance(value, dict) else {}

    def url(self, page='index', params=None):
        parsed = urlsplit(page)
        name = parsed.path.removesuffix('.php').strip('/')
        name = '' if name == 'index' else name
        values = dict(parse_qsl(parsed.query))
        values.update(params or {})
        if self.lang != 'zh':
            values.setdefault('lang', self.lang)
        if name == 'scenario_doc':
            slug = values.pop('p', self.slug)
            kind = values.pop('k', self.kind)
            path = self.base + '/scenario/' + slug
            if kind and kind != SCENARIO_DOC_KIND:
                path += '/' + kind
        else:
            path = self.base + '/' + name
        return path + ('?' + urlencode(values) if values else '') + ('#' + parsed.fragment if parsed.fragment else '')

    def asset(self, path):
        return self.base + '/' + path.lstrip('/') + '?v=' + str(self.cfg('version', '1'))

    def lang_url(self, lang):
        return self.url(self.page, {**self.query, 'lang': lang})

    def canonical(self):
        params = {k: v for k, v in self.query.items() if k != 'lang'}
        if self.page == 'scenario_doc':
            path = self.base + '/scenario/' + self.slug
            return path + ('/' + self.kind if self.kind != SCENARIO_DOC_KIND else '')
        path = self.base + '/' + ('' if self.page == 'index' else self.page)
        return path + ('?' + urlencode(params) if params else '')

    def demo_image(self):
        return self.asset(self.cfg('demo_image', ''))

    def icon(self, name, css='icon'):
        return (f'<svg class="{e(css)}" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
                'stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" '
                'aria-hidden="true" focusable="false">' + self.icons.get(name, self.icons['sparkles']) + '</svg>')

    def tag(self, label, modifier=''):
        return f'<span class="tag{e(" tag--" + modifier) if modifier else ""}">{e(label)}</span>'

    def section_heading(self, title, subtitle='', left=False):
        result = f'<h2 class="section-title{" section-title--left" if left else ""}">{e(self.t(title))}</h2>'
        if subtitle:
            result += f'<p class="section-subtitle{" section-subtitle--left" if left else ""}">{e(self.t(subtitle))}</p>'
        return result

    def page_hero(self, title, lead, opts=None):
        opts = opts or {}
        extra = opts.get('extra', '')
        result = f'<header class="page-hero{" page-hero--rich" if extra else ""}"><div class="section-container">'
        breadcrumb = opts.get('breadcrumb', title)
        if breadcrumb:
            result += (f'<nav class="breadcrumb" aria-label="breadcrumb"><a href="{e(self.url())}">{e(self.t("nav.home"))}</a>'
                       f'<span aria-hidden="true">/</span><span>{e(self.t(breadcrumb))}</span></nav>')
        if opts.get('eyebrow'):
            result += '<span class="perm-eyebrow">' + self.icon(opts.get('eyebrow_icon', 'sparkles')) + e(self.t(opts['eyebrow'])) + '</span>'
        accent = self.t(opts['title_accent']) if opts.get('title_accent') else ''
        lines = []
        for line in self.t(title).split('\n'):
            lines.append(e(line).replace(e(accent), '<span class="hero-accent">' + e(accent) + '</span>', 1) if accent else e(line))
        return result + '<h1>' + '<br>'.join(lines) + '</h1><p>' + e(self.t(lead)) + '</p>' + extra + '</div></header>'

    def capability_card(self, item):
        key = 'capabilities.items.' + item['id']
        inner = ('<span class="feature-icon">' + self.icon(item['icon']) + '</span>'
                 + '<h3 class="feature-title">' + e(self.t(key + '.title')) + '</h3>'
                 + '<p class="feature-desc">' + e(self.t(key + '.desc')) + '</p>')
        if not item.get('doc'):
            return '<article class="feature-card reveal">' + inner + '</article>'
        return (f'<a class="feature-card feature-card--link reveal" href="{e(self.doc_url(item["doc"]))}">' + inner
                + '<span class="feature-more">' + e(self.t('common.read_doc')) + self.icon('arrow') + '</span></a>')

    def enterprise_card(self, item):
        key = 'enterprise.items.' + item['id']
        points = self.t_list(key + '.points')
        items = ''.join('<li>' + self.icon('check') + '<span>' + e(point) + '</span></li>' for point in points)
        return ('<article class="feature-card enterprise-card reveal"><span class="feature-icon">' + self.icon(item['icon'])
                + '</span><h3 class="feature-title">' + e(self.t(key + '.title')) + '</h3><p class="feature-desc">'
                + e(self.t(key + '.desc')) + '</p>' + ('<ul class="feature-points">' + items + '</ul>' if points else '') + '</article>')

    def copy_button(self):
        return (f'<button class="copy-btn" type="button" data-copy-label="{e(self.t("common.copy"))}" '
                f'data-copy-done="{e(self.t("common.copied"))}">{e(self.t("common.copy"))}</button>')

    def code_block(self, label, code):
        return ('<div class="code-block code-block--wide"><div class="code-header">'
                '<span class="code-dot red"></span><span class="code-dot yellow"></span><span class="code-dot green"></span>'
                f'<span class="code-label">{e(label)}</span>' + self.copy_button()
                + f'</div><pre class="code-content is-active"><code>{e(code)}</code></pre></div>')

    def deploy_block(self):
        tabs, panels = [], []
        declared = self.cfg('site_url', '').strip().rstrip('/')
        from channel.web.help_site import normalize_site_url
        base = normalize_site_url(declared).rstrip('/') or self.origin + self.base
        for index, item in enumerate(self.content('deployments')):
            ident = item['id']
            active = ' is-active' if index == 0 else ''
            command = self.cfg('deployments', {}).get(ident, '').replace('{site_url}', base)
            prompt = '>' if ident == 'win' else '$'
            tabs.append(f'<button class="code-tab{active}" type="button" data-code-tab="{e(ident)}">{e(self.t("quickstart.tab_" + ident))}</button>')
            code = '\n'.join(f'<span class="code-prompt">{e(prompt)}</span> {e(line)}' for line in command.split('\n'))
            panels.append(f'<pre class="code-content{active}" data-code-panel="{e(ident)}"><code><span class="code-comment">'
                          + e('# ' + self.t('quickstart.comment_' + ident)) + '</span>\n' + code + '</code></pre>')
        return ('<div class="code-block"><div class="code-header"><span class="code-dot red"></span>'
                '<span class="code-dot yellow"></span><span class="code-dot green"></span><div class="code-tabs">'
                + ''.join(tabs) + '</div>' + self.copy_button() + '</div>' + ''.join(panels) + '</div>')

    # ===== 应用场景（平台场景目录，清单唯一来源 webhelp/scenarios.json） =====

    def scenario_items(self):
        return list(scenario_document(str(resource_root())).get('items', []))

    def scenario_count(self):
        return len(self.scenario_items())

    def scenario_source_url(self):
        return str(scenario_document(str(resource_root())).get('source', ''))

    def scenario_role_filters(self):
        """岗位筛选标签：顺序对齐源站 ROLES，只保留本目录实际出现的岗位，并附带数量。"""
        preferred = ('企业负责人', '管理层', '财务', 'HR', '销售', 'PMC', '采购', '生产',
                     '研发', '客服', '运营', '行政', '法务', '产品', '市场', '投研', '教师')
        counts = {}
        for item in self.scenario_items():
            for role in item.get('roles') or []:
                if role:
                    counts[role] = counts.get(role, 0) + 1
        ordered = [{'id': role, 'count': counts[role]} for role in preferred if role in counts]
        extras = [{'id': role, 'count': counts[role]} for role in sorted(set(counts) - {r['id'] for r in ordered})]
        return ordered + extras

    def scenario_groups(self):
        """按数据文件声明的分组与顺序切分场景，空分组直接跳过。"""
        document = scenario_document(str(resource_root()))
        items = list(document.get('items', []))
        groups = []
        for index, group in enumerate(document.get('groups', [])):
            ident = group.get('id', '')
            members = [item for item in items if item.get('group') == ident]
            if not members:
                continue
            groups.append({'id': ident, 'items': members, 'count': len(members),
                           'modifier': 'section section--soft' if index % 2 else 'section'})
        return groups

    def scenario_agent_key(self, slug):
        """帮助站一键体验对应的智能体键：安装后多为「键」或「键-租户码」。"""
        if not slug:
            return ''
        if slug == 'rfq-quote':
            return 'rfq-quote'
        return 'wb-' + slug

    def scenario_card(self, item):
        roles = list(item.get('roles') or [])
        role_tags = ''.join(self.tag(role) for role in roles)
        role_attr = ' '.join(e(role) for role in roles)
        group_label = self.t('scenarios.groups.' + str(item.get('group', '')))
        search_bits = [item.get('name', ''), item.get('pain', ''), item.get('industry', ''),
                       group_label, ' '.join(roles)]
        search_attr = e(' '.join(bit for bit in search_bits if bit).lower())
        out = ('<article class="feature-card feature-card--compact scenario-card reveal"'
               ' data-scenario-roles="' + role_attr + '"'
               ' data-scenario-search="' + search_attr + '">'
               '<span class="scenario-ribbon">' + e(self.t('scenarios.try')) + '</span>'
               '<div class="scenario-meta" aria-label="' + e(self.t('scenarios.roles_label')) + '">'
               + '<span class="tag tag--enterprise">' + e(group_label) + '</span>' + role_tags + '</div>'
               '<h3 class="feature-title">' + e(item.get('name', '')) + '</h3>')
        if item.get('pain'):
            out += ('<p class="scenario-pain"><b>' + e(self.t('scenarios.pain_label')) + '</b>'
                    + e(item['pain']) + '</p>')
        details = ''
        if item.get('desc'):
            details += '<p class="scenario-desc">' + e(item['desc']) + '</p>'
        if item.get('case_hint'):
            details += ('<p class="scenario-hint"><b>' + e(self.t('scenarios.hint_label')) + '</b>'
                        + e(item['case_hint']) + '</p>')
        if details:
            out += ('<details class="scenario-more"><summary><span>' + e(self.t('scenarios.explain_label'))
                    + '</span>' + self.icon('arrow') + '</summary><div class="scenario-more-body">'
                    + details + '</div></details>')
        actions = ''
        if self.scenario_agent_key(item.get('slug', '')):
            actions += ('<button class="btn btn-primary btn-sm" type="button" data-scenario-try="'
                        + e(item['slug']) + '">' + e(self.t('scenarios.try')) + '</button>')
        if item.get('detail_url'):
            actions += ('<a class="btn btn-secondary btn-sm" href="' + e(self.scenario_page_url(item['slug']))
                        + '">' + e(self.t('scenarios.detail')) + '</a>')
        if actions:
            out += '<div class="scenario-actions">' + actions + '</div>'
        return out + '</article>'

    def scenario_payload_block(self):
        """一键体验：跳转控制台打开对应智能体，并带上初始消息。"""
        document = scenario_document(str(resource_root()))
        agents = {}
        for item in document.get('items', []):
            slug = item.get('slug', '')
            key = self.scenario_agent_key(slug)
            if slug and key:
                agents[slug] = key
        payload = json.dumps({
            'agents': agents,
            'message': self.t('scenarios.try_message'),
            'open_path': '/',
        }, ensure_ascii=False).replace('<', '\\u003c')
        return '<script type="application/json" data-scenario-data>' + payload + '</script>'

    # ===== 场景详情（本地快照，唯一来源 webhelp/scenario_docs.json） =====

    def scenario_snapshot(self):
        return scenario_document_pages(str(resource_root()))

    def scenario_catalog(self):
        value = self.scenario_snapshot().get('catalog', {})
        return value if isinstance(value, dict) else {}

    def scenario_doc_pages(self):
        value = self.scenario_snapshot().get('items', {})
        return value if isinstance(value, dict) else {}

    def scenario_doc_meta(self, slug):
        entry = self.scenario_catalog().get(slug, {})
        return entry if isinstance(entry, dict) else {}

    def scenario_page(self, slug, kind):
        kind = kind or SCENARIO_DOC_KIND
        entry = self.scenario_doc_pages().get(slug)
        page = entry.get(kind) if isinstance(entry, dict) else None
        return page if isinstance(page, dict) else {}

    def scenario_page_exists(self, slug, kind):
        if not slug or not re.fullmatch(r'[a-z0-9-]+', slug):
            return False
        return bool(self.scenario_page(slug, kind).get('blocks'))

    def scenario_page_url(self, slug, kind=''):
        return self.url('scenario_doc', {'p': slug, 'k': kind or SCENARIO_DOC_KIND})

    def scenario_ordered_slugs(self):
        return [item['slug'] for item in self.scenario_items() if item.get('slug') in self.scenario_doc_pages()]

    def scenario_neighbor(self, offset):
        slugs = self.scenario_ordered_slugs()
        if self.slug not in slugs:
            return {}
        index = slugs.index(self.slug) + offset
        if not 0 <= index < len(slugs):
            return {}
        slug = slugs[index]
        return {'slug': slug, 'name': self.scenario_doc_meta(slug).get('name', slug)}

    def scenario_doc_tabs(self):
        tabs = ''
        for kind in SCENARIO_KINDS:
            if not self.scenario_page_exists(self.slug, kind):
                continue
            current = kind == self.kind
            tabs += ('<a class="sdoc-tab' + (' is-current' if current else '') + '" href="'
                     + e(self.scenario_page_url(self.slug, kind)) + '"'
                     + (' aria-current="page"' if current else '') + '>'
                     + e(self.t('scenario_doc.kinds.' + kind)) + '</a>')
        return '<nav class="sdoc-tabs" aria-label="' + e(self.t('scenario_doc.tabs_label')) + '">' + tabs + '</nav>'

    def scenario_doc_actions(self):
        out = '<div class="scenario-actions scenario-actions--doc">'
        if self.scenario_agent_key(self.slug):
            out += ('<button class="btn btn-primary btn-sm" type="button" data-scenario-try="' + e(self.slug)
                    + '">' + e(self.t('scenarios.try')) + '</button>')
        out += ('<a class="btn btn-secondary btn-sm" href="' + e(self.url('scenarios')) + '">'
                + e(self.t('scenario_doc.back')) + '</a></div>')
        return out

    def scenario_doc_hero(self):
        page = self.scenario_page(self.slug, self.kind)
        name = self.scenario_doc_meta(self.slug).get('name') or self.slug
        out = ('<header class="page-hero page-hero--doc"><div class="section-container">'
               '<nav class="breadcrumb" aria-label="breadcrumb"><a href="' + e(self.url()) + '">' + e(self.t('nav.home'))
               + '</a><span aria-hidden="true">/</span><a href="' + e(self.url('scenarios')) + '">'
               + e(self.t('scenarios.title')) + '</a><span aria-hidden="true">/</span><span>' + e(name) + '</span></nav>'
               '<h1>' + e(name) + '</h1>')
        if page.get('lead'):
            out += '<p class="sdoc-lead">' + self.scenario_html(page['lead']) + '</p>'
        return out + self.scenario_doc_tabs() + self.scenario_doc_actions() + '</div></header>'

    def scenario_doc_toc(self, body):
        """目录直接从渲染后的正文里取，锚点与标题天然一致，不会各走一套编号。"""
        entries = []
        for level, ident, inner in re.findall(
                r'<h([234]) class="sdoc-heading[^"]*" id="([^"]+)">(.*?)</h[234]>', body, re.S):
            text = plain(inner)
            if text:
                entries.append({'level': int(level), 'id': ident, 'text': text})
        if len(entries) < 2:
            return ''
        out = '<details class="doc-toc" open><summary>' + e(self.t('scenario_doc.toc')) + '</summary><ol>'
        for entry in entries:
            out += ('<li class="doc-toc-item doc-toc-item--h%d"><a href="#%s">%s</a></li>'
                    % (entry['level'], e(entry['id']), e(entry['text'])))
        return out + '</ol></details>'

    def scenario_blocks(self, blocks, prefix='b'):
        out = []
        for index, block in enumerate(blocks or []):
            if isinstance(block, dict):
                out.append(self.scenario_block(block, '%s-%d' % (prefix, index)))
        return ''.join(out)

    def scenario_block(self, block, anchor=''):
        kind = str(block.get('type', ''))
        if kind not in SCENARIO_PRIMITIVES:
            kind = 'neutral'
        return getattr(self, 'scenario_block_' + kind)(block, anchor)

    def scenario_html(self, value):
        return scenario_html(value)

    def plain(self, value):
        return plain(value)

    def scenario_block_heading(self, block, anchor=''):
        level = min(4, max(2, int(block.get('level', 2))))
        number = block.get('n', '')
        out = ('<h%d class="sdoc-heading sdoc-heading--h%d" id="%s">' % (level, level, e(anchor)))
        if number:
            out += '<span class="sdoc-heading-n">' + e(number) + '</span>'
        out += '<span class="sdoc-heading-text">' + self.scenario_html(block.get('title', '')) + '</span></h%d>' % level
        if block.get('subline'):
            out += '<p class="sdoc-subline">' + self.scenario_html(block['subline']) + '</p>'
        return out

    def scenario_block_prose(self, block, anchor=''):
        return '<div class="sdoc-prose">' + self.scenario_html(block.get('html', '')) + '</div>'

    def scenario_block_neutral(self, block, anchor=''):
        return '<div class="sdoc-prose">' + self.scenario_html(block.get('html', '')) + '</div>'

    def scenario_block_note(self, block, anchor=''):
        tone = str(block.get('tone', 'info'))
        if tone not in SCENARIO_NOTE_TONES:
            tone = 'info'
        out = '<aside class="sdoc-note sdoc-note--' + tone + '">'
        if block.get('title'):
            out += '<p class="sdoc-note-title">' + self.scenario_html(block['title']) + '</p>'
        return out + '<div class="sdoc-note-body">' + self.scenario_html(block.get('html', '')) + '</div></aside>'

    def scenario_block_cta(self, block, anchor=''):
        out = '<div class="sdoc-cta">'
        if block.get('title'):
            out += '<h3 class="sdoc-cta-title">' + self.scenario_html(block['title']) + '</h3>'
        return out + '<div class="sdoc-cta-body">' + self.scenario_html(block.get('html', '')) + '</div></div>'

    def scenario_block_pains(self, block, anchor=''):
        out = '<ol class="sdoc-pains">'
        for item in block.get('items', []):
            out += ('<li class="sdoc-pain"><span class="sdoc-pain-n">' + e(item.get('n', '')) + '</span>'
                    '<div class="sdoc-pain-body"><p class="sdoc-pain-text">' + self.scenario_html(item.get('html', '')) + '</p>')
            if item.get('cite'):
                out += '<p class="sdoc-pain-cite">' + self.scenario_html(item['cite']) + '</p>'
            out += '</div></li>'
        return out + '</ol>'

    def scenario_block_compare(self, block, anchor=''):
        sides = ''
        for side_key in ('old', 'new'):
            side = block.get(side_key) or {}
            sides += ('<div class="sdoc-compare-side sdoc-compare-side--' + side_key + '">'
                      '<h4 class="sdoc-compare-title">' + self.scenario_html(side.get('title', '')) + '</h4>'
                      '<div class="sdoc-compare-body">' + self.scenario_html(side.get('html', '')) + '</div></div>')
        return '<div class="sdoc-compare">' + sides + '</div>'

    def scenario_block_cards(self, block, anchor=''):
        columns = block.get('columns')
        modifier = ' sdoc-cards--c%d' % columns if isinstance(columns, int) and 2 <= columns <= 5 else ''
        out = '<div class="sdoc-cards' + modifier + '">'
        for item in block.get('items', []):
            tone = str(item.get('tone', ''))
            card = '<article class="sdoc-card' + (' sdoc-card--' + e(tone) if tone else '') + '">'
            if item.get('kicker'):
                card += '<span class="sdoc-card-kicker">' + self.scenario_html(item['kicker']) + '</span>'
            if item.get('title'):
                card += '<h4 class="sdoc-card-title">' + self.scenario_html(item['title']) + '</h4>'
            if item.get('meta'):
                card += '<span class="sdoc-card-meta">' + self.scenario_html(item['meta']) + '</span>'
            out += card + '<div class="sdoc-card-body">' + self.scenario_html(item.get('html', '')) + '</div></article>'
        return out + '</div>'

    def scenario_block_steps(self, block, anchor=''):
        out = '<ol class="sdoc-steps">'
        for item in block.get('items', []):
            out += ('<li class="sdoc-step"><span class="sdoc-step-n">' + e(item.get('n', '')) + '</span>'
                    '<span class="sdoc-step-copy"><b class="sdoc-step-title">' + self.scenario_html(item.get('title', ''))
                    + '</b><span class="sdoc-step-desc">' + self.scenario_html(item.get('desc', '')) + '</span></span></li>')
        return out + '</ol>'

    def scenario_block_demo_steps(self, block, anchor=''):
        out = '<div class="sdoc-dsteps">'
        for index, item in enumerate(block.get('items', [])):
            out += ('<section class="sdoc-dstep" id="' + e(anchor) + '-s%d">' % index
                    + '<header class="sdoc-dstep-head">')
            if item.get('n'):
                out += '<span class="sdoc-dstep-n">' + e(item['n']) + '</span>'
            out += '<div class="sdoc-dstep-titlewrap"><h3 class="sdoc-dstep-title">' + self.scenario_html(item.get('title', '')) + '</h3>'
            if item.get('subtitle'):
                out += '<p class="sdoc-dstep-subtitle">' + self.scenario_html(item['subtitle']) + '</p>'
            out += '</div>'
            for chip in item.get('meta', []):
                out += '<span class="sdoc-dstep-meta">' + self.scenario_html(chip) + '</span>'
            out += ('</header><div class="sdoc-dstep-body">'
                    + self.scenario_blocks(item.get('blocks', []), '%s-s%d' % (anchor, index)) + '</div></section>')
        return out + '</div>'

    def scenario_block_outputs(self, block, anchor=''):
        out = '<div class="sdoc-outputs">'
        for item in block.get('items', []):
            out += ('<article class="sdoc-output">'
                    + ('<span class="sdoc-output-kind">' + self.scenario_html(item['kind']) + '</span>' if item.get('kind') else '')
                    + ('<h4 class="sdoc-output-file">' + self.scenario_html(item['file']) + '</h4>' if item.get('file') else '')
                    + ('<p class="sdoc-output-desc">' + self.scenario_html(item['desc']) + '</p>' if item.get('desc') else '')
                    + '</article>')
        return out + '</div>'

    def scenario_block_files(self, block, anchor=''):
        out = '<section class="sdoc-files">'
        if block.get('title'):
            out += '<h4 class="sdoc-files-title">' + self.scenario_html(block['title']) + '</h4>'
        out += '<ul class="sdoc-files-list">'
        for item in block.get('items', []):
            out += ('<li class="sdoc-file"><span class="sdoc-file-name">' + self.scenario_html(item.get('name', ''))
                    + '</span><span class="sdoc-file-desc">' + self.scenario_html(item.get('desc', '')) + '</span></li>')
        return out + '</ul></section>'

    def scenario_block_score(self, block, anchor=''):
        return '<div class="sdoc-prose sdoc-score">' + self.scenario_html(block.get('html', '')) + '</div>'

    def scenario_block_tasks(self, block, anchor=''):
        out = ('<ol class="sdoc-tasks" data-copy-label="' + e(self.t('common.copy'))
               + '" data-copy-done="' + e(self.t('common.copied')) + '">')
        for item in block.get('items', []):
            out += ('<li class="sdoc-task" id="' + e(anchor) + '-t' + e(str(item.get('n', ''))) + '">'
                    '<div class="sdoc-task-head"><span class="sdoc-task-n">' + e(item.get('n', '')) + '</span>'
                    '<h4 class="sdoc-task-title">' + self.scenario_html(item.get('title', '')) + '</h4>')
            if item.get('star'):
                out += '<span class="sdoc-task-star">' + self.scenario_html(item['star']) + '</span>'
            out += ('<button class="copy-btn copy-btn--inline" type="button" data-sdoc-copy="' + e(str(item.get('n', '')))
                    + '" data-copy-label="' + e(self.t('common.copy')) + '" data-copy-done="' + e(self.t('common.copied'))
                    + '">' + e(self.t('common.copy')) + '</button></div>'
                    '<p class="sdoc-task-body">' + self.scenario_html(item.get('html', '')) + '</p></li>')
        return out + '</ol>'

    def scenario_block_table(self, block, anchor=''):
        head = block.get('head') or []
        highlight = block.get('highlight')
        out = '<div class="doc-table-wrap"><table class="sdoc-table">'
        if head:
            out += '<thead><tr>'
            for index, cell in enumerate(head):
                current = ' class="is-current"' if index == highlight else ''
                out += '<th scope="col"' + current + '>' + self.scenario_html(cell) + '</th>'
            out += '</tr></thead>'
        out += '<tbody>'
        for row in block.get('rows', []):
            out += '<tr>' + ''.join('<td>' + self.scenario_html(cell) + '</td>' for cell in row) + '</tr>'
        return out + '</tbody></table></div>'

    def scenario_block_chips(self, block, anchor=''):
        out = '<div class="sdoc-chips">'
        if block.get('label'):
            out += '<span class="sdoc-chips-label">' + self.scenario_html(block['label']) + '</span>'
        for item in block.get('items', []):
            out += '<span class="sdoc-chip">' + self.scenario_html(item) + '</span>'
        return out + '</div>'

    def scenario_doc_body(self):
        page = self.scenario_page(self.slug, self.kind)
        return self.scenario_blocks(page.get('blocks', []))

    def scenario_doc_nav(self):
        pages = self.scenario_doc_pages()
        out = '<p class="doc-aside-title">' + e(self.t('scenario_doc.aside_title')) + '</p>'
        for group in self.scenario_groups():
            members = [item for item in group['items'] if item.get('slug') in pages]
            if not members:
                continue
            out += ('<p class="doc-aside-section">' + e(self.t('scenarios.groups.' + group['id'])) + '</p>'
                    '<ul class="doc-aside-list">')
            for item in members:
                current = item['slug'] == self.slug
                out += ('<li><a class="doc-aside-link' + (' is-active' if current else '')
                        + '" href="' + e(self.scenario_page_url(item['slug'])) + '"'
                        + (' aria-current="page"' if current else '') + '>' + e(item.get('name', '')) + '</a></li>')
            out += '</ul>'
        return out

    def scenario_doc_footer_nav(self):
        previous, following = self.scenario_neighbor(-1), self.scenario_neighbor(1)
        if not previous and not following:
            return ''
        out = '<nav class="doc-pager">'
        for link, label, modifier, arrow in ((previous, 'prev', '--prev', self.icon('arrow')),
                                             (following, 'next', '--next', self.icon('arrow'))):
            if link:
                out += ('<a class="doc-pager-link doc-pager-link' + modifier + '" href="'
                        + e(self.scenario_page_url(link['slug'])) + '">'
                        + '<span class="doc-pager-label">' + e(self.t('scenario_doc.' + label)) + '</span>'
                        + '<span class="doc-pager-title">' + e(link['name']) + '</span>' + arrow + '</a>')
            else:
                out += '<span class="doc-pager-link doc-pager-link' + modifier + ' is-empty"></span>'
        return out + '</nav>'

    def perm_stats(self):
        return '<div class="perm-stats">' + ''.join(f'<div class="perm-stat reveal"><strong>{e(stat.get("value", ""))}</strong><span>{e(stat.get("label", ""))}</span></div>' for stat in self.t_list('perm.stats')) + '</div>'

    def perm_tiers(self):
        out = []
        for item in self.content('perm_tiers'):
            ident = item['id']; key = 'perm.tiers.' + ident
            actions = ''.join('<li>' + e(a) + '</li>' for a in self.t_list(key + '.actions'))
            out.append(f'<article class="perm-tier perm-tier--{e(ident)} reveal"><span class="perm-tier-badge">'
                       + e(self.t(key + '.name')) + '</span><span class="perm-tier-icon">' + self.icon(item['icon'])
                       + '</span><h3 class="perm-tier-actor">' + e(self.t(key + '.actor')) + '</h3><p class="perm-tier-actordesc">'
                       + e(self.t(key + '.actor_desc')) + '</p><div class="perm-tier-field"><span class="perm-tier-label">'
                       + e(self.t('perm.labels.scope')) + '</span><p class="perm-tier-scope">' + e(self.t(key + '.scope'))
                       + '</p></div><div class="perm-tier-field"><span class="perm-tier-label">' + e(self.t('perm.labels.actions'))
                       + '</span><ul class="perm-tier-actions">' + actions + '</ul></div></article>')
        return '<div class="perm-tiers">' + ''.join(out) + '</div>'

    def perm_flow_panel(self):
        graph = self.t_map('perm.flow_graph'); desc = graph.get('actor_desc', {})
        icons = {'platform': 'tenant', 'tenant': 'rbac', 'user': 'access'}
        def node(tier):
            return (f'<div class="perm-node perm-node--{tier}"><span class="perm-node-icon">' + self.icon(icons[tier])
                    + '</span><span class="perm-node-name">' + e(self.t('perm.tiers.' + tier + '.actor'))
                    + '</span><span class="perm-node-desc">' + e(desc.get(tier, '')) + '</span></div>')
        def arrow(direction, key):
            return f'<div class="perm-arrow perm-arrow--{direction}"><span class="perm-arrow-label">{e(graph.get(key, ""))}</span><span class="perm-arrow-line" aria-hidden="true"></span></div>'
        return ('<div class="perm-flow reveal"><div class="perm-flow-grid">' + node('platform') + arrow('right', 'grant_platform')
                + node('tenant') + arrow('down', 'grant_tenant') + node('user') + '</div><div class="perm-return">'
                + '<span class="perm-return-text">' + e(graph.get('share_out', '')) + '</span><span class="perm-return-arrow" aria-hidden="true"></span>'
                + '<span class="perm-return-text">' + e(graph.get('share_in', '')) + '</span></div></div>')

    def perm_roles(self):
        out = []
        for item in self.content('perm_roles'):
            ident = item['id']; key = 'perm.roles.' + ident
            groups = ''.join('<div class="perm-role-group"><h4 class="perm-role-grouptitle">' + e(group.get('title', ''))
                             + '</h4><ul class="perm-role-list">' + ''.join('<li>' + e(entry) + '</li>' for entry in group.get('items', []))
                             + '</ul></div>' for group in self.t_list(key + '.groups'))
            out.append(f'<article class="perm-role perm-role--{e(ident)} reveal"><header class="perm-role-head"><span class="perm-role-icon">'
                       + self.icon(item['icon']) + '</span><div class="perm-role-id"><h3 class="perm-role-name">' + e(self.t(key + '.name'))
                       + '</h3><p class="perm-role-desc">' + e(self.t(key + '.desc')) + '</p></div><span class="perm-role-tag">'
                       + e(self.t(key + '.tag')) + '</span></header><div class="perm-role-groups">' + groups + '</div></article>')
        return '<div class="perm-roles">' + ''.join(out) + '</div>'

    def doc_exists(self, slug):
        return bool(re.fullmatch(r'[a-z0-9-]+', slug) and slug in self.docs and (resource_root() / 'docs' / (slug + '.html')).is_file())

    def doc_title(self, slug):
        titles = self.docs.get(slug, {}).get('title', {})
        return titles.get(self.lang, titles.get('zh', slug))

    def doc_lead(self, slug):
        return self.docs.get(slug, {}).get('lead', '')

    def doc_url(self, slug):
        return self.url('doc', {'p': slug})

    def doc_grouped(self):
        groups = {}
        for slug, meta in self.docs.items():
            groups.setdefault(meta.get('section', 'intro'), {})[slug] = meta
        return groups

    def doc_neighbor(self, slug, offset):
        ids = list(self.docs)
        if slug not in ids: return ''
        index = ids.index(slug) + offset
        return ids[index] if 0 <= index < len(ids) else ''

    def doc_body(self, slug):
        if not self.doc_exists(slug): return ''
        body = (resource_root() / 'docs' / (slug + '.html')).read_text(encoding='utf-8')
        body = re.sub(r'src="(assets/[^\"]*)"', lambda m: 'src="' + e(self.base + '/' + unescape(m[1])) + '"', body)
        body = re.sub(r'href="(doc(?:\.php)?\?[^\"]*)"', lambda m: 'href="' + e(self.url(unescape(m[1]))) + '"', body)
        if body.count('<table>') == body.count('</table>'):
            body = body.replace('<table>', '<div class="doc-table-wrap"><table>').replace('</table>', '</table></div>')
        return body

    def doc_toc(self, body):
        return [{'level': int(level), 'id': unescape(ident), 'text': unescape(re.sub(r'<[^>]*>', '', text)).strip()}
                for level, ident, text in re.findall(r'<h([23])\b[^>]*\bid="([^"]+)"[^>]*>(.*?)</h[23]>', body, re.S)]

    def manual_nav(self):
        out = '<p class="doc-aside-title">' + e(self.t('manual.toc')) + '</p>'
        for part in self.content('manual_parts'):
            topics = [(n, topic) for n, topic in enumerate(self.content('manual_topics')) if topic.get('part', 'conversation') == part['id']]
            if not topics: continue
            out += '<p class="doc-aside-section">' + e(self.t('manual.parts.' + part['id'])) + '</p><ul class="doc-aside-list">'
            for n, topic in topics:
                out += f'<li><a class="doc-aside-link" href="#manual-{e(topic["id"])}">{n + 1}. {e(self.t("manual.topics." + topic["id"] + ".nav"))}</a></li>'
            out += '</ul>'
        return out

    def manual_step(self, topic, number, step):
        key = 'manual.topics.' + topic + '.steps.' + step['id']
        title = self.t_opt(key + '.title').strip()
        body = self.t_opt(key + '.body').strip()
        out = (f'<figure class="manual-step" id="manual-{e(topic)}-{e(step["id"])}"><figcaption class="manual-step-head">'
               f'<span class="manual-step-no" aria-hidden="true">{number}</span><span class="manual-step-copy">'
               f'<span class="manual-step-title">{e(title)}</span><span class="manual-step-body">{e(body)}</span></span></figcaption>')
        if step.get('shot'):
            path = e(self.asset(step['shot']))
            out += f'<a class="manual-shot" href="{path}" target="_blank" rel="noopener"><img class="manual-shot-img" src="{path}" alt="{e(title)}" loading="lazy" decoding="async"></a>'
        return out + '</figure>'

    def manual_refs(self, item):
        groups = [('manual.docs_label', item.get('docs', [])), ('manual.pages_label', item.get('links', []))]
        result = ''
        for label, refs in groups:
            links = ''
            for ref in refs:
                if label == 'manual.docs_label':
                    if not self.doc_exists(ref): continue
                    href, title = self.doc_url(ref), self.doc_title(ref)
                else:
                    pages = self.content('manual_page_links', {})
                    if ref not in pages: continue
                    href, title = self.url(pages[ref]), self.t('manual.page_links.' + ref, ref)
                links += f'<a class="manual-ref" href="{e(href)}">{e(title)}' + self.icon('arrow') + '</a>'
            if links:
                result += '<div class="manual-refs"><span class="manual-refs-label">' + e(self.t(label)) + '</span><div class="manual-refs-list">' + links + '</div></div>'
        return result

    def year(self):
        return date.today().year

    def escape(self, value):
        return e(value)
