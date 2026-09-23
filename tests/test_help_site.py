"""Exercise the integrated route, rendered navigation and public asset boundary."""
import json
import re
import shutil
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from channel.web.web_channel import build_web_app
from channel.web.route_registry import check_route_coverage
from webhelp.site import (HelpView, PAGES, link_allowed, resource_root, scenario_document,
                          scenario_document_pages)
from webhelp.tools.check_manual import validate
from webhelp.tools.build_docs import Document, clean
from webhelp.tools import check_scenario_docs, check_scenarios


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []
        self.ids = set()
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if 'id' in attrs: self.ids.add(attrs['id'])
        for name in ['src', 'href']:
            if name in attrs: self.urls.append(attrs[name])


class HelpSiteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = build_web_app()

    def test_all_pages_and_documents_in_both_languages(self):
        docs = HelpView().docs
        scenarios = scenario_document_pages(str(resource_root()))['items']
        checked = set()
        for lang in ['zh', 'en']:
            pages = [page for page in PAGES if page not in ('doc', 'scenario_doc')]
            paths = ['/help/' + (page if page != 'index' else '') + '?lang=' + lang for page in pages]
            paths += ['/help/doc?p=' + slug + '&lang=' + lang for slug in docs]
            slugs = sorted(scenarios) if lang == 'zh' else sorted(scenarios)[:3]
            kinds = [''] if lang != 'zh' else ['', '/tasks', '/demo']
            for slug in slugs:
                paths += ['/help/scenario/' + slug + kind + '?lang=' + lang for kind in kinds]
            for path in paths:
                with self.subTest(path=path):
                    response = self.app.request(path)
                    self.assertEqual(response.status, '200 OK')
                    html = response.data.decode()
                    self.assertIn('<html lang="' + ('zh-CN' if lang == 'zh' else 'en') + '">', html)
                    self.assertNotIn('<?php', html)
                    self.assertNotIn('YOUR-SITE-DOMAIN', html)
                    self.assertNotRegex(html, r'(?:href|src)="[^\"]*\.php')
                    links = Links(); links.feed(html)
                    for target in links.urls:
                        if target.startswith('#'):
                            self.assertIn(target[1:], links.ids)
                            continue
                        # 站点默认自包含；站外链接只允许 config.json 登记的协议与主机。
                        self.assertTrue(link_allowed(target), target)
                        if not target.startswith('/help/'):
                            continue
                        if target in checked: continue
                        checked.add(target)
                        linked = self.app.request(target)
                        self.assertEqual(linked.status, '200 OK', target)
        self.assertGreater(len(docs), 20)

    def test_language_cookie_and_switch_keep_document_slug(self):
        response = self.app.request('/help/doc?p=memory&lang=en')
        self.assertIn('webhelp_lang=en', response.headers['Set-Cookie'])
        self.assertIn('Path=/help', response.headers['Set-Cookie'])
        self.assertIn('HttpOnly', response.headers['Set-Cookie'])
        self.assertIn('/help/doc?p=memory&amp;lang=zh', response.data.decode())
        response = self.app.request('/help/manual', headers={'Cookie': 'webhelp_lang=en'})
        self.assertIn('<html lang="en">', response.data.decode())
        response = self.app.request('/help/?lang=INVALID', headers={'Cookie': 'webhelp_lang=INVALID'})
        self.assertIn('<html lang="zh-CN">', response.data.decode())

    def test_redirects_preserve_query(self):
        for source, target in [('/help?lang=en', '/help/?lang=en'),
                               ('/help/index.php?lang=en', '/help/?lang=en'),
                               ('/help/doc.php?p=memory&lang=en', '/help/doc?p=memory&lang=en')]:
            response = self.app.request(source)
            self.assertEqual(response.status, '301 Moved Permanently')
            self.assertEqual(response.headers['Location'], target)

    def test_unknown_pages_and_private_files_are_not_served(self):
        for path in ['config.json', 'content.json', 'templates/index.html', 'site.py', 'docs/manifest.json',
                     'tools/build_docs.py', 'missing', 'doc?p=../config', 'doc?p=missing',
                     'assets/../config.json', 'assets/%2e%2e/config.json', 'assets/img/../../site.py',
                     'assets/.secret', 'assets/no-file.js']:
            self.assertEqual(self.app.request('/help/' + path).status, '404 Not Found', path)
        self.assertEqual(self.app.request('/help/', method='POST').status, '405 Method Not Allowed')

    def test_symlink_escape_is_denied(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'site'; (root / 'assets').mkdir(parents=True)
            outside = Path(folder) / 'secret.js'; outside.write_text('secret')
            (root / 'assets/leak.js').symlink_to(outside)
            with patch('channel.web.help_site.resource_root', return_value=root):
                response = self.app.request('/help/assets/leak.js')
            self.assertEqual(response.status, '404 Not Found')

    def test_asset_mime_cache_and_ranges(self):
        path = '/help/assets/css/style.css'
        response = self.app.request(path)
        self.assertIn('text/css', response.headers['Content-Type'])
        self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')
        cached = self.app.request(path, headers={'If-None-Match': response.headers['ETag']})
        self.assertEqual(cached.status, '304 Not Modified')
        partial = self.app.request(path, headers={'Range': 'bytes=2-10'})
        self.assertEqual(partial.status, '206 Partial Content')
        self.assertEqual(partial.data, response.data[2:11])
        suffix = self.app.request(path, headers={'Range': 'bytes=-7'})
        self.assertEqual(suffix.data, response.data[-7:])
        for value in ['bytes=-0', 'bytes=9999999-', 'bytes=9-2', 'bytes=abc', 'bytes=0-1,4-5']:
            self.assertEqual(self.app.request(path, headers={'Range': value}).status, '416 Range Not Satisfiable')

    def test_command_urls_use_current_backend(self):
        response = self.app.request('/help/quickstart', host='help.example.test:9899')
        self.assertIn('http://help.example.test:9899/help/assets/deploy/run.sh', response.data.decode())

    def test_escaping_and_route_coverage(self):
        view = HelpView('manual')
        view.translations['manual']['title'] = '<script>alert(1)</script>'
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;', view.render())
        self.assertNotIn('<script>alert(1)</script>', view.render())
        import channel.web.web_channel as module
        self.assertEqual(check_route_coverage(vars(module)), [])

    def test_manual_assets_and_translations(self):
        self.assertEqual(validate(), [])

    # ===== 应用场景（/help/scenarios） =====

    def snapshot_root(self, folder):
        """Minimal webhelp root: only the files the snapshot validators read."""
        root = Path(folder) / 'webhelp'
        (root / 'lang').mkdir(parents=True)
        for name in ('config.json', 'scenarios.json', 'scenario_docs.json'):
            shutil.copy(resource_root() / name, root / name)
        for lang in ('zh', 'en'):
            shutil.copy(resource_root() / 'lang' / (lang + '.json'), root / 'lang' / (lang + '.json'))
        return root

    def rewrite_snapshot(self, root, mutate, name='scenarios.json'):
        document = json.loads((root / name).read_text(encoding='utf-8'))
        mutate(document)
        (root / name).write_text(json.dumps(document, ensure_ascii=False), encoding='utf-8')

    def test_scenario_page_carries_the_published_snapshot(self):
        document = scenario_document(str(resource_root()))
        items = document['items']
        self.assertEqual(len(items), 63)
        html = self.app.request('/help/scenarios').data.decode()
        self.assertEqual(len(re.findall(r'feature-card[^"]*scenario-card', html)), len(items))
        self.assertEqual(html.count('data-scenario-try="'), len(items))
        self.assertEqual(html.count('href="/help/scenario/'), len(items))
        self.assertNotIn('scenario-group-head', html)
        self.assertEqual(re.findall(r'href="(/help/[^"]*)" class="nav-link[^"]*"', html),
                         ['/help/', '/help/scenarios', '/help/about'])
        self.assertIn('id="scenarioRoleFilters"', html)
        self.assertIn('id="scenarioSearch"', html)
        self.assertIn('搜索方案 / 痛点关键词', html)
        self.assertRegex(html, r'data-role=""[^>]*>全部 <span class="scenario-chip-count">63</span>')
        self.assertNotIn('scenario-intro', html)
        self.assertRegex(html, r'data-role="财务"[^>]*>财务 <span class="scenario-chip-count">\d+</span>')
        self.assertRegex(html, r'data-scenario-search="[^"]+')
        self.assertRegex(html, r'data-scenario-roles="[^"]*\b财务\b')
        visible = html.split('data-scenario-data>')[0]
        for item in items:
            self.assertIn(item['name'], html)
            self.assertNotIn(item['prompt'], visible)
        for target in re.findall(r'(?:src|<link[^>]*href)="([^"]+)"', html):
            self.assertTrue(target.startswith('/help/'), target)
        payload = json.loads(re.search(r'data-scenario-data>(.*?)</script>', html, re.S).group(1))
        self.assertEqual(payload.get('open_path'), '/')
        self.assertEqual(payload.get('message'), '你能做什么？')
        self.assertEqual(sorted(payload.get('agents', {})), sorted(item['slug'] for item in items))
        self.assertEqual(payload['agents']['biz-review'], 'wb-biz-review')
        self.assertEqual(payload['agents']['rfq-quote'], 'rfq-quote')

    def test_scenario_page_keeps_scenario_copy_and_switches_language(self):
        html = self.app.request('/help/scenarios?lang=en').data.decode()
        self.assertIn('Use Cases', html)
        self.assertIn('Run it now', html)
        self.assertIn('/help/scenarios?lang=zh', html)
        self.assertNotIn('scenarios.groups', html)
        self.assertIn('经营复盘', html)

    def test_scenario_catalog_validation_accepts_the_shipped_snapshot(self):
        self.assertEqual(check_scenarios.validate(), [])

    def test_scenario_catalog_validation_rejects_missing_field(self):
        with tempfile.TemporaryDirectory() as folder:
            root = self.snapshot_root(folder)
            self.rewrite_snapshot(root, lambda doc: doc['items'][0].update(pain=''))
            errors = check_scenarios.validate(root, page='')
        self.assertTrue(any('缺少字段 pain' in message for message in errors), errors)

    def test_scenario_catalog_validation_rejects_unregistered_external_link(self):
        with tempfile.TemporaryDirectory() as folder:
            root = self.snapshot_root(folder)
            self.rewrite_snapshot(root, lambda doc: doc['items'][0].update(detail_url='https://evil.example/x.html'))
            errors = check_scenarios.validate(root, page='')
        self.assertTrue(any('不在白名单' in message for message in errors), errors)

    def test_scenario_catalog_validation_rejects_render_drift(self):
        errors = check_scenarios.validate(page='<article class="feature-card scenario-card"></article>')
        self.assertTrue(any('页面渲染条目数' in message for message in errors), errors)

    def test_scenario_catalog_validation_rejects_missing_translation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = self.snapshot_root(folder)
            pack = json.loads((root / 'lang' / 'en.json').read_text(encoding='utf-8'))
            del pack['scenarios']['groups']['hr']
            (root / 'lang' / 'en.json').write_text(json.dumps(pack, ensure_ascii=False), encoding='utf-8')
            errors = check_scenarios.validate(root, page='')
        self.assertTrue(any('缺少文案键 scenarios.groups.hr' in message for message in errors), errors)
        self.assertTrue(any('en 缺少分组文案 scenarios.groups.hr' in message for message in errors), errors)

    # ===== 场景详情（/help/scenario/<slug>[/tasks|/demo]） =====

    def test_scenario_doc_routes_render_the_three_pages(self):
        for slug in ('ecn', 'biz-review'):
            for suffix in ('', '/tasks', '/demo'):
                with self.subTest(path=slug + suffix):
                    response = self.app.request('/help/scenario/' + slug + suffix)
                    self.assertEqual(response.status, '200 OK')
                    html = response.data.decode()
                    self.assertIn('doc-body sdoc-body', html)
                    self.assertIn('<nav class="sdoc-tabs"', html)
                    self.assertNotIn('scenario_docs.json', html)
        detail = scenario_document_pages(str(resource_root()))['items']['ecn']
        for kind, suffix in (('intro', ''), ('tasks', '/tasks'), ('demo', '/demo')):
            html = self.app.request('/help/scenario/ecn' + suffix).data.decode()
            self.assertIn(detail[kind]['title'], html)
            self.assertIn(detail[kind]['lead'], html)

    def test_scenario_doc_unknown_slugs_and_bare_path_are_not_served(self):
        for path in ['/help/scenario_doc', '/help/scenario/', '/help/scenario/missing',
                     '/help/scenario/ecn/nope', '/help/scenario/../config', '/help/scenario/ECN']:
            self.assertEqual(self.app.request(path).status, '404 Not Found', path)

    def test_scenario_doc_pages_are_self_contained_and_offline(self):
        detail = scenario_document_pages(str(resource_root()))['items']['ecn']
        html = self.app.request('/help/scenario/ecn/demo').data.decode()
        links = Links(); links.feed(html)
        for target in links.urls:
            self.assertTrue(link_allowed(target), target)
            if not target.startswith('/help/') or target.startswith('/help/scenario/ecn'):
                continue
            self.assertEqual(self.app.request(target).status, '200 OK', target)
        visible = check_scenario_docs.visible_text(html)
        for segment in detail['demo']['text']:
            self.assertIn(segment, visible)

    def test_scenario_doc_switches_shell_language_and_keeps_body_copy(self):
        html = self.app.request('/help/scenario/ecn/tasks?lang=en').data.decode()
        self.assertIn('<html lang="en">', html)
        self.assertIn('Suggested tasks', html)
        # 正文保持来源页原语言，切换语言不改写内容。
        self.assertIn('工程变更管理ECN', html)
        self.assertIn('/help/scenario/ecn/tasks?lang=zh', html)
        # 切语言不改条目数、也不改当前文档类型。
        zh = self.app.request('/help/scenario/ecn/tasks?lang=zh').data.decode()
        self.assertEqual(html.count('class="sdoc-task"'), zh.count('class="sdoc-task"'))
        self.assertNotEqual(html.count('class="sdoc-task"'), 0)
        self.assertIn('/help/scenario/ecn/tasks?lang=en', zh)

    def test_scenario_doc_pages_are_indexed_and_navigable(self):
        html = self.app.request('/help/scenario/ecn').data.decode()
        # 场景详情不再展示右侧「全部场景」侧栏；场景间跳转走列表页与页脚上下篇。
        self.assertNotIn('doc-aside', html)
        self.assertNotIn('全部场景', html)
        listed = self.app.request('/help/scenarios').data.decode()
        self.assertEqual(listed.count('href="/help/scenario/'), 63)
        # 做法页已本地化：列表页可见内容里不再出现来源站主机（提示词里的安装地址仍随深链下发）。
        self.assertNotIn('codebuddy.work', listed.split('data-scenario-data>')[0])

    def test_scenario_doc_snapshot_validation_accepts_the_shipped_snapshot(self):
        self.assertEqual(check_scenario_docs.validate(render=False), [])
        for slug in ('ecn', 'biz-review'):
            self.assertEqual(check_scenario_docs.validate_page(slug, 'demo', 'zh'), [])

    def test_scenario_doc_snapshot_validation_rejects_incomplete_pages(self):
        with tempfile.TemporaryDirectory() as folder:
            root = self.snapshot_root(folder)
            self.rewrite_snapshot(root, lambda doc: doc['items']['ecn'].__setitem__(
                'demo', {**doc['items']['ecn']['demo'], 'blocks': []}), 'scenario_docs.json')
            errors = check_scenario_docs.validate_document(root)
        self.assertTrue(any('ecn/demo 没有正文' in message for message in errors), errors)

    def test_scenario_doc_snapshot_validation_rejects_active_content(self):
        with tempfile.TemporaryDirectory() as folder:
            root = self.snapshot_root(folder)

            def inject(doc):
                page = doc['items']['ecn']['intro']
                page['blocks'][0] = {'type': 'prose',
                                     'html': '<img src=x onerror=alert(1)><script>alert(1)</script>'}
            self.rewrite_snapshot(root, inject, 'scenario_docs.json')
            errors = check_scenario_docs.validate_document(root)
        self.assertTrue(any('含未登记的标签' in message for message in errors), errors)

    def test_scenario_doc_snapshot_validation_rejects_unknown_primitive_and_tone(self):
        with tempfile.TemporaryDirectory() as folder:
            root = self.snapshot_root(folder)

            def mutate(doc):
                page = doc['items']['ecn']['intro']
                page['blocks'][0] = {'type': 'carousel', 'html': 'x'}
                note = next(block for block in doc['items']['ecn']['demo']['blocks'] if block['type'] == 'note')
                note['tone'] = 'shouting'
            self.rewrite_snapshot(root, mutate, 'scenario_docs.json')
            errors = check_scenario_docs.validate_document(root)
        self.assertTrue(any('原语未登记: carousel' in message for message in errors), errors)
        self.assertTrue(any('提示语气未登记: shouting' in message for message in errors), errors)

    def test_scenario_doc_snapshot_validation_rejects_dropped_text(self):
        with tempfile.TemporaryDirectory() as folder:
            root = self.snapshot_root(folder)

            def mutate(doc):
                page = doc['items']['ecn']['intro']
                page['blocks'] = page['blocks'][1:]
            self.rewrite_snapshot(root, mutate, 'scenario_docs.json')
            errors = check_scenario_docs.validate_document(root)
        self.assertTrue(any('ecn/intro 丢文本' in message for message in errors), errors)

    def test_scenario_doc_snapshot_validation_rejects_missing_shell_copy(self):
        with tempfile.TemporaryDirectory() as folder:
            root = self.snapshot_root(folder)
            for lang in ('zh', 'en'):
                pack = json.loads((root / 'lang' / (lang + '.json')).read_text(encoding='utf-8'))
                del pack['scenario_doc']['kinds']['demo']
                (root / 'lang' / (lang + '.json')).write_text(json.dumps(pack, ensure_ascii=False), encoding='utf-8')
            errors = check_scenario_docs.validate_document(root)
        self.assertTrue(any('缺少页面文案 scenario_doc.kinds.demo' in message for message in errors), errors)

    def test_scenario_doc_html_sanitizer_keeps_only_whitelisted_tags(self):
        from webhelp.site import scenario_html
        value = '<b>粗</b><br><ul><li>项</li></ul><script>alert(1)</script><img src=x onerror=1>'
        cleaned = scenario_html(value)
        self.assertIn('<b>粗</b>', cleaned)
        self.assertIn('<br>', cleaned)
        self.assertNotIn('script', cleaned)
        self.assertNotIn('img', cleaned)
        self.assertNotIn('onerror', cleaned)

    def test_desktop_bundle_ships_every_webhelp_resource(self):
        """PyInstaller ships only what the spec lists; a missing JSON is a 500 in the shipped app."""
        spec = (resource_root().parent / 'desktop' / 'build' / 'cowagent-backend.spec').read_text(encoding='utf-8')
        bundled = set(re.findall(r"rp\('webhelp', '([^']+)'\)", spec))
        for name in ['templates', 'assets', 'docs', 'lang', 'config.json', 'content.json', 'scenarios.json',
                     'scenario_docs.json', 'icons.json']:
            self.assertIn(name, bundled)

    def test_document_importer_strips_active_and_external_content(self):
        doc = Document('<div id="content"><h2 id="x">Title</h2><script>alert(1)</script>'
                       '<p><a href="https://other.test">External</a> <a href="/zh/memory">Memory</a>'
                       '<a href="#x">Jump</a><img src="https://images.test/a.png" onerror="alert(1)"></p></div>')
        result = clean(doc.content(), {'/zh/memory': 'memory'}, lambda source: 'assets/img/docs/a.png')
        self.assertNotIn('script', result)
        self.assertNotIn('onerror', result)
        self.assertNotIn('https://', result)
        self.assertIn('href="doc?p=memory"', result)
        self.assertIn('href="#x"', result)
