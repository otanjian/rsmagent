# encoding:utf-8
"""§6.4 database 能力逐项验收 —— `adopt-upstream-web-split` 合并候选。

规范依据：`doc/master合并到rdai分支规范.md` §6.4。能力只有同时取得

1. **实际可用**：合法用户登录、选择上下文后，从真实入口完成业务动作并核对结果；
2. **权限仍成立**：匿名 / 无权 / 跨租户 / 平台平面被拒，且拒绝后没有副作用；
3. **链路完整**：请求经真实 `build_web_app()` 路由到达业务 handler，不是 503 关闭；

三类证据后才能标为通过。本文件在 **两个租户、多个用户、一个真实 WSGI 应用**上执行，
与 `scripts/conflict-baseline.txt` 的 `keep-fork` 前端取舍无关：本轮合并的核心风险是
上游拆分的 web 层被重新落到 database 身份上，因此这里断言的是**入口与授权**，不是页面。

矩阵（能力 → 入口 → 证据）：

| 能力 | 真实入口 | 正向 | 隔离 |
| --- | --- | --- | --- |
| 平台平面：模型 / 配置 / 渠道 / 日志 | `/api/models`、`/config`、`/api/channels`、`/api/logs` | 平台管理员 200 | 租户管理员、普通成员 403；匿名 401 |
| 搜索提供方（合并 Port 1） | `POST /api/models action=set_search_credential` | searxng URL、keenable 匿名层可保存并回读 | 非平台管理员 403 |
| 备用模型链（合并 Port 2） | `POST /api/models action=set_capability chat_fallback` | 两环链回读一致并落 `config.json` | 空链启用被拒；非平台管理员 403 |
| 模型目录覆盖（合并移植） | `POST /api/models action=save_catalog` | 覆盖/隐藏回读一致 | 删除自定义提供方后目录一并清除 |
| 版本（合并移植） | `GET /api/version` | 公开可达且含 `version`/`install_kind` | 无 |
| 租户平面：记忆 / 调度 / 历史 / 技能 / 知识 / 智能体 / 会话 / 工作区 / 项目 / 渠道 | 见 `TENANT_PLANE` | 成员到达 handler，非 503 | 匿名 401；跨租户 403 |
| 已知缺口（不在本 change 收口） | 3 条 `/api/update/*` | — | 未注册 → 404（不是静默放开） |
| 本 change 已注册未开放（8 条动作） | `DECLARED_WHILE_CLOSED` | 开放后由各自验收用例正向覆盖 | 关闭 → 503 网关拒绝，与 `/auth/context.feature_actions` 同一份声明 |

后端增量本身的逐条裁定见 `evidence/21-increment-adjudication.md`；本文件只回答
"合并后的候选在 database 模式下是否仍然可用、仍然收权、入口仍然接通"。
"""

import json
import os
import tempfile
import unittest
from unittest.mock import patch

from tests._helpers import IdentityStack, WebAppHarness

ACME_AGENT = "acme-agent"
GLOBEX_AGENT = "globex-agent"

GLOBEX_ADMIN_TEMP = "GlobexTempPass1"
GLOBEX_ADMIN_FINAL = "Str0ngGlobexFinal1"
MEMBER = IdentityStack.MEMBER_PASSWORD

#: A gate refusal means the route is closed or unauthorized *before* the
#: handler; a permitted caller must never see one.
GATE_REFUSALS = (401, 403, 503)

#: Platform-plane surface (route policy ``platform``).
PLATFORM_PLANE = ("/api/models", "/config", "/api/channels", "/api/logs")

#: Tenant-plane surface: one representative real route per capability in the
#: §6 comparison list. Bodies are deliberately minimal -- the point is where the
#: request stops for each actor class, not what the handler does with it.
TENANT_PLANE = (
    ("/api/memory", None),
    ("/api/scheduler", None),
    ("/api/history", None),
    ("/api/skills", None),
    ("/api/knowledge/list", None),
    ("/api/agents", None),
    ("/api/sessions", None),
    ("/api/projects/browse", None),
    ("/api/workspace/tree", None),
    ("/api/tenant/channels", None),
)

#: Routes upstream added that this console keeps unrouted (evidence/21 §E, and
#: the desktop renderer's live consumers, evidence/deferred-upstream-frontend
#: §D1). They must answer 404 -- an accidental 200 would mean a new entry went
#: live without an authorization decision.
#:
#: The eight desktop interfaces this file used to list here are gone from this
#: tuple: change `integrate-upstream-core-capabilities` registers them on
#: purpose, so they are no longer "unrouted". Their new shape is asserted by
#: `DECLARED_WHILE_CLOSED` below, which is a stronger check than the 404 they
#: used to produce -- a 404 cannot tell "no such feature" from "not opened
#: here", and only the registered form can carry an authorization decision.
UNROUTED = (
    ("GET", "/api/update/check"),
    ("POST", "/api/update/start"),
    ("GET", "/api/update/status"),
)

#: The eight actions of `integrate-upstream-core-capabilities` that are
#: registered before they are accepted: the route exists, the gate refuses it
#: with 503 before any handler runs, and `/auth/context.feature_actions` reports
#: the same answer from the same declaration (`auth/capability_matrix.py`).
#: Pattern, verb and the capability action each one projects.
DECLARED_WHILE_CLOSED = (
    ("/api/scheduler/instances", "GET", "scheduler.instances"),
    ("/api/scheduler/recipients", "GET", "scheduler.recipients"),
    ("/api/scheduler/create", "POST", "scheduler.create"),
    ("/api/scheduler/runs", "GET", "scheduler.runs.list"),
    ("/api/scheduler/runs/detail", "GET", "scheduler.runs.detail"),
    ("/api/scheduler/runs/delete", "POST", "scheduler.runs.delete"),
    ("/api/sessions/(.*)/context_usage", "GET", "session_context.usage"),
    ("/api/sessions/(.*)/compact_context", "POST", "session_context.compact"),
)


def _status(response) -> int:
    """HTTP status as an int.

    ``WebAppHarness`` returns web.py's response: the policy processor produces a
    reason phrase ("403 Forbidden") while a handler raising ``web.HTTPError``
    produces a bare number ("403"), so both shapes are read.
    """
    raw = str(getattr(response, "status", "") or "")
    return int(raw.split(" ", 1)[0]) if raw[:3].isdigit() else 0


def _body(response) -> dict:
    try:
        return json.loads(response.data.decode("utf-8"))
    except Exception:  # noqa: BLE001 - non-JSON bodies are simply "not a payload"
        return {}


#: One built app for the whole module: building ``build_web_app()`` and its
#: identity database is the expensive half, and every class below only adds
#: assertions on the same two tenants.
_STATE = {}


def _ensure_state():
    if _STATE:
        return _STATE
    tmp = tempfile.TemporaryDirectory(prefix="db-capability-acceptance-")
    # Contain every config/catalog write this surface can make (config.json,
    # system/models.json) inside the fixture; the running tree is never touched.
    # NOTE: the data root must sit *beside* the instance root, not above it --
    # ``common.state_dir`` refuses a tenant root nested inside ``get_data_root()``
    # as a home/global escape (which would make ``/api/memory`` 503 in fixture).
    env = patch.dict(os.environ, {
        "COW_DATA_DIR": os.path.join(tmp.name, "data"),
        "COW_CREDENTIAL_MASTER_KEY": "00112233445566778899aabbccddeeff"
                                    "00112233445566778899aabbccddeeff",
    })
    env.start()
    app = WebAppHarness(os.path.join(tmp.name, "instance"))
    app.add_agent(ACME_AGENT)

    acme_id = app.tenant_id
    root_token = app.login("root")
    member_id = app.member("acmemember", ["member"])
    member_token = app.login("acmemember")

    service = app.service
    beta = service.create_tenant(
        actor_user_id=app.admin_id, code="globex", name="Globex",
        admin_username="globexadmin", admin_display="Globex Admin",
        admin_password=GLOBEX_ADMIN_TEMP,
        recent_password=IdentityStack.ROOT_PASSWORD,
        shared_root=os.path.join(tmp.name, "instance", "tenants", "globex"),
    )
    globex_id = beta["id"]
    service.change_password(
        service.login("globexadmin", GLOBEX_ADMIN_TEMP).token,
        GLOBEX_ADMIN_TEMP, GLOBEX_ADMIN_FINAL)
    globex_admin_token = service.login("globexadmin", GLOBEX_ADMIN_FINAL).token
    globex_admin_id = [
        m for m in service.list_members(globex_id)["items"]
        if m["username"] == "globexadmin"][0]["user_id"]
    app.add_agent(GLOBEX_AGENT, tenant_id=globex_id)

    created = service.create_member(
        actor_user_id=globex_admin_id, tenant_id=globex_id,
        operation="create-new", username="globexmember",
        display_name="Globex Member",
        temporary_password=IdentityStack.TEMP_PASSWORD, roles=["member"])
    temp = service.login("globexmember", IdentityStack.TEMP_PASSWORD).token
    service.change_password(temp, IdentityStack.TEMP_PASSWORD, MEMBER)
    globex_member_token = service.login("globexmember", MEMBER).token

    _STATE.update({
        "tmp": tmp, "env": env, "app": app,
        "acme_id": acme_id, "root_token": root_token,
        "member_id": member_id, "member_token": member_token,
        "globex_id": globex_id, "globex_admin_id": globex_admin_id,
        "globex_admin_token": globex_admin_token,
        "globex_member_id": created["user_id"],
        "globex_member_token": globex_member_token,
    })
    return _STATE


class _TwoTenantAcceptance(unittest.TestCase):
    """Two tenants, several users, one real ``build_web_app()``.

    Tenant A is the harness's bootstrapped ``acme`` (platform admin ``root``);
    tenant B is created through the ordinary platform-admin path, so its admin
    is a real tenant admin rather than a second platform account. Every request
    names its tenant per call, so one built app answers both.
    """

    @classmethod
    def setUpClass(cls):
        cls.state = _ensure_state()
        for name, value in cls.state.items():
            setattr(cls, name, value)

    # -- request helpers ---------------------------------------------------

    def _call(self, method, path, payload=None, token=None, tenant=None):
        headers = {"X-Tenant-ID": tenant} if tenant is not None else None
        if method == "GET":
            return self.app.get(path, token=token, headers=headers)
        return self.app.post(path, payload if payload is not None else {},
                             token=token, headers=headers)

    def _platform(self, path, payload=None, method="GET"):
        return self._call(method, path, payload, self.root_token, self.acme_id)

    def _models(self):
        response = self._platform("/api/models")
        self.assertEqual(_status(response), 200)
        return _body(response)

    def _config_file(self):
        with open(os.path.join(self.tmp.name, "data", "config.json"),
                  encoding="utf-8") as fh:
            return json.load(fh)


class PlatformPlaneAcceptance(_TwoTenantAcceptance):
    """模型 / 配置 / 渠道 / 日志：只有平台管理员可达。"""

    def test_the_platform_admin_reaches_every_platform_entry(self):
        for path in PLATFORM_PLANE:
            response = self._platform(path)
            self.assertEqual(_status(response), 200,
                             "%s answered %s for the platform admin: %s"
                             % (path, response.status, response.data[:200]))
            self.assertNotIn(_status(response), GATE_REFUSALS, path)

    def test_a_tenant_admin_is_refused_every_platform_entry(self):
        for path in PLATFORM_PLANE:
            response = self._call("GET", path, token=self.globex_admin_token,
                                  tenant=self.globex_id)
            self.assertEqual(_status(response), 403,
                             "%s answered %s for a tenant admin" % (path, response.status))

    def test_a_plain_member_is_refused_every_platform_entry(self):
        for path in PLATFORM_PLANE:
            response = self._call("GET", path, token=self.member_token,
                                  tenant=self.acme_id)
            self.assertEqual(_status(response), 403,
                             "%s answered %s for a plain member" % (path, response.status))

    def test_an_anonymous_caller_is_refused_every_platform_entry(self):
        for path in PLATFORM_PLANE:
            response = self._call("GET", path, tenant=self.acme_id)
            self.assertEqual(_status(response), 401,
                             "%s answered %s for an anonymous caller"
                             % (path, response.status))

    def test_a_platform_write_is_refused_to_a_tenant_admin(self):
        response = self._call(
            "POST", "/api/models",
            {"action": "set_search_credential", "provider": "searxng",
             "url": "https://evil.example"},
            token=self.globex_admin_token, tenant=self.globex_id)
        self.assertEqual(_status(response), 403)


class SearchProviderAcceptance(_TwoTenantAcceptance):
    """合并 Port 1：三个新搜索提供方在真实入口上可配置、可回读、可清除。"""

    def _provider(self, provider_id):
        search = self._models()["capabilities"]["search"]
        return next(p for p in search["providers"] if p["id"] == provider_id)

    def _save(self, payload):
        response = self._platform("/api/models", payload, method="POST")
        self.assertEqual(_status(response), 200)
        return _body(response)

    def test_searxng_takes_an_instance_url_and_reads_back_verbatim(self):
        saved = self._save({"action": "set_search_credential", "provider": "searxng",
                            "url": "https://searx.example.org"})
        self.assertEqual(saved.get("status"), "success")
        entry = self._provider("searxng")
        self.assertTrue(entry["configured"], entry)
        self.assertTrue(entry["needs_url"], entry)
        self.assertEqual(entry["url_masked"], "https://searx.example.org")

        cleared = self._save({"action": "set_search_credential", "provider": "searxng",
                              "url": ""})
        self.assertEqual(cleared.get("status"), "success")
        self.assertFalse(self._provider("searxng")["configured"])

    def test_keenable_reaches_its_keyless_tier_with_an_empty_key(self):
        saved = self._save({"action": "set_search_credential", "provider": "keenable",
                            "api_key": "", "anonymous": True})
        self.assertEqual(saved.get("status"), "success")
        entry = self._provider("keenable")
        self.assertTrue(entry["configured"], entry)
        self.assertTrue(entry["anonymous"], entry)
        self.assertTrue(entry["needs_dedicated_key"], entry)

        self._save({"action": "set_search_credential", "provider": "keenable",
                    "api_key": "", "anonymous": False})
        self.assertFalse(self._provider("keenable")["configured"])

    def test_a_real_key_turns_the_keyless_tier_off(self):
        self._save({"action": "set_search_credential", "provider": "keenable",
                    "api_key": "sk-acceptance-key", "anonymous": True})
        entry = self._provider("keenable")
        self.assertTrue(entry["configured"])
        self.assertFalse(entry["anonymous"], entry)
        self._save({"action": "set_search_credential", "provider": "keenable",
                    "api_key": ""})

    def test_an_unknown_provider_is_refused(self):
        result = self._save({"action": "set_search_credential",
                             "provider": "not-a-provider", "api_key": "x"})
        self.assertEqual(result.get("status"), "error")


class FallbackChainAcceptance(_TwoTenantAcceptance):
    """合并 Port 2：备用模型是有序链，写入的必须正是运行时会读的形状。"""

    def _save(self, payload):
        response = self._platform("/api/models", payload, method="POST")
        self.assertEqual(_status(response), 200)
        return _body(response)

    def _capability(self):
        return self._models()["capabilities"]["chat_fallback"]

    def test_a_two_link_chain_round_trips_and_lands_in_the_config_file(self):
        saved = self._save({
            "action": "set_capability", "capability": "chat_fallback",
            "enabled": True,
            "chain": [
                {"provider": "deepseek", "model": "deepseek-v4-flash"},
                {"provider": "zhipu", "model": "glm-4.5"},
            ],
        })
        self.assertEqual(saved.get("status"), "success")

        cap = self._capability()
        self.assertTrue(cap["enabled"], cap)
        self.assertEqual(
            [(link["provider"], link["model"]) for link in cap["chain"]],
            [("deepseek", "deepseek-v4-flash"), ("zhipu", "glm-4.5")],
        )
        # The runtime reads ``chat_fallback.chain`` from config.json; the console
        # writing the pre-chain shape would look saved and never apply.
        self.assertEqual(
            [(link["provider"], link["model"])
             for link in self._config_file()["chat_fallback"]["chain"]],
            [("deepseek", "deepseek-v4-flash"), ("zhipu", "glm-4.5")],
        )

        self._save({"action": "set_capability", "capability": "chat_fallback",
                    "enabled": False, "chain": []})
        self.assertFalse(self._capability()["enabled"])

    def test_enabling_with_no_usable_link_is_refused(self):
        result = self._save({"action": "set_capability", "capability": "chat_fallback",
                             "enabled": True, "chain": []})
        self.assertEqual(result.get("status"), "error")
        self.assertFalse(self._capability()["enabled"],
                         "a refused enable must not leave the fallback on")

    def test_half_filled_links_are_dropped_rather_than_blocking_the_save(self):
        self._save({
            "action": "set_capability", "capability": "chat_fallback",
            "enabled": True,
            "chain": [
                {"provider": "deepseek", "model": "deepseek-v4-flash"},
                {"provider": "", "model": ""},
                {"provider": "zhipu", "model": ""},
            ],
        })
        cap = self._capability()
        self.assertTrue(cap["enabled"])
        self.assertEqual([link["provider"] for link in cap["chain"]], ["deepseek"])
        self._save({"action": "set_capability", "capability": "chat_fallback",
                    "enabled": False, "chain": []})


class ModelCatalogAcceptance(_TwoTenantAcceptance):
    """模型目录覆盖：可保存、可回读，删提供方时一起清除（合并移植项）。"""

    PROVIDER = "deepseek"

    def _post(self, payload):
        response = self._platform("/api/models", payload, method="POST")
        self.assertEqual(_status(response), 200)
        return _body(response)

    def _card(self, provider_id):
        providers = self._models()["providers"]
        return next(p for p in providers if p["id"] == provider_id)

    def test_an_override_is_saved_and_reflected_on_read_back(self):
        saved = self._post({"action": "save_catalog", "provider_id": self.PROVIDER,
                            "models": [{"name": "deepseek-v4-flash"},
                                       {"name": "acceptance-only-model"}],
                            "hidden": []})
        self.assertEqual(saved.get("status"), "success", saved)
        names = [m["name"] if isinstance(m, dict) else m
                 for m in self._card(self.PROVIDER)["models"]]
        self.assertIn("acceptance-only-model", names)

        # Leave no override behind for the next case in this class.
        self._post({"action": "save_catalog", "provider_id": self.PROVIDER,
                    "models": [], "hidden": []})

    def test_a_hidden_model_disappears_from_the_card(self):
        # ``hidden`` tombstones a *preset*: a name that is also sent as an
        # override is an override, not a tombstone, so the model to hide must
        # come from the seed list.
        seeded = self._card(self.PROVIDER)["seed"]
        target = seeded[0]["name"] if isinstance(seeded[0], dict) else seeded[0]

        self._post({"action": "save_catalog", "provider_id": self.PROVIDER,
                    "models": [], "hidden": [target]})
        self.assertNotIn(target, self._card(self.PROVIDER)["models"])

        # Empty lists mean "back to presets" -- leave no tombstone behind.
        self._post({"action": "save_catalog", "provider_id": self.PROVIDER,
                    "models": [], "hidden": []})
        self.assertIn(target, self._card(self.PROVIDER)["models"])

    def test_deleting_a_custom_provider_drops_its_catalog(self):
        from models import model_catalog

        created = self._post({"action": "set_custom_provider",
                              "name": "Acceptance Probe",
                              "api_base": "https://probe.invalid/v1",
                              "model": "probe-model", "api_key": "sk-probe"})
        self.assertEqual(created.get("status"), "success", created)
        provider_id = created["id"]
        card_id = "custom:%s" % provider_id

        self._post({"action": "save_catalog", "provider_id": card_id,
                    "models": [{"name": "probe-model"},
                               {"name": "probe-extra"}],
                    "hidden": ["probe-extra"]})
        self.assertTrue(model_catalog.get_catalog(card_id))

        deleted = self._post({"action": "delete_custom_provider", "id": provider_id})
        self.assertEqual(deleted.get("status"), "success", deleted)
        self.assertEqual(model_catalog.get_catalog(card_id), [],
                         "a deleted provider's catalog must not survive to re-attach")
        self.assertNotIn(card_id, [p["id"] for p in self._models()["providers"]])


class VersionAcceptance(_TwoTenantAcceptance):
    def test_version_is_public_and_carries_the_local_metadata_payload(self):
        response = self._call("GET", "/api/version", tenant=self.acme_id)
        self.assertEqual(_status(response), 200, response.data[:200])
        payload = _body(response)
        self.assertIsInstance(payload.get("version"), str)
        self.assertTrue(payload["version"].strip())
        # The merge replaced a one-field stub with upstream's local payload.
        for key in ("install_kind", "update_supported", "platform"):
            self.assertIn(key, payload)


class TenantPlaneAcceptance(_TwoTenantAcceptance):
    """租户平面：成员可达；匿名与跨租户被拒。"""

    def test_a_member_reaches_every_tenant_entry(self):
        for path, _ in TENANT_PLANE:
            response = self._call("GET", path, token=self.member_token,
                                  tenant=self.acme_id)
            self.assertNotIn(
                _status(response), GATE_REFUSALS,
                "%s answered %s for a member of its own tenant: %s"
                % (path, response.status, response.data[:200]))
            self.assertNotEqual(_body(response).get("code"), "database_unavailable", path)

    def test_the_listing_entries_answer_the_member_with_a_success_payload(self):
        """Reachability alone is weak evidence; these answer with real data."""
        listing = [path for path, _ in TENANT_PLANE if path != "/api/history"]
        for path in listing:
            response = self._call("GET", path, token=self.member_token,
                                  tenant=self.acme_id)
            self.assertEqual(_status(response), 200, path)
            self.assertEqual(
                _body(response).get("status"), "success",
                "%s did not answer a member with a success payload: %s"
                % (path, response.data[:200]))

    def test_only_the_declared_while_closed_actions_are_closed(self):
        """The merge retrofitted database identity onto the new web layer.

        A ``closed`` policy would be the 503 the old console suffered from, so
        every route that predates this change must still declare an open policy
        for every method it serves. The eight actions of
        ``integrate-upstream-core-capabilities`` are the documented exception:
        they are registered while unopened, so the refusal is an authorization
        decision the projection can report instead of a 404 that reads as "no
        such feature". Any *other* closed route is the regression this asserts
        against.
        """
        from channel.web.route_registry import derive_route_policy

        closed = {(path, method)
                  for path, methods in derive_route_policy().items()
                  for method, entry in methods.items()
                  if entry.get("policy") == "closed"}
        expected = {(path, method)
                    for path, method, _ in DECLARED_WHILE_CLOSED}
        self.assertEqual(
            closed, expected,
            "closed routes must be exactly the declared-while-closed set: an "
            "unrelated route going closed is the old 503 symptom")

    def test_the_projection_agrees_with_the_closed_gate(self):
        """One declaration, two surfaces: the gate and the client projection.

        The spec forbids a capability having two independently maintained
        switches, so a route that the gate refuses must also read as
        unavailable to the client -- otherwise the console offers an entry whose
        every request answers 503.
        """
        from auth import capability_matrix
        from channel.web.route_registry import derive_route_policy

        policy = derive_route_policy()
        projection = capability_matrix.feature_action_availability()
        for path, method, action in DECLARED_WHILE_CLOSED:
            self.assertEqual(
                policy.get(path, {}).get(method, {}).get("policy"), "closed",
                "%s %s must be refused by the gate while %s is unopened"
                % (method, path, action))
            self.assertIn(action, projection, "%s must be projected" % action)
            self.assertFalse(
                projection[action]["available"],
                "%s is refused by the gate, so it cannot read as available"
                % action)

    def test_an_anonymous_caller_is_refused_every_tenant_entry(self):
        for path, _ in TENANT_PLANE:
            response = self._call("GET", path, tenant=self.acme_id)
            self.assertEqual(_status(response), 401,
                             "%s answered %s for an anonymous caller"
                             % (path, response.status))

    def test_a_cross_tenant_caller_is_refused_every_tenant_entry(self):
        for path, _ in TENANT_PLANE:
            response = self._call("GET", path, token=self.globex_member_token,
                                  tenant=self.acme_id)
            self.assertEqual(_status(response), 403,
                             "%s answered %s for a foreign tenant's member"
                             % (path, response.status))

    def test_a_tenant_header_is_required(self):
        for path, _ in TENANT_PLANE:
            response = self._call("GET", path, token=self.member_token, tenant="")
            self.assertIn(_status(response), (400, 403),
                          "%s answered %s without a tenant selection"
                          % (path, response.status))

    def test_the_chat_transport_is_reachable_for_a_member(self):
        response = self._call("POST", "/message", {"content": "acceptance probe"},
                              token=self.member_token, tenant=self.acme_id)
        self.assertNotIn(_status(response), GATE_REFUSALS,
                         "/message answered %s for a member: %s"
                         % (response.status, response.data[:200]))

    def test_the_chat_transport_is_refused_to_an_anonymous_caller(self):
        response = self._call("POST", "/message", {"content": "x"},
                              tenant=self.acme_id)
        self.assertEqual(_status(response), 401)


class KnownGapAcceptance(_TwoTenantAcceptance):
    """缺口如实呈现：未注册的仍是 404，注册未开放的必须是带判定的 503。"""

    def test_the_update_endpoints_are_not_routed(self):
        for method, path in UNROUTED:
            response = self._call(method, path, {}, token=self.root_token,
                                  tenant=self.acme_id)
            self.assertEqual(_status(response), 404,
                             "%s %s answered %s -- an unrouted entry went live"
                             % (method, path, response.status))

    def test_the_declared_while_closed_actions_are_refused_not_missing(self):
        """Registered-but-unopened must not be mistaken for unrouted.

        503 is the gate's own refusal, produced before any handler runs, and it
        is what makes the capability projection and the route agree. A 404 or
        405 here would mean the route was never registered (the projection would
        then be advertising nothing) or the catch-all swallowed it.
        """
        for path, method, action in DECLARED_WHILE_CLOSED:
            url = path.replace("(.*)", "x")
            response = self._call(method, url, {}, token=self.root_token,
                                  tenant=self.acme_id)
            self.assertEqual(
                _status(response), 503,
                "%s %s answered %s while %s is unopened -- a registered action "
                "must be refused by the gate, never reported as missing"
                % (method, url, response.status, action))


def tearDownModule():
    if not _STATE:
        return
    try:
        _STATE["app"].close()
    finally:
        _STATE["env"].stop()
        _STATE["tmp"].cleanup()
        _STATE.clear()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()