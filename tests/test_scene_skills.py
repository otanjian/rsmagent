"""Scene bundles remain discoverable without changing custom skill precedence."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from Scene.catalog import ROOT
from scenes import config as scenes_config
from scenes import service as scenes_service


class SceneMigrationTests(unittest.TestCase):
    def test_catalog_uses_per_scene_definitions(self):
        catalog = scenes_config.load_config()
        self.assertEqual(len(catalog['categories']), 10)
        self.assertEqual(len(catalog['scenes']), 27)
        self.assertEqual(sum(len(s.get('sub_scenes', [])) for s in catalog['scenes']), 81)
        for scene in catalog['scenes']:
            self.assertEqual(scene, json.loads((ROOT / scene['id'] / 'scene.json').read_text()))

    def test_original_files_match_recorded_source_hashes(self):
        manifest = json.loads((ROOT / 'source-manifest.json').read_text())
        for entry in manifest['files']:
            with self.subTest(file=entry['path']):
                self.assertEqual(hashlib.sha256((ROOT / entry['path']).read_bytes()).hexdigest(), entry['sha256'])

    def test_scene_can_activate_and_resolve_subscene_skill(self):
        with patch('bridge.bridge.Bridge'):
            scene, error = scenes_service.activate('supplier_risk', 'scene-migration-test')
        try:
            self.assertIsNone(error)
            self.assertEqual(scene['parent_id'], 'procurement_supplier')
            self.assertEqual(scenes_service.resolve_skill_names(scene), ['procurement-supplier-risk'])
        finally:
            scenes_service.clear_scene_context('scene-migration-test')

    def test_scene_bundles_and_custom_override(self):
        from agent.skills.manager import SkillManager
        with tempfile.TemporaryDirectory() as root:
            custom_dir = Path(root) / 'skills'
            skill_dir = custom_dir / 'quality-trace'
            skill_dir.mkdir(parents=True)
            (skill_dir / 'SKILL.md').write_text('---\nname: quality-trace\ndescription: Custom trace\n---\nCustom.\n')
            manager = SkillManager(custom_dir=str(custom_dir))
            self.assertIn('skill-creator', manager.skills)
            for name in ('quality-trace', 'sap-integration', 'bid-analysis',
                         'procurement-supplier-risk', 'production-scheduling',
                         'financial-report-analysis', 'finance-ledger-generator'):
                self.assertIn(name, manager.skills.keys())
            trace = manager.skills['quality-trace']
            self.assertEqual(trace.skill.source, 'custom')
            self.assertEqual(trace.shadowed.skill.source, 'builtin')
            self.assertIn('/Scene/quality_traceability/', trace.shadowed.skill.file_path)


if __name__ == '__main__':
    unittest.main()
