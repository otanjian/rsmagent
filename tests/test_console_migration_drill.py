# encoding:utf-8
"""8.3 演练：菜单与偏好迁移的幂等、中断补偿、显式撤权与恢复。

``test_console_menu_mapping.py`` 与 ``test_user_default_migration.py`` 固定的是
**迁移函数本身**的语义（``_migration_26`` / ``_migration_25`` 直接调用）。本文件演练的是
**发布过程**：把 1..24 版当成「升级前机器上真实存在的库」，然后在**真实的迁移执行器**
（``IdentityStore`` → ``_migrate``）上跑 25、26 两步，并按发布演练要求的四个问题下断言。

1. **重复执行**——重开库不产生第二次映射、第二条审计、第二次版本自增；已迁移的库与
   干净升级的库逐字段相同。
2. **中断补偿**——让审计 id 分配在**真实链条中间**抛出（不是像
   ``test_personal_delivery_drill`` D2 那样在链尾追加一条假迁移）。每版迁移各自一个事务，
   所以断在 26 里时 25 的列与修复已经落库、26 整体回滚；断在 25 里时连 ``ALTER TABLE``
   一起回滚。两种情况下的重试都必须与干净升级等价，且只生效一次。
3. **显式关闭被保留**——没有偏好的成员不被回填；管理员撤掉的正式菜单页不会被重启
   「补回来」，也不会借旧 id 复活。
4. **只读/运行分片的启用与恢复**——迁移本身不打开任何能力开关、不建运行实例；开关的
   撤回与一致性备份恢复由 ``test_personal_delivery_drill.py`` 的 D5–D8 覆盖（见
   ``evidence/8-3-migration-drill.md``），这里只钉住「迁移不启用分片」这一半。

本文件只做只读观测与临时库演练，不触碰生产数据，也不修改任何既有测试。
"""

import inspect
import os
import re
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import auth.store as store_module
from auth.store import IdentityStore, _migrations, migration_versions

#: 25/26 所在的链条位置：1..24 是「升级前机器上的库」。
PRE_UPGRADE = 24

MENU_MAP_ACTION = "role.menu.legacy_personal_mapped"
TENANT_REPAIR_ACTION = "tenant.default_agent.repaired"
MEMBER_REPAIR_ACTION = "member.default_agent.repaired"

#: 迁移在 ``_migration_25`` 里写的两条审计（一个租户默认修复 + 一个成员默认修复）。
#: 用来定位「链条中间」这个断点：第 3 次 id 分配就落在 26 的第一个角色上。
REPAIRS_INSIDE_25 = 2


def _build_legacy_store(path: str) -> None:
    """一个停在 24 版、且同时带着旧菜单授权与偏好缺陷的库。"""
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute(
        "CREATE TABLE schema_migrations("
        " version INTEGER NOT NULL,"
        " applied_at INTEGER NOT NULL DEFAULT (unixepoch()))")
    for index in range(PRE_UPGRADE):
        _migrations[index](con)

    for user_id in ("u_root", "u_alice", "u_bob", "u_carol", "u_dave", "u_erin"):
        con.execute(
            "INSERT INTO users(id, username, display_name, password_hash)"
            " VALUES(?,?,?,'x')", (user_id, user_id[2:], user_id[2:].title()))
    con.execute("INSERT INTO tenants(id, code, name, shared_root)"
                " VALUES('t1','acme','Acme','/s/acme')")
    con.execute("INSERT INTO tenants(id, code, name, shared_root)"
                " VALUES('t2','globex','Globex','/s/globex')")

    # acme: 一个租户共享 Agent、两个成员私有 Agent。
    con.execute("INSERT INTO agent_bindings(agent_id, tenant_id,"
                " private_owner_user_id) VALUES('shared-assistant','t1',NULL)")
    con.execute("INSERT INTO agent_bindings(agent_id, tenant_id,"
                " private_owner_user_id) VALUES('alice-assistant','t1','u_alice')")
    con.execute("INSERT INTO agent_bindings(agent_id, tenant_id,"
                " private_owner_user_id) VALUES('bob-assistant','t1','u_bob')")

    # 偏好：合法私有、合法共享、指向别人的私有、以及「明确没有偏好」。
    for membership_id, user_id, default in (
            ("m_alice", "u_alice", "alice-assistant"),
            ("m_carol", "u_carol", "shared-assistant"),
            ("m_erin", "u_erin", "bob-assistant"),
            ("m_dave", "u_dave", None),
    ):
        con.execute(
            "INSERT INTO memberships(id, tenant_id, user_id, display_name,"
            " default_agent_id) VALUES(?,?,?,?,?)",
            (membership_id, "t1", user_id, user_id, default))
    # 租户默认指向一个私有 Agent：迁移必须清掉指针、保留归属。
    con.execute("UPDATE tenants SET default_agent_id='alice-assistant' WHERE id='t1'")

    # 四种角色：两个内置（都带旧 id）、一个有旧 id 的自定义角色、一个完全没有旧 id。
    for role_id, code, builtin, version in (
            ("r_member", "member", 1, 7),
            ("r_admin", "tenant_admin", 1, 3),
            ("r_custom", "custom1", 0, 2),
            ("r_quiet", "custom2", 0, 1),
    ):
        con.execute(
            "INSERT INTO roles(id, tenant_id, code, name, builtin,"
            " permissions_json, version) VALUES(?,?,?,?,?,'[]',?)",
            (role_id, "t1", code, code.title(), builtin, version))
    for grant_id, role_id, kind, resource in (
            ("g1", "r_member", "menu", "nav:personal.agents"),
            ("g2", "r_member", "menu", "nav:personal.memory"),
            ("g3", "r_member", "menu", "nav:personal.tools"),
            ("g4", "r_member", "menu", "nav:personal.skills"),
            ("g5", "r_member", "menu", "nav:workbench.history"),
            ("g6", "r_member", "tool", "builtin:echo"),
            ("g7", "r_admin", "menu", "nav:personal.channels"),
            # 自定义角色已经手工拿到正式页,且另有非菜单授权。
            ("g8", "r_custom", "menu", "nav:personal.memory"),
            ("g9", "r_custom", "menu", "nav:admin.agents"),
            ("g10", "r_custom", "menu", "nav:workbench.chat"),
            ("g11", "r_quiet", "menu", "nav:workbench.history"),
    ):
        con.execute(
            "INSERT INTO role_resource_grants(id, tenant_id, role_id,"
            " resource_kind, resource_id, action) VALUES(?,?,?,?,?,?)",
            (grant_id, "t1", role_id, kind, resource,
             "execute" if kind == "tool" else "view"))

    con.execute("INSERT INTO schema_migrations(version) VALUES %s"
                % ",".join("(%d)" % (i + 1) for i in range(PRE_UPGRADE)))
    con.commit()
    con.close()


# --- 只读观测（绝不经过 IdentityStore：那会自动把 25/26 补上） -----------------


def _connect(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    return con


def _menu_grants(con, role_id):
    return sorted(
        r["resource_id"] for r in con.execute(
            "SELECT resource_id FROM role_resource_grants"
            " WHERE role_id=? AND resource_kind='menu'", (role_id,)))


def _role_version(con, role_id):
    return con.execute(
        "SELECT version FROM roles WHERE id=?", (role_id,)).fetchone()["version"]


def _columns(con, table):
    return {r["name"] for r in con.execute("PRAGMA table_info(%s)" % table)}


def _versions(path):
    con = _connect(path)
    try:
        return sorted(r["version"] for r in con.execute(
            "SELECT version FROM schema_migrations"))
    finally:
        con.close()


def _owners(path):
    con = _connect(path)
    try:
        return sorted((r["agent_id"], r["private_owner_user_id"]) for r in con.execute(
            "SELECT agent_id, private_owner_user_id FROM agent_bindings"))
    finally:
        con.close()


def _audit_actions(path):
    con = _connect(path)
    try:
        return {r["action"]: r["count"] for r in con.execute(
            "SELECT action, COUNT(*) AS count FROM audit_events GROUP BY action")}
    finally:
        con.close()


def _snapshot(path):
    """升级结果的完整指纹,用于「重试 == 干净升级」的等价断言。"""
    con = _connect(path)
    try:
        return {
            "schema": sorted(r["version"] for r in con.execute(
                "SELECT version FROM schema_migrations")),
            "grants": sorted(
                (r["role_id"], r["resource_kind"], r["resource_id"], r["action"])
                for r in con.execute(
                    "SELECT role_id, resource_kind, resource_id, action"
                    " FROM role_resource_grants")),
            "roles": sorted(
                (r["id"], r["version"]) for r in con.execute(
                    "SELECT id, version FROM roles")),
            "memberships": sorted(
                (r["id"], r["default_agent_id"], r["default_agent_revision"],
                 r["default_agent_origin"]) for r in con.execute(
                    "SELECT id, default_agent_id, default_agent_revision,"
                    " default_agent_origin FROM memberships")),
            "tenants": sorted(
                (r["id"], r["default_agent_id"]) for r in con.execute(
                    "SELECT id, default_agent_id FROM tenants")),
            "owners": sorted(
                (r["agent_id"], r["private_owner_user_id"]) for r in con.execute(
                    "SELECT agent_id, private_owner_user_id FROM agent_bindings")),
            "audit": sorted(
                (r["action"], r["target"], r["redacted_changes"]) for r in con.execute(
                    "SELECT action, target, redacted_changes FROM audit_events")),
        }
    finally:
        con.close()


def _crash_on_call(number):
    """第 ``number`` 次审计 id 分配时崩溃,模拟进程在链条中间倒下。"""
    real = store_module._new_migration_id
    state = {"n": 0}

    def flaky():
        state["n"] += 1
        if state["n"] == number:
            raise RuntimeError("simulated crash during the upgrade")
        return real()

    return flaky


class _DrillBase(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.path = os.path.join(self.root, "identity.db")
        _build_legacy_store(self.path)

    def _upgrade(self):
        return IdentityStore(self.path)

    def _crash(self, at_call):
        with patch.object(store_module, "_new_migration_id", _crash_on_call(at_call)):
            with self.assertRaises(RuntimeError):
                IdentityStore(self.path)


class MenuGrantMappingDrill(_DrillBase):
    """8.3 — 菜单授权迁移：映射、幂等、中断、不复活。"""

    def test_the_chain_maps_every_role_that_held_a_legacy_id(self):
        self._upgrade()
        con = _connect(self.path)
        try:
            # 断言写成「加法健壮」的形态:26 的映射结果必须在位,但链条上随后
            # 注册的其它版本可能再追加授权(观测时链尾已有 `_migration_27` 为内置
            # 角色补 `nav:admin.models`),所以这里钉的是「旧 id 全部消失、映射目标
            # 在位且不重复、版本恰好 +1」,而不是「集合恰好等于」。
            member = _menu_grants(con, "r_member")
            # member: 五个旧 id 变成三个正式页(工具/技能多对一)。
            self.assertEqual([g for g in member if g.startswith("nav:personal.")], [])
            for page in ("admin.agents", "admin.memory", "admin.skills"):
                self.assertEqual(member.count("nav:" + page), 1,
                                 "映射目标必须恰好一条(多对一不得重复): %s" % page)
            self.assertIn("nav:workbench.history", member, "原有正式页不得被收走")
            # 内置角色的版本只能断言「相对基线确有自增」:链条上随后注册的版本
            # 也会为内置角色追加授权(27 补 nav:admin.models、30 补外部系统接入、
            # 31 补 todo.assign),绝对值会随链尾增长而过期 —— 这正是上面注释说的
            # 「加法健壮」。自增的幂等性由 re-open 与 crash 两个用例逐字段覆盖。
            self.assertGreater(_role_version(con, "r_member"), 7)
            tools = con.execute(
                "SELECT COUNT(*) AS c FROM role_resource_grants"
                " WHERE role_id='r_member' AND resource_kind='tool'"
                " AND resource_id='builtin:echo'").fetchone()["c"]
            self.assertEqual(tools, 1, "非菜单授权不得被迁移触碰")

            admin = _menu_grants(con, "r_admin")
            self.assertEqual([g for g in admin if g.startswith("nav:personal.")], [])
            self.assertIn("nav:admin.channels", admin)
            self.assertGreater(_role_version(con, "r_admin"), 3)

            # 自定义角色:已手工持有的正式页不会重复插入,其它授权不动。
            self.assertEqual(
                _menu_grants(con, "r_custom"),
                ["nav:admin.agents", "nav:admin.memory", "nav:workbench.chat"])
            # 自定义角色不受任何内置角色补授迁移影响,所以这里可以钉绝对值:
            # 它恰好证明了「自定义角色一个都没被动过」。
            self.assertEqual(_role_version(con, "r_custom"), 3)

            # 没有旧 id 的角色:版本不动、没有任何被迁移的痕迹。
            self.assertEqual(_menu_grants(con, "r_quiet"), ["nav:workbench.history"])
            self.assertEqual(_role_version(con, "r_quiet"), 1)

            # 全库不再有任何旧个人页授权。
            legacy = con.execute(
                "SELECT COUNT(*) AS c FROM role_resource_grants"
                " WHERE resource_id LIKE 'nav:personal.%'").fetchone()["c"]
            self.assertEqual(legacy, 0)
        finally:
            con.close()

        self.assertEqual(_audit_actions(self.path).get(MENU_MAP_ACTION), 3,
                         "每个被映射的角色恰好一条审计")

    def test_reopening_after_the_chain_neither_re_maps_nor_re_audits(self):
        self._upgrade()
        before = _snapshot(self.path)
        for _ in range(2):
            IdentityStore(self.path)
        self.assertEqual(_snapshot(self.path), before,
                         "重复打开既不改授权,也不追加审计")

    def test_a_crash_inside_the_menu_step_rolls_the_whole_step_back(self):
        self._crash(at_call=REPAIRS_INSIDE_25 + 1)

        # 25 已经提交(每版迁移各自一个事务),26 整体回滚。
        self.assertEqual(_versions(self.path), list(range(1, PRE_UPGRADE + 1)) + [25])
        con = _connect(self.path)
        try:
            self.assertEqual(
                _menu_grants(con, "r_member"),
                ["nav:personal.agents", "nav:personal.memory", "nav:personal.skills",
                 "nav:personal.tools", "nav:workbench.history"],
                "断在 26 里时,旧 id 必须原样留在库里")
            self.assertEqual(_role_version(con, "r_member"), 7)
            self.assertEqual(_role_version(con, "r_admin"), 3)
            self.assertEqual(
                con.execute("SELECT default_agent_id FROM tenants"
                            " WHERE id='t1'").fetchone()["default_agent_id"], None,
                "25 的修复已经落库")
        finally:
            con.close()
        actions = _audit_actions(self.path)
        self.assertEqual(actions.get(MENU_MAP_ACTION, 0), 0,
                         "回滚的那一步不得留下审计")
        self.assertEqual(actions.get(TENANT_REPAIR_ACTION), 1)
        self.assertEqual(actions.get(MEMBER_REPAIR_ACTION), 1)

        # 重试就是一次普通的重启:结果与干净升级逐字段相同,且只生效一次。
        self._upgrade()
        self.assertEqual(_snapshot(self.path), _snapshot(self._clean_upgrade()),
                         "中断后重试必须与一次性升级等价")
        actions = _audit_actions(self.path)
        self.assertEqual(actions.get(MENU_MAP_ACTION), 3, "不得重复映射")
        self.assertEqual(actions.get(TENANT_REPAIR_ACTION), 1)
        self.assertEqual(actions.get(MEMBER_REPAIR_ACTION), 1)

    def _clean_upgrade(self):
        path = os.path.join(tempfile.mkdtemp(), "identity.db")
        _build_legacy_store(path)
        IdentityStore(path)
        return path

    def test_the_retry_after_a_crash_equals_a_clean_upgrade(self):
        """同一个断点,两条路径(断+重试 / 一次性)必须落在同一个状态上。"""
        self._crash(at_call=1)          # 断在 25 内部,连列一起回滚
        self._upgrade()
        self.assertEqual(_snapshot(self.path), _snapshot(self._clean_upgrade()))

    def test_a_crash_inside_the_preference_step_rolls_back_its_columns_too(self):
        self._crash(at_call=1)

        self.assertEqual(_versions(self.path), list(range(1, PRE_UPGRADE + 1)),
                         "回滚的版本不得被记录")
        con = _connect(self.path)
        try:
            columns = _columns(con, "memberships")
            self.assertNotIn("default_agent_revision", columns,
                             "失败版本的 ALTER TABLE 必须一起回滚")
            self.assertNotIn("default_agent_origin", columns)
            self.assertEqual(
                con.execute("SELECT default_agent_id FROM tenants"
                            " WHERE id='t1'").fetchone()["default_agent_id"],
                "alice-assistant")
            self.assertEqual(
                con.execute("SELECT default_agent_id FROM memberships"
                            " WHERE id='m_erin'").fetchone()["default_agent_id"],
                "bob-assistant")
        finally:
            con.close()
        self.assertEqual(_audit_actions(self.path), {},
                         "整步回滚后不得留下半条审计")

        # 重试:25 与 26 一起补上,偏好修复恰好一次。
        self._upgrade()
        con = _connect(self.path)
        try:
            self.assertIn("default_agent_origin", _columns(con, "memberships"))
            self.assertEqual(
                [(r["id"], r["default_agent_id"], r["default_agent_origin"])
                 for r in con.execute(
                     "SELECT id, default_agent_id, default_agent_origin"
                     " FROM memberships ORDER BY id")],
                [("m_alice", "alice-assistant", "user"),
                 ("m_carol", "shared-assistant", "user"),
                 ("m_dave", None, None),
                 ("m_erin", None, None)])
            self.assertEqual(
                con.execute("SELECT default_agent_id FROM tenants"
                            " WHERE id='t1'").fetchone()["default_agent_id"], None)
        finally:
            con.close()
        actions = _audit_actions(self.path)
        self.assertEqual(actions.get(TENANT_REPAIR_ACTION), 1)
        self.assertEqual(actions.get(MEMBER_REPAIR_ACTION), 1)
        self.assertEqual(actions.get(MENU_MAP_ACTION), 3,
                         "重试必须把整条链走完,而不是停在 25")

    def test_a_member_with_no_preference_is_never_backfilled(self):
        self._upgrade()
        self._upgrade()  # 再开一次,依然不得被租户默认填上
        con = _connect(self.path)
        try:
            row = con.execute("SELECT * FROM memberships WHERE id='m_dave'").fetchone()
        finally:
            con.close()
        self.assertIsNone(row["default_agent_id"],
                          "显式没有偏好必须保持没有偏好")
        self.assertIsNone(row["default_agent_origin"])

    def test_a_repair_keeps_the_private_owner_of_every_agent(self):
        before = _owners(self.path)
        self._upgrade()
        self.assertEqual(_owners(self.path), before,
                         "迁移不得清除任何 Agent 的私有归属")
        self.assertIn(("alice-assistant", "u_alice"), before)

    def test_the_upgrade_deletes_no_business_row(self):
        """退役不删除业务数据:升级只改菜单授权与无效指针。

        被删除的行**只允许**是旧个人菜单授权行;链条上后续版本追加行是允许的
        (例如 `_migration_27` 为内置角色补 `nav:admin.models`),所以这里断言
        「业务表行数不变 + 删除集合 ⊆ 旧个人菜单授权」,而不是行数必须变小。
        """
        con = _connect(self.path)
        try:
            before = {
                table: con.execute("SELECT COUNT(*) AS c FROM %s" % table).fetchone()["c"]
                for table in ("agent_bindings", "users", "tenants", "memberships", "roles")
            }
            grants_before = {
                (r["role_id"], r["resource_kind"], r["resource_id"])
                for r in con.execute(
                    "SELECT role_id, resource_kind, resource_id"
                    " FROM role_resource_grants")
            }
            non_menu_before = con.execute(
                "SELECT COUNT(*) AS c FROM role_resource_grants"
                " WHERE resource_kind != 'menu'").fetchone()["c"]
        finally:
            con.close()

        self._upgrade()

        con = _connect(self.path)
        try:
            after = {
                table: con.execute("SELECT COUNT(*) AS c FROM %s" % table).fetchone()["c"]
                for table in ("agent_bindings", "users", "tenants", "memberships", "roles")
            }
            grants_after = {
                (r["role_id"], r["resource_kind"], r["resource_id"])
                for r in con.execute(
                    "SELECT role_id, resource_kind, resource_id"
                    " FROM role_resource_grants")
            }
            non_menu_after = con.execute(
                "SELECT COUNT(*) AS c FROM role_resource_grants"
                " WHERE resource_kind != 'menu'").fetchone()["c"]
        finally:
            con.close()

        for table in ("agent_bindings", "users", "tenants", "memberships", "roles"):
            self.assertEqual(after[table], before[table],
                             "%s 的行数不得因升级改变" % table)
        self.assertEqual(non_menu_after, non_menu_before,
                         "非菜单授权一行都不得被删除")

        removed = grants_before - grants_after
        self.assertTrue(removed, "旧个人菜单授权必须被删除")
        for role_id, kind, resource_id in sorted(removed):
            self.assertEqual(kind, "menu", "被删除的不是菜单授权行: %s" % resource_id)
            self.assertTrue(resource_id.startswith("nav:personal."),
                            "被删除的只能是旧个人菜单授权行: %s/%s"
                            % (role_id, resource_id))

    def test_no_migration_in_the_chain_assigns_a_private_owner(self):
        """静态护栏:链条里没有任何一版迁移把 ``private_owner_user_id`` 写掉。

        「租户共享」只能是显式的、有审计的操作(``make_agent_tenant_shared``),
        绝不能是某次默认修复或菜单迁移的副作用。
        """
        assignment = re.compile(r"private_owner_user_id\s*=")
        copy_loss = re.compile(r"rebuild\(\s*[\"']agent_bindings[\"']")
        offenders = []
        rebuilt = []
        for index, migration in enumerate(_migrations, start=1):
            source = inspect.getsource(migration)
            if assignment.search(source):
                offenders.append(index)
            if copy_loss.search(source):
                rebuilt.append(index)
        self.assertEqual(offenders, [],
                         "这些迁移版本给 private_owner_user_id 赋了值: %s" % offenders)
        self.assertEqual(
            rebuilt, [],
            "这些迁移版本重建了 agent_bindings——重建的列拷贝清单一旦漏列,归属会被"
            "静默清空,而不会有任何赋值语句可查: %s" % rebuilt)

    def test_the_chain_does_not_enable_a_capability_switch_or_a_runtime_shard(self):
        """迁移不启用分片:不开开关、不建运行实例。"""
        from auth.policy import PERSONAL_CAPABILITY_DEFAULTS

        self.assertIs(PERSONAL_CAPABILITY_DEFAULTS["personal_channel_runtime"], False,
                      "运行开关的出厂值必须是关")
        self._upgrade()
        con = _connect(self.path)
        try:
            for table in ("tenant_channel_instances", "credentials"):
                count = con.execute(
                    "SELECT COUNT(*) AS c FROM %s" % table).fetchone()["c"]
                self.assertEqual(count, 0, "%s 不该被迁移写出行" % table)
        finally:
            con.close()

    def test_rolling_the_data_back_cannot_reopen_the_retired_state(self):
        """回滚必须是「重放升级」,而不是「回到旧行为」。

        把升级前的副本盖回 ``identity.db``(发布回滚最常见的形态)再启动当前代码,
        旧个人页授权与无法解析的默认指针必须被**重新收口**,而不是被当成现状接受。
        """
        import shutil

        legacy_copy = os.path.join(self.root, "pre-upgrade.db")
        shutil.copyfile(self.path, legacy_copy)

        self._upgrade()
        self.assertEqual(_audit_actions(self.path).get(MENU_MAP_ACTION), 3)

        # —— 回滚:拿升级前的数据盖回去 ——
        shutil.copyfile(legacy_copy, self.path)
        con = _connect(self.path)
        try:
            self.assertEqual(_role_version(con, "r_member"), 7)
            self.assertIn("nav:personal.memory", _menu_grants(con, "r_member"))
        finally:
            con.close()

        # —— 用当前代码再启动一次:旧状态不允许被恢复 ——
        self._upgrade()
        con = _connect(self.path)
        try:
            legacy = con.execute(
                "SELECT COUNT(*) AS c FROM role_resource_grants"
                " WHERE resource_id LIKE 'nav:personal.%'").fetchone()["c"]
            self.assertEqual(legacy, 0, "回滚不得让旧个人页授权重新生效")
            tenant_default = con.execute(
                "SELECT default_agent_id FROM tenants WHERE id='t1'").fetchone()
            self.assertIsNone(tenant_default["default_agent_id"])
            self.assertNotEqual(tenant_default["default_agent_id"], "shared-assistant",
                                "修复是把指针清空,不是替用户选一个共享 Agent")
            erin = con.execute("SELECT default_agent_id FROM memberships"
                               " WHERE id='m_erin'").fetchone()
            self.assertIsNone(erin["default_agent_id"],
                              "指向他人私有 Agent 的默认不得借回滚复活")
        finally:
            con.close()
        actions = _audit_actions(self.path)
        self.assertEqual(actions.get(MENU_MAP_ACTION), 3, "重放后恰好一次映射")
        self.assertEqual(actions.get(TENANT_REPAIR_ACTION), 1)
        self.assertEqual(actions.get(MEMBER_REPAIR_ACTION), 1)
        self.assertEqual(_owners(self.path),
                         [("alice-assistant", "u_alice"),
                          ("bob-assistant", "u_bob"),
                          ("shared-assistant", None)],
                         "重放升级不得改动归属")

    def test_a_withdrawn_menu_grant_is_not_restored_by_a_restart(self):
        self._upgrade()
        con = _connect(self.path)
        try:
            # 管理员显式撤掉「记忆管理」(并像 update_role 那样自增版本)。
            con.execute("DELETE FROM role_resource_grants WHERE role_id='r_member'"
                        " AND resource_kind='menu' AND resource_id='nav:admin.memory'")
            con.execute("UPDATE roles SET version=version+1 WHERE id='r_member'")
            con.commit()
            withdrawn = (_menu_grants(con, "r_member"), _role_version(con, "r_member"))
        finally:
            con.close()

        IdentityStore(self.path)
        IdentityStore(self.path)

        con = _connect(self.path)
        try:
            self.assertEqual((_menu_grants(con, "r_member"),
                              _role_version(con, "r_member")), withdrawn)
            self.assertNotIn("nav:personal.memory", _menu_grants(con, "r_member"),
                             "撤回正式页之后,旧 id 也不得复活")
        finally:
            con.close()


if __name__ == "__main__":
    unittest.main()
