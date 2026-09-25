#!/usr/bin/env python3
"""Extract the local scenario documents snapshot from the published WorkBuddy pages.

The three source families (``deep-dives`` 场景引入, ``-tasks`` 推荐任务 and
``live-demos`` Live Demo) are rendered from three different markup vocabularies
(106 / 20 / 236 distinct CSS classes). This script flattens all of them onto a
small set of content primitives that ``webhelp/site.py`` renders with the help
site's own components.

Two guarantees matter more than tidiness here:

* **Nothing is dropped.** Any structure the mapper does not recognise becomes a
  ``neutral`` block that keeps its visible text and inline emphasis. Losing copy
  is a bug, not a styling choice.
* **Only allowlisted inline markup survives.** ``b`` / ``strong`` / ``br`` are
  kept; ``a`` becomes plain text (the site ships no outbound links); every other
  tag is unwrapped to its text.

The snapshot also stores a per-document ``text`` baseline: the visible text of the
source page. ``webhelp/tools/check_scenario_docs.py`` compares the rendered page
against that baseline, which turns "did normalisation lose anything?" into a
mechanical, offline check instead of a manual review.

Usage::

    python3 extract_scenario_docs.py [--root ../../..] [--out webhelp/scenario_docs.json]
                                     [--cache DIR]
    python3 extract_scenario_docs.py --selftest DIR   # map a local corpus, no network

Network access is required to (re)capture. Re-capturing is a reviewed step, like
``capture_scenarios.py``: the output is committed and validated before release.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.request
from datetime import date
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin

UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/120 Safari/537.36')
SOURCE = 'https://www.workbuddy.link/p/ZljbYQzFpALFLcPwKPlct1'
EXTRACTOR = 'openspec/changes/add-local-scenario-docs/evidence/extract_scenario_docs.py'
KINDS = ('intro', 'tasks', 'demo')

PRIMITIVES = (
    'heading', 'prose', 'note', 'cta', 'pains', 'compare', 'cards', 'steps',
    'demo_steps', 'outputs', 'files', 'score', 'tasks', 'table', 'chips', 'neutral',
)

INLINE_OK = frozenset({'b', 'strong'})
VOID = frozenset({'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input',
                  'link', 'meta', 'param', 'source', 'track', 'wbr'})
# Which open elements a start tag implies closing, per HTML's optional-end rules.
CLOSE_ON_START = {
    'p': ('p',),
    'li': ('li',),
    'td': ('td', 'th'),
    'th': ('td', 'th'),
    'tr': ('td', 'th', 'tr'),
    'option': ('option',),
    'dt': ('dt', 'dd'),
    'dd': ('dt', 'dd'),
}

# Chrome whose text the site re-authors locally (the tab strip).
BASELINE_SKIP = frozenset({'subnav'})

# Grid containers that hold a row of titled cards, and their column counts.
GRIDS = frozenset({
    'g2', 'g3', 'g4', 'val4', 'disp4', 'grade5', 'cost4', 'cause4', 'verdict-grid',
    'action-grid', 'lv-grid', 'lv3', 'entry3', 'kpi-row', 'srcs',
})
COLUMN_HINTS = (('g2', 2), ('g3', 3), ('g4', 4), ('val4', 4), ('cost4', 4),
                ('cause4', 4), ('disp4', 4), ('grade5', 5), ('lv3', 3),
                ('entry3', 3), ('lv-grid', 3), ('kpi-row', 4))
TABLE_WRAPS = frozenset({'tbl', 'compare-t', 'audit-tbl'})
NOTE_CONTAINERS = (('callout', 'info'), ('notice', 'info'), ('think', 'think'),
                   ('prompt', 'prompt'))
CARD_TONES = ('pass', 'rw', 'hold', 'dev', 'rej', 'acc', 'lag', 'hunt', 'est',
              'ok', 'no', 'reuse', 'scrap', 'red', 'yellow', 'blue', 'amber',
              'p1', 'p2', 'p3', 'high', 'good', 'bad', 'save')
# Containers we are willing to walk into when they hold something recognisable.
CONTAINERS = frozenset({'div', 'section', 'article', 'aside', 'figure', 'main'})
# Leaf tags that carry prose rather than structure.
PROSE_TAGS = frozenset({'p', 'ul', 'ol', 'pre', 'blockquote', 'dl'})


# --------------------------------------------------------------------------- DOM

class TextNode:
    __slots__ = ('data',)

    def __init__(self, data):
        self.data = data


class Element:
    """Minimal DOM node. ``find*`` comes in class-flavoured and tag-flavoured form."""

    __slots__ = ('tag', 'classes', 'children')

    def __init__(self, tag, classes=()):
        self.tag = tag
        self.classes = set(classes)
        self.children = []

    def has(self, name):
        return name in self.classes

    def elements(self):
        return [c for c in self.children if isinstance(c, Element)]

    def find(self, cls, deep=True):
        for child in self.elements():
            if child.has(cls):
                return child
            if deep:
                found = child.find(cls)
                if found is not None:
                    return found
        return None

    def find_tag(self, tag, deep=True):
        for child in self.elements():
            if child.tag == tag:
                return child
            if deep:
                found = child.find_tag(tag)
                if found is not None:
                    return found
        return None

    def find_all(self, cls, deep=True):
        out = []
        for child in self.elements():
            if child.has(cls):
                out.append(child)
            if deep:
                out.extend(child.find_all(cls))
        return out

    def find_all_tags(self, tag, deep=True):
        out = []
        for child in self.elements():
            if child.tag == tag:
                out.append(child)
            if deep:
                out.extend(child.find_all_tags(tag))
        return out

    def by_tag(self, tag):
        return [c for c in self.elements() if c.tag == tag]

    def text(self):
        parts = []
        for child in self.children:
            parts.append(child.data if isinstance(child, TextNode) else child.text())
        return squash(' '.join(parts))


class Builder(HTMLParser):
    """Forgiving builder: recovers from unclosed <p>/<li> and stray end tags."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Element('#root')
        self.stack = [self.root]

    @staticmethod
    def _classes(attrs):
        for key, value in attrs:
            if key == 'class' and value:
                return value.split()
        return []

    def handle_starttag(self, tag, attrs):
        closes = CLOSE_ON_START.get(tag, ())
        while len(self.stack) > 1 and self.stack[-1].tag in closes:
            self.stack.pop()
        node = Element(tag, self._classes(attrs))
        self.stack[-1].children.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.stack[-1].children.append(Element(tag, self._classes(attrs)))

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        for depth in range(len(self.stack) - 1, 0, -1):
            if self.stack[depth].tag == tag:
                del self.stack[depth:]
                return

    def handle_data(self, data):
        self.stack[-1].children.append(TextNode(data))

    def handle_comment(self, data):
        return


def parse(html):
    builder = Builder()
    builder.feed(html)
    builder.close()
    return builder.root


# ------------------------------------------------------------------- text helpers

def squash(text):
    """Fold whitespace and drop the platform's zero-width markers."""
    return re.sub(r'\s+', ' ', text.replace('\ufeff', '').replace('\u200b', '')).strip()


def plain(html):
    return squash(re.sub(r'<[^>]*>', ' ', html))


def escape(text):
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def rich(node):
    """Serialise a subtree to allowlisted inline HTML; unwrap every other tag."""
    if node is None:
        return ''
    out = []
    for child in node.children:
        if isinstance(child, TextNode):
            out.append(escape(child.data))
            continue
        if child.tag == 'br':
            out.append('<br>')
            continue
        inner = rich(child)
        if not inner:
            continue
        out.append('<b>%s</b>' % inner if child.tag in INLINE_OK else inner)
    joined = re.sub(r'(?:<br>\s*)+', '<br>', ''.join(out))
    return re.sub(r'^(?:<br>)+|(?:<br>)+$', '', joined).strip()


def without(root, target):
    """Shallow copy of ``root`` with ``target`` removed anywhere underneath."""
    clone = Element(root.tag, root.classes)
    for child in root.children:
        if child is target:
            continue
        clone.children.append(without(child, target) if isinstance(child, Element) else child)
    return clone


def clean_html(raw):
    """Strip the publishing platform's annotations before parsing."""
    raw = re.sub(r'<!--.*?-->', '', raw, flags=re.S)
    raw = re.sub(r'<script.*?</script>', '', raw, flags=re.S)
    raw = re.sub(r'<style.*?</style>', '', raw, flags=re.S)
    raw = re.sub(r'\s+data-page-node-id="[^"]*"', '', raw)
    raw = re.sub(r'\s+data-pnid-children="[^"]*"', '', raw)
    return raw


def content_root(raw):
    """``div.wrap`` minus its footer, plus the footer element itself.

    The footer sits inside ``div.wrap`` on the deep-dive pages but beside it on
    the Live Demo pages, so it is located by tag and stripped from wherever it is.
    Its text is kept separately (``note``) and is deliberately not part of the
    ``text`` baseline, which only covers what the body renders.
    """
    root = parse(clean_html(raw))
    wrap = root.find('wrap')
    if wrap is None:
        return root, None
    footer = wrap.find_tag('footer') or root.find_tag('footer')
    if footer is not None and footer in wrap.children:
        wrap.children = [c for c in wrap.children if c is not footer]
    return wrap, footer


def first_of(node, classes):
    for name in classes:
        found = node.find(name)
        if found is not None:
            return found
    return None


# ---------------------------------------------------------------------- primitives

def table_block(table):
    head, rows, highlight = [], [], None
    thead = table.find_tag('thead')
    for cell in (thead if thead is not None else table).find_all_tags('th'):
        head.append(rich(cell))
        if cell.has('col-current'):
            highlight = len(head) - 1
    body = table.find_tag('tbody')
    for tr in (body if body is not None else table).find_all_tags('tr'):
        cells = tr.by_tag('td')
        if cells:
            rows.append([rich(cell) for cell in cells])
    if not head and not rows:
        return None
    return {'type': 'table', 'head': head, 'rows': rows, 'highlight': highlight}


def cards_block(grid):
    items = []
    for card in grid.elements():
        if card.tag not in ('div', 'article', 'section', 'figure'):
            continue
        heading = first_of(card, ('h', 'gt', 'vh', 'lt', 'ah', 'dp-h', 'en-c', 'ct-h',
                                  'cs-h', 'lv-h', 'et', 'tt', 'k', 'lbl'))
        if heading is None:
            heading = card.find_tag('h3') or card.find_tag('h4') or card.find_tag('h5')
        body = first_of(card, ('gd', 'vd', 'ld', 'dp-b', 'ed', 'ct-b', 'cs-b', 'bubble',
                               'dd', 'mt', 'od', 'v', 'd'))
        title = rich(heading)
        if body is not None:
            html = rich(body)
        else:
            html = rich(card)
            if title and html.startswith(title):
                html = html[len(title):].strip()
        item = {'title': title, 'html': html}
        kicker = first_of(card, ('kicker', 'lbl'))
        if kicker is not None and rich(kicker) != title:
            item['kicker'] = rich(kicker)
        meta = first_of(card, ('who', 'av'))
        if meta is not None:
            item['meta'] = rich(meta)
        tone = card_tone(card)
        if tone:
            item['tone'] = tone
        if not title and not html:
            continue
        items.append(item)
    if not items:
        return None
    return {'type': 'cards', 'columns': columns_of(grid), 'items': items}


def card_tone(card):
    for word in CARD_TONES:
        if card.has(word):
            return word
    return ''


def columns_of(grid):
    for name, count in COLUMN_HINTS:
        if grid.has(name):
            return count
    return None


def pain_block(node):
    items = []
    for pain in node.elements():
        if not pain.has('pain'):
            continue
        number = pain.find('i')
        body = pain.find('t') if pain.find('t') is not None else pain
        cite = ''
        spans = body.find_all('src')
        if spans:
            cite = rich(spans[0])
            body = without(body, spans[0])
        items.append({'n': plain(number.text()) if number is not None else str(len(items) + 1),
                      'html': rich(body), 'cite': cite})
    return {'type': 'pains', 'items': items} if items else None


def compare_block(node):
    old, new = node.find('old'), node.find('new')
    if old is None or new is None:
        return None
    return {'type': 'compare',
            'old': compare_side(old, '传统做法'),
            'new': compare_side(new, '本产品')}


def compare_side(node, fallback):
    heading = node.find_tag('h4') or node.find_tag('h3')
    return {'title': rich(heading) or fallback, 'html': list_html(node)}


def list_html(node):
    items = node.find_all_tags('li')
    if not items:
        return rich(node)
    return '<ul>%s</ul>' % ''.join('<li>%s</li>' % rich(li) for li in items)


def steps_block(node):
    items = []
    for step in node.find_all('step'):
        number = step.find('n')
        title = step.find_tag('b')
        desc = step.find_tag('span')
        if title is None and desc is None:
            continue
        items.append({'n': rich(number) if number is not None else str(len(items) + 1),
                      'title': rich(title), 'desc': rich(desc)})
    return {'type': 'steps', 'items': items} if items else None


def outputs_block(node):
    items = []
    for out in node.find_all('out'):
        items.append({'kind': rich(out.find('on')), 'file': rich(out.find('ot')),
                      'desc': rich(out.find('od'))})
    return {'type': 'outputs', 'items': items} if items else None


def files_block(node):
    items = []
    for src in node.find_all('src'):
        name, desc = src.find('fn'), src.find('fd')
        if name is None and desc is None:
            continue
        items.append({'name': rich(name), 'desc': rich(desc)})
    if not items:
        return None
    heading = node.find_tag('h3') or node.find_tag('h4')
    return {'type': 'files', 'title': rich(heading), 'items': items}


def chips_block(node):
    label = node.find('lbl')
    text = rich(label)
    items = [rich(span) for span in node.find_all_tags('span')]
    items = [value for value in items if value and value != text]
    if not items and not text:
        return None
    return {'type': 'chips', 'label': text, 'items': items}


def tasks_block(node):
    items = []
    for task in node.find_all('task'):
        head = task.find('th')
        scope = head if head is not None else task
        body = task.find('tp')
        items.append({'n': rich(scope.find('no')) or ('%02d' % (len(items) + 1)),
                      'title': rich(scope.find('ttl')),
                      'star': rich(scope.find('star')),
                      'html': rich(body) if body is not None else rich(task)})
    return {'type': 'tasks', 'items': items} if items else None


def demo_step(step):
    head = step.find('dstep-h')
    body = step.find('dstep-b')
    title_node = head.find_tag('h2') if head is not None else None
    sub = head.find('sd') if head is not None else None
    title = rich(title_node) if title_node is not None else rich(head)
    subtitle = rich(sub)
    return {'n': rich(head.find('dstep-n')) if head is not None else '',
            'title': title,
            'subtitle': subtitle,
            'meta': head_meta(head, title, subtitle),
            'blocks': sequence(body if body is not None else step)}


def head_meta(head, title, subtitle):
    """Text the step header carries besides its number, title and subtitle.

    Live Demo headers also hold a status chip (``✓ 已完成``) and occasionally an
    owner tag. They are collected generically so that an unmodelled chip shows up
    as metadata instead of being dropped.
    """
    if head is None:
        return []
    out = []
    for node in head.find_all_tags('span') + head.find_all_tags('div'):
        if node is head or node.has('sd') or node.has('dstep-n'):
            continue
        text = rich(node)
        if not text or text in (title, subtitle):
            continue
        if title and title in text:
            continue
        if text not in out:
            out.append(text)
    return out


def note_block(tone, node, title_from=None):
    heading = title_from if title_from is not None else (node.find_tag('h3') or node.find_tag('h4'))
    title = rich(heading)
    body = rich(node)
    if title and body.startswith(title):
        body = body[len(title):].strip()
    return {'type': 'note', 'tone': tone, 'title': title, 'html': body}


def cta_block(node):
    heading = node.find_tag('h3') or node.find_tag('h2')
    body = rich(node)
    title = rich(heading)
    if title and body.startswith(title):
        body = body[len(title):].strip()
    return {'type': 'cta', 'title': title, 'html': body}


def prose_block(node):
    html = list_html(node) if node.find_all_tags('li') else rich(node)
    return {'type': 'prose', 'html': html} if html else None


def neutral_block(node):
    html = rich(node)
    return {'type': 'neutral', 'html': html} if html else None


def blocks_inside(node):
    """True when a container holds something the mapper can turn into a block."""
    for child in node.elements():
        if child.has('dstep') or child.tag in ('h2', 'h3', 'h4'):
            return True
        if block_of(child) is not None or blocks_inside(child):
            return True
    return False


def block_of(node):
    """Map a non-heading container to a primitive, or ``None`` if unrecognised."""
    if node.tag == 'table':
        return table_block(node)
    for wrap in TABLE_WRAPS:
        if node.has(wrap):
            table = node.find_tag('table')
            if table is not None:
                return table_block(table)
    if node.has('pains'):
        return pain_block(node)
    if node.has('cmp'):
        return compare_block(node)
    if node.has('flow'):
        return steps_block(node)
    if node.has('outs'):
        return outputs_block(node)
    if node.has('tlist'):
        return tasks_block(node)
    if node.has('std-strip'):
        return chips_block(node)
    if node.has('cta'):
        return cta_block(node)
    if node.classes & GRIDS:
        return cards_block(node)
    for name, tone in NOTE_CONTAINERS:
        if node.has(name):
            return note_block(tone, node)
    if node.has('warn'):
        return note_block('warn', node)
    if node.has('verdict'):
        return note_block('verdict', node)
    if node.has('card') or node.has('callout'):
        files = files_block(node)
        if files is not None:
            return files
        if node.has('ev') or node.find('ev') is not None:
            return {'type': 'score', 'html': rich(node)}
        if node.find_tag('h3') is not None or node.find_tag('h4') is not None:
            return note_block('plain', node)
        return prose_block(node)
        if node.tag in PROSE_TAGS:
            return prose_block(node)
        return None


def heading_block(node, subline=''):
    number = node.find('num')
    title = rich(node)
    if number is not None:
        marker = rich(number)
        if marker and title.startswith(marker):
            title = title[len(marker):].strip()
    return {'type': 'heading', 'level': int(node.tag[1]), 'n': rich(number),
            'title': title, 'subline': subline}


def sequence(container, skip=()):
    """Walk children in order, pairing a heading with its following ``subline``.

    Consecutive demo steps collapse into one ``demo_steps`` block. Unknown
    containers are recursed into so their text survives; only a container with
    nothing renderable inside becomes a single ``neutral`` block.
    """
    blocks = []
    children = container.elements()
    index = 0
    while index < len(children):
        node = children[index]
        if node.classes & BASELINE_SKIP or node.classes & set(skip):
            index += 1
            continue
        if node.tag in ('h2', 'h3', 'h4'):
            subline = ''
            if index + 1 < len(children) and children[index + 1].has('subline'):
                subline = rich(children[index + 1])
                index += 1
            blocks.append(heading_block(node, subline))
            index += 1
            continue
        if node.has('dstep'):
            run = []
            while index < len(children) and children[index].has('dstep'):
                run.append(demo_step(children[index]))
                index += 1
            blocks.append({'type': 'demo_steps', 'items': run})
            continue
        block = block_of(node)
        if block is None and node.tag in CONTAINERS and blocks_inside(node):
            nested = sequence(node)
            if nested:
                blocks.extend(nested)
                index += 1
                continue
        if block is None:
            block = prose_block(node)
        if block:
            blocks.append(block)
        index += 1
    return blocks


# ------------------------------------------------------------------------- baseline

def source_text(root):
    """Visible text of the source body, minus locally re-authored chrome."""
    parts = []

    def walk(node, skip):
        for child in node.children:
            if isinstance(child, TextNode):
                if not skip:
                    text = squash(child.data)
                    if len(text) >= 2:
                        parts.append(text)
                continue
            child_skip = skip or bool(child.classes & BASELINE_SKIP) or child.tag == 'button'
            walk(child, child_skip)

    walk(root, False)
    return parts


# ------------------------------------------------------------------------- documents

def document_for(kind, raw):
    root, footer = content_root(raw)
    hero = root.find('page-hero')
    # The hero supplies title/lead, but it can also carry real content (ecn puts the
    # cited standards strip inside it), so everything else in it stays as blocks.
    if hero is not None and hero in root.children:
        keep = [child for child in hero.children
                if not (isinstance(child, Element) and (child.tag == 'h1' or child.has('lead')))]
        root.children[root.children.index(hero):root.children.index(hero) + 1] = keep
    blocks = sequence(root, skip=('subnav',))
    return {
        'kind': kind,
        'title': rich(hero.find_tag('h1')) if hero is not None else '',
        'lead': rich(hero.find('lead')) if hero is not None else '',
        'blocks': blocks,
        'note': rich(footer),
        'text': source_text(root),
    }


# --------------------------------------------------------------------------- capture

def fetch(url, cache=None):
    key = re.sub(r'[^A-Za-z0-9._-]+', '_', url.split('/page/')[-1])[:150]
    cached = cache / (key + '.html') if cache is not None else None
    if cached is not None and cached.is_file() and cached.stat().st_size > 500:
        return cached.read_text(encoding='utf-8', errors='replace')
    request = urllib.request.Request(url, headers={'User-Agent': UA})
    with urllib.request.urlopen(request, timeout=60) as response:
        body = response.read().decode('utf-8', 'replace')
    if cached is not None:
        cache.mkdir(parents=True, exist_ok=True)
        cached.write_text(body, encoding='utf-8')
    return body


def plan(catalog):
    """``(slug, kind, url)`` for every page; the second ``demo`` URL is a fallback."""
    for item in catalog['items']:
        detail = item['detail_url']
        slug = item['slug']
        yield slug, 'intro', detail
        yield slug, 'tasks', urljoin(detail, '../deep-dives/%s-tasks.html' % slug)
        yield slug, 'demo', urljoin(detail, '../live-demos/%s/index.html' % slug)
        yield slug, 'demo', urljoin(detail, '../live-demos/%s.html' % slug)


def capture(root, cache=None, stats=False):
    catalog = json.loads((root / 'webhelp/scenarios.json').read_text(encoding='utf-8'))
    documents, problems = {}, []
    for slug, kind, url in plan(catalog):
        bucket = documents.setdefault(slug, {})
        if kind in bucket:
            continue
        try:
            raw = fetch(url, cache)
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            if kind == 'demo':
                continue
            problems.append('%s %s 抓取失败: %s' % (slug, kind, exc))
            continue
        bucket[kind] = document_for(kind, raw)
        if stats:
            print('  %-32s %-6s %7d 字节' % (slug, kind, len(raw)), file=sys.stderr)
        time.sleep(0.05)
    for item in catalog['items']:
        absent = sorted(set(KINDS) - set(documents.get(item['slug'], {})))
        if absent:
            problems.append('%s 缺少文档: %s' % (item['slug'], absent))
    return catalog, documents, problems


def snapshot(catalog, documents):
    return {
        'source': SOURCE,
        'extracted_at': date.today().isoformat(),
        'extractor': EXTRACTOR,
        'kinds': list(KINDS),
        'primitives': list(PRIMITIVES),
        'catalog': {item['slug']: {'name': item['name'], 'group': item['group']}
                    for item in catalog['items']},
        'items': documents,
    }


def write_snapshot(root, out, catalog, documents):
    target = root / out
    target.write_text(json.dumps(snapshot(catalog, documents), ensure_ascii=False,
                                 separators=(',', ':')) + '\n', encoding='utf-8')
    return target


# -------------------------------------------------------------------------- selftest

def selftest(directory):
    """Map a local corpus (``<slug>.<family>.html``) and report mapping coverage.

    Also runs the text-loss invariant, which is the check that matters: a mapping
    that "looks tidy" but drops a paragraph is a failure, not a style choice.
    """
    from collections import Counter
    counts, neutrals = Counter(), Counter()
    losses = []
    for path in sorted(Path(directory).glob('*.html')):
        if path.stat().st_size < 500:
            continue
        parts = path.name.split('.')
        kind = {'deep': 'intro', 'tasks': 'tasks', 'live': 'demo',
                'live-flat': 'demo'}.get(parts[1] if len(parts) > 1 else '')
        if kind is None:
            continue
        document = document_for(kind, path.read_text(encoding='utf-8', errors='replace'))
        counts[kind] += 1
        for block in walk_blocks(document['blocks']):
            if block['type'] == 'neutral':
                neutrals[kind] += 1
        if not document['title']:
            print('  缺少标题: %s' % path.name)
        missing = lost_text(document)
        if missing:
            losses.append((path.name, missing))
    print('自测：%s' % dict(counts))
    print('  中性块（未映射结构）：%s' % dict(neutrals))
    print('  丢文本的页面：%d' % len(losses))
    for name, missing in losses[:12]:
        print('    %s → 缺 %d 段，例: %s' % (name, len(missing), missing[0][:70]))
    return losses


def walk_blocks(blocks):
    for block in blocks:
        yield block
        for step in block.get('items', []) if block.get('type') == 'demo_steps' else []:
            yield from walk_blocks(step.get('blocks', []))


def flattened_text(document):
    """All text a rendered document would show, as one squashed string.

    Tags are removed without inserting a separator so that a source text node
    split across inline emphasis (``<b>``) still matches contiguously.
    """
    parts = []

    def walk(value):
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, list):
            for entry in value:
                walk(entry)
        elif isinstance(value, dict):
            for key, entry in value.items():
                if key != 'text':
                    walk(entry)

    walk({key: value for key, value in document.items() if key != 'text'})
    joined = unescape(re.sub(r'<[^>]*>', '', ''.join(parts)))
    return squash(joined)


def lost_text(document):
    """Source text segments that the mapped document would no longer show."""
    flat = flattened_text(document)
    return [segment for segment in document.get('text', []) if segment not in flat]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', default=str(Path(__file__).resolve().parents[4]))
    parser.add_argument('--out', default='webhelp/scenario_docs.json')
    parser.add_argument('--cache', default='')
    parser.add_argument('--stats', action='store_true')
    parser.add_argument('--selftest', default='')
    args = parser.parse_args()

    if args.selftest:
        selftest(args.selftest)
        return 0

    root = Path(args.root).resolve()
    cache = Path(args.cache).resolve() if args.cache else None
    catalog, documents, problems = capture(root, cache, args.stats)
    if problems:
        print('\n'.join('FAIL ' + problem for problem in problems), file=sys.stderr)
        return 1
    target = write_snapshot(root, args.out, catalog, documents)
    counts = {kind: sum(1 for docs in documents.values() if kind in docs) for kind in KINDS}
    print('写入 %s：%d 个场景，%.2f MB' % (target, len(documents), target.stat().st_size / 1048576))
    for kind in KINDS:
        print('  %-6s %d 份' % (kind, counts[kind]))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
