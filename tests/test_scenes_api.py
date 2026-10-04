# encoding:utf-8
"""场景数据接口测试（阶段 0）。

覆盖 ``GET /api/scenes`` 与 ``POST /api/scenes/activate`` 的处理器接线与
``scenes.service`` 的目录/激活逻辑：正常返回、配置缺失/解析失败、缺参、
场景不存在、子场景激活（合并父元数据）与会话上下文写入。

鉴权与租户门禁分别由 ``tests/test_http_gate.py`` /
``tests/test_scenes_tenant_scope.py`` 覆盖；本文件把 ``_db_scope`` 打桩为带
``chat.use`` 的 ``RequestContext``，专注验证场景接口本身。
"""
import contextlib
import json
import unittest
from unittest.mock import patch

import web

from auth.runtime import RequestContext
from channel.web import web_channel
from common.runtime_identity import RuntimeIdentity, use_identity
from scenes import service as scenes_service
from scenes import config as scenes_config


_TENANT = "tnt_test"

_CATALOG = {
    "categories": [{"id": "test", "name": "测试"}],
    "scenes": [{
        "id": "test_scene", "name": "测试场景", "category": "test",
        "skill_name": "test-skill", "required_permission": "chat.use",
        "sub_scenes": [{"id": "test_sub_scene", "name": "测试子场景"}],
    }],
}


def _ctx():
    return RequestContext(
        user_id="u_test", username="u_test", display_name="u_test",
        is_platform_admin=False, must_change_password=False,
        tenant_id=_TENANT, membership={"id": "m1"},
        permissions={"chat.use"}, is_tenant_admin=False,
    )


@contextlib.contextmanager
def _fake_db_scope():
    with use_identity(RuntimeIdentity(user_id="u_test", tenant_id=_TENANT)):
        yield _ctx()


class ScenesApiTests(unittest.TestCase):
    def setUp(self):
        scenes_service.clear_all_scene_context()

    def tearDown(self):
        scenes_service.clear_all_scene_context()

    def _app(self):
        # Minimal routes only: HTTP policy is covered elsewhere. These tests
        # exercise the handler body under a faked request scope.
        return web.application(
            (
                "/api/scenes", "ScenesHandler",
                "/api/scenes/activate", "SceneActivateHandler",
            ),
            vars(web_channel),
            autoreload=False,
        )

    def _request(self, path, method="GET", data=None):
        app = self._app()
        kwargs = {"method": method, "headers": {"Host": "test"}}
        if data is not None:
            kwargs["data"] = json.dumps(data)
            kwargs["headers"]["Content-Type"] = "application/json"
        with patch.object(web_channel, "_db_scope", _fake_db_scope):
            return app.request(path, **kwargs)

    @staticmethod
    def _json(resp):
        return json.loads(resp.data.decode("utf-8"))

    # ------------------------------------------------------------------
    # GET /api/scenes
    # ------------------------------------------------------------------
    def test_scenes_returns_catalog(self):
        resp = self._request("/api/scenes", method="GET")
        data = self._json(resp)
        self.assertEqual(data["status"], "success")
        self.assertEqual(len(data["categories"]), 10)
        self.assertEqual(len(data["scenes"]), 27)

    def test_scenes_returns_configured_catalog(self):
        with patch.object(scenes_config, "load_config", return_value=_CATALOG):
            data = self._json(self._request("/api/scenes"))
        self.assertEqual(data["categories"], _CATALOG["categories"])
        self.assertEqual(data["scenes"], _CATALOG["scenes"])

    def test_scenes_config_missing_returns_empty(self):
        with patch.object(scenes_config, "load_config", return_value=None):
            resp = self._request("/api/scenes", method="GET")
        data = self._json(resp)
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["categories"], [])
        self.assertEqual(data["scenes"], [])

    def test_scenes_config_parse_failure_returns_empty(self):
        # load_config 内部解析失败时降级返回 None，service 再降级为空结构。
        import tempfile, os
        bad = os.path.join(tempfile.mkdtemp(), "scenes_config.json")
        with open(bad, "w", encoding="utf-8") as f:
            f.write("{ not valid json")
        with patch.object(scenes_config, "config_path", return_value=bad):
            self.assertIsNone(scenes_config.load_config())
            resp = self._request("/api/scenes", method="GET")
        data = self._json(resp)
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["categories"], [])
        self.assertEqual(data["scenes"], [])

    # ------------------------------------------------------------------
    # POST /api/scenes/activate
    # ------------------------------------------------------------------
    def test_activate_missing_params(self):
        resp = self._request("/api/scenes/activate", method="POST", data={})
        data = self._json(resp)
        self.assertEqual(data["status"], "error")
        self.assertIn("scene_id", data["message"])

    def test_activate_scene_not_found(self):
        resp = self._request(
            "/api/scenes/activate",
            method="POST",
            data={"scene_id": "does_not_exist", "session_id": "s1"},
        )
        data = self._json(resp)
        self.assertEqual(data["status"], "error")
        self.assertIn("not found", data["message"])

    @patch.object(scenes_config, "load_config", return_value=_CATALOG)
    def test_activate_top_scene_writes_context(self, _load):
        resp = self._request(
            "/api/scenes/activate",
            method="POST",
            data={"scene_id": "test_scene", "session_id": "s1"},
        )
        data = self._json(resp)
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["scene"]["id"], "test_scene")
        # 会话上下文已写入（按请求租户命名空间）
        ctx = scenes_service.get_scene_context("s1", tenant_id=_TENANT)
        self.assertIsNotNone(ctx)
        self.assertEqual(ctx["id"], "test_scene")

    @patch.object(scenes_config, "load_config", return_value=_CATALOG)
    def test_activate_sub_scene_merges_parent(self, _load):
        resp = self._request(
            "/api/scenes/activate",
            method="POST",
            data={"scene_id": "test_sub_scene", "session_id": "s2"},
        )
        data = self._json(resp)
        self.assertEqual(data["status"], "success")
        scene = data["scene"]
        # 子场景激活：合并父场景元数据
        self.assertEqual(scene["id"], "test_sub_scene")
        self.assertEqual(scene["parent_id"], "test_scene")
        self.assertEqual(scene["parent_name"], "测试场景")
        self.assertEqual(scene["skill_name"], "test-skill")

    # ------------------------------------------------------------------
    # service 层直接验证
    # ------------------------------------------------------------------
    @patch.object(scenes_config, "load_config", return_value=_CATALOG)
    def test_find_scene_top_and_sub(self, _load):
        scene, is_sub = scenes_service.find_scene("test_scene")
        self.assertEqual(scene["id"], "test_scene")
        self.assertFalse(is_sub)

        scene, is_sub = scenes_service.find_scene("test_sub_scene")
        self.assertTrue(is_sub)
        self.assertEqual(scene["parent_id"], "test_scene")

        scene, is_sub = scenes_service.find_scene("nope")
        self.assertIsNone(scene)
        self.assertFalse(is_sub)


if __name__ == "__main__":
    unittest.main()
