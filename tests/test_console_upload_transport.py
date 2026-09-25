# encoding:utf-8
"""Console attachment transport in database mode.

The console uploads through ``POST /upload`` and reads thumbnails/audio back
through ``GET /uploads/(.*)``. Both are tenant routes, but only the first is a
``fetch`` the console can attach ``X-Tenant-ID`` to; the second is fetched by the
browser as an ``<img>``/``<audio>`` subresource and structurally cannot carry a
custom header.

These tests pin the contract at the two ends:

* the upload request returns a read-back address that names the *same* Agent it
  was written to (otherwise a non-default-Agent attachment 404s on read);
* the read-back route resolves its tenant from the addressed Agent's binding
  instead of demanding a header, while still enforcing membership and the
  Agent's ``agent.read`` grant.
"""

import json
import os
import tempfile
import unittest
from unittest.mock import patch

import web

import config
from agent.registry import AgentProfile, AgentRegistry
from agent.memory import clear_conversation_store_cache
from auth.service import IdentityService
from channel.web import web_channel
from tests._helpers import cookie_value as _cookie_value


def _multipart(fields, file_field, filename, content):
    """Build a multipart/form-data body web.py's test client can carry.

    ``web.application.request`` only accepts ``str`` and re-encodes it as UTF-8,
    so the parts are ASCII-only by construction and round-trip byte-for-byte.
    """
    boundary = "----cowuploadboundary"
    parts = []
    for key, value in fields.items():
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; "
            f'name="{key}"\r\n\r\n{value}\r\n'.encode("ascii"))
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; "
        f'name="{file_field}"; filename="{filename}"\r\n'
        f"Content-Type: image/png\r\n\r\n".encode("ascii") + content + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode("ascii"))
    return (b"".join(parts).decode("ascii"),
            f"multipart/form-data; boundary={boundary}")


class ConsoleUploadTransportTests(unittest.TestCase):
    """`POST /upload` and `GET /uploads/(.*)` under real database auth."""

    def setUp(self):
        from channel.web import auth_handlers
        auth_handlers.reset_login_rate_limiter()
        temporary = tempfile.TemporaryDirectory(prefix="console-upload-")
        self.addCleanup(temporary.cleanup)
        self.root = temporary.name
        self.db_path = os.path.join(temporary.name, "identity.db")
        self.service = IdentityService(self.db_path)
        tenant = self.service.bootstrap(
            tenant_code="acme", tenant_name="Acme", admin_username="root",
            admin_display="Root", admin_password="Str0ngAdminPass",
            shared_root=os.path.join(temporary.name, "acme"), allow_weak=True,
        )
        self.tenant_id = tenant["id"]
        self.admin_id = self.service.list_platform_users()[0]["id"]

        # A second, unrelated tenant with its own Agent: the caller is not a
        # member, so a resource-derived read must be refused.
        other = self.service.create_tenant(
            actor_user_id=self.admin_id, code="other", name="Other",
            shared_root=os.path.join(temporary.name, "other"),
            admin_username="other-root", admin_display="Other Root",
            admin_password="OtherStr0ngPass", recent_password="Str0ngAdminPass",
        )
        self.other_tenant_id = other["id"]

        # Two Agents in the caller's own tenant: a non-default one is the case
        # the old header-less read path silently resolved to the default.
        self.agent_workspace = os.path.join(temporary.name, "acme", "agents", "chat-agent")
        self.other_agent_workspace = os.path.join(temporary.name, "acme", "agents", "second-agent")
        self.third_workspace = os.path.join(temporary.name, "other", "agents", "foreign-agent")
        for path in (self.agent_workspace, self.other_agent_workspace, self.third_workspace):
            os.makedirs(os.path.join(path, "tmp"), exist_ok=True)
        registry = AgentRegistry([
            AgentProfile(id="chat-agent", name="Chat", workspace=self.agent_workspace),
            AgentProfile(id="second-agent", name="Second", workspace=self.other_agent_workspace),
            AgentProfile(id="foreign-agent", name="Foreign", workspace=self.third_workspace),
        ], "chat-agent")
        self.service.bind_agent(tenant_id=self.tenant_id, agent_id="chat-agent")
        self.service.bind_agent(tenant_id=self.tenant_id, agent_id="second-agent")
        self.service.bind_agent(tenant_id=self.other_tenant_id, agent_id="foreign-agent")

        settings = {
            "identity_mode": "database",
            "identity_db_path": self.db_path,
            "agent_workspace": os.path.join(temporary.name, "acme"),
        }
        for target in (config, web_channel):
            patcher = patch.object(target, "conf", return_value=settings)
            patcher.start()
            self.addCleanup(patcher.stop)
        registry_patch = patch("agent.registry.get_agent_registry", return_value=registry)
        registry_patch.start()
        self.addCleanup(registry_patch.stop)
        self.addCleanup(clear_conversation_store_cache)
        self.app = web_channel.build_web_app()

        login = self.app.request(
            "/auth/login", method="POST",
            headers={"Host": "localhost:9899", "Origin": "http://localhost:9899",
                     "Content-Type": "application/json"},
            data=json.dumps({"username": "root", "password": "Str0ngAdminPass"}),
        )
        self.assertEqual(login.status, "200 OK")
        self.token = _cookie_value(login, "cow_session")
        self.assertTrue(self.token)

    # -- helpers ---------------------------------------------------------

    def _headers(self, *, tenant=None, extra=None):
        headers = {
            "Host": "localhost:9899",
            "Origin": "http://localhost:9899",
            "Cookie": "cow_session=" + self.token,
        }
        if tenant:
            headers["X-Tenant-ID"] = tenant
        if extra:
            headers.update(extra)
        return headers

    def _upload(self, *, tenant=None, agent_id=None, content=b"hello-attachment"):
        fields = {"session_id": "ses-upload"}
        if agent_id:
            fields["agent_id"] = agent_id
        body, content_type = _multipart(fields, "file", "shot.png", content)
        return self.app.request(
            "/upload", method="POST", data=body,
            headers=self._headers(tenant=tenant, extra={"Content-Type": content_type}),
        )

    def _json(self, response):
        return json.loads(response.data.decode("utf-8"))

    # -- POST /upload ----------------------------------------------------

    def test_upload_without_tenant_selection_is_refused(self):
        """The gate still requires a selection: the console must send the header.

        This is why the frontend has to inject ``X-Tenant-ID`` for ``/upload`` —
        the browser request is otherwise indistinguishable from an anonymous one.
        """
        response = self._upload()
        self.assertEqual(int(response.status.split()[0]), 400)
        self.assertEqual(self._json(response)["code"], "missing_tenant")

    def test_upload_readback_address_names_the_written_agent(self):
        """``preview_url`` must address the Agent the file was written to."""
        response = self._upload(tenant=self.tenant_id, agent_id="second-agent")
        self.assertEqual(response.status, "200 OK")
        body = self._json(response)
        self.assertEqual(body["status"], "success")
        self.assertEqual(body["file_type"], "image")
        self.assertIn("?agent_id=second-agent", body["preview_url"])

    def test_upload_writes_into_the_authorized_agents_user_subtree(self):
        """Pinned against the write target, so a mismatch is not just cosmetic.

        Change ``isolate-shared-agent-user-data``: the target is still the
        *authorized* Agent's workspace, but the file now lands in the caller's
        own ``user/<user_id>/uploads`` so two members sharing the Agent cannot
        overwrite or read each other's attachments.
        """
        body = self._json(self._upload(tenant=self.tenant_id, agent_id="second-agent"))
        expected = os.path.join(
            os.path.realpath(self.other_agent_workspace),
            "user", self.admin_id, "uploads") + os.sep
        self.assertTrue(
            os.path.realpath(body["file_path"]).startswith(expected),
            body["file_path"])

    # -- GET /uploads/(.*) ----------------------------------------------

    def _seed_upload(self, workspace, name, content=b"image-bytes"):
        """Put a file where ``/uploads`` now reads from: the caller's own subtree."""
        path = os.path.join(workspace, "user", self.admin_id, "uploads", name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(content)
        return path

    def test_subresource_read_derives_tenant_from_agent_binding(self):
        """An <img> cannot send X-Tenant-ID; the Agent's binding supplies it."""
        self._seed_upload(self.other_agent_workspace, "thumb.png")
        response = self.app.request(
            "/uploads/thumb.png?agent_id=second-agent", method="GET",
            headers=self._headers(),
        )
        self.assertEqual(response.status, "200 OK", response.data)
        self.assertEqual(response.data, b"image-bytes")

    def test_subresource_read_refuses_a_non_member(self):
        """Deriving the tenant from the resource must not skip membership."""
        self._seed_upload(self.third_workspace, "foreign.png")
        response = self.app.request(
            "/uploads/foreign.png?agent_id=foreign-agent", method="GET",
            headers=self._headers(),
        )
        self.assertNotEqual(response.status, "200 OK")
        self.assertEqual(int(response.status.split()[0]), 403)
        self.assertNotIn(b"foreign", response.data)

    def test_subresource_read_hides_an_unbound_agent(self):
        """No resolvable binding -> not found, without leaking existence."""
        self._seed_upload(self.other_agent_workspace, "thumb.png")
        response = self.app.request(
            "/uploads/thumb.png?agent_id=nope-not-bound", method="GET",
            headers=self._headers(),
        )
        self.assertEqual(int(response.status.split()[0]), 404)

    def test_subresource_read_rejects_a_conflicting_selection(self):
        """An explicit selection that disagrees with the resource is a 400."""
        self._seed_upload(self.other_agent_workspace, "thumb.png")
        response = self.app.request(
            "/uploads/thumb.png?agent_id=second-agent", method="GET",
            headers=self._headers(tenant=self.other_tenant_id),
        )
        self.assertEqual(int(response.status.split()[0]), 400)
        self.assertEqual(self._json(response)["code"], "conflicting_tenant")

    def test_subresource_read_still_requires_credentials(self):
        """Resource derivation is not an authentication bypass."""
        self._seed_upload(self.other_agent_workspace, "thumb.png")
        response = self.app.request(
            "/uploads/thumb.png?agent_id=second-agent", method="GET",
            headers={"Host": "localhost:9899", "Origin": "http://localhost:9899"},
        )
        self.assertEqual(int(response.status.split()[0]), 401)

    def test_subresource_read_rejects_path_traversal(self):
        """Deriving the tenant must not widen the served path."""
        secret = os.path.join(self.root, "secret.png")
        with open(secret, "wb") as handle:
            handle.write(b"secret")
        response = self.app.request(
            "/uploads/..%2F..%2Fsecret.png?agent_id=second-agent", method="GET",
            headers=self._headers(),
        )
        self.assertNotEqual(response.status, "200 OK")
        self.assertNotIn(b"secret", response.data)


if __name__ == "__main__":
    unittest.main()
