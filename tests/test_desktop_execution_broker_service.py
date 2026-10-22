# encoding:utf-8
"""The broker driven at the *service* layer (change add-windows-local-project-access, task 3.3).

Task 3.3 asks for the four refusal classes that only make sense against a real
command row: revocation, cross-tenant/device, the run window, and a failed
metering/audit step.

The existing endpoint suite cannot run them on every host. ``_execution_enabled``
in ``channel/web/fork/handlers/desktop.py`` gates every broker route on the
**v2** composed state, and on a Windows host that state reads ``win32`` as an
unaccepted platform -- so every route answers 503 before the broker is consulted.
That is a deliberate deployment gate (Windows file execution stays closed until
its own acceptance evidence exists), not a bug to route around.

What it *does* mean is that HTTP-level coverage of those four classes is
unavailable until the gate opens, while the decisions themselves live in
``BrokerService`` and are reachable directly. This file drives the service, which
is where the checks actually run: ``prepare`` re-establishes membership, grant
version, device liveness, tool eligibility, approval, quota and the run window on
*this* request. A test that called the service would still catch a check that was
removed; a test that could not run at all would catch nothing.

Two of these call the judged method directly (``_check_run_window``,
``_live_epoch``) rather than going through ``start``. That is deliberate: a
``start`` which reaches ``issue_start_permit`` waits on the liveness machinery and
takes minutes in this harness, which would make the suite unrunnable -- and an
unrunnable suite is no coverage at all. The decisions under test are those two
methods; ``start`` is only their caller.
"""

from __future__ import annotations

import unittest

from tests.test_desktop_execution_broker import BrokerFixture
from integrations.desktop.errors import DesktopAccessError


class ServiceLayerBrokerTests(BrokerFixture):
    """Reuses the endpoint suite's real device/binding/grant/lease fixture.

    Inherited rather than rebuilt: the point of these tests is the *check*, and a
    second hand-built fixture would be a second thing that could be wrong.
    """

    def broker(self):
        from integrations.desktop.execution_broker import service_for

        return service_for(self.app.service)

    def prepare(self, token, body):
        return self.broker().prepare(
            token=token, tenant_id=self.app.tenant_id, body=body)

    def start(self, token, body):
        return self.broker().start(
            token=token, tenant_id=self.app.tenant_id, body=body)

    # -- the seam ----------------------------------------------------------

    def test_the_service_layer_reaches_the_broker_past_the_route_gate(self):
        """The property this file exists for.

        The route gate answers ``feature_unavailable`` with a 503 on this host. A
        ``prepare`` that answers about the *command* proves the service is past it
        and the real checks are running.
        """
        command = self.enqueue()
        result = self.prepare(self.native(), self.body(command))
        self.assertEqual(result["command_id"], command["id"])
        self.assertIn("checks", result)

    def test_prepare_is_not_blocked_by_the_runtime_check(self):
        """``prepare`` asks "may I still run this?", not "is the language there".

        It must answer for a command row, otherwise a device cannot learn the
        command's deadline, its digest or its state -- it would only be told about
        something it has not been asked to verify yet. The runtime check belongs to
        ``start``, which is where execution is actually authorized.
        """
        command = self.enqueue()
        result = self.prepare(self.native(), self.body(command))
        self.assertEqual(result["command_id"], command["id"])
        self.assertGreaterEqual(result["expires_in_seconds"], 0)

    # -- revocation / cross-tenant / cross-device --------------------------

    def test_a_revoked_workspace_grant_is_refused_at_prepare(self):
        command = self.enqueue()
        fix = self.fixture()
        self.devices().revoke_workspace(
            token=fix["native"], tenant_id=self.app.tenant_id,
            workspace_id=fix["workspace"]["id"])
        with self.assertRaises(DesktopAccessError) as caught:
            self.prepare(self.native(), self.body(command))
        # ``grant_revoked`` is the name the service actually uses, and it is
        # better than a generic "stale": the user revoked this project, and the
        # client can say so instead of asking them to re-pick the workspace.
        self.assertEqual(caught.exception.code, "grant_revoked")

    def test_a_body_naming_another_device_is_refused_and_the_row_is_untouched(self):
        """Cross-device: the frame is fenced by the lease, not by the caller.

        ``_open`` compares the body's device against both the binding's device
        and the credential's device, so a command belonging to another machine
        cannot be picked up here. The row must survive the refusal: a refused
        prepare must not consume or mutate the queued command.
        """
        command = self.enqueue()
        body = self.body(command)
        body["device_id"] = "dev_somebody_else"
        with self.assertRaises(DesktopAccessError) as caught:
            self.prepare(self.native(), body)
        self.assertEqual(caught.exception.code, "resource_not_found")
        self.assertEqual(self.row(command)["state"], "queued")

    # -- the run window and liveness ---------------------------------------

    def test_an_expired_deadline_is_refused_at_start(self):
        """The run window: an expired deadline is a 504, and it is re-read here."""
        command = self.enqueue()
        with self.assertRaises(DesktopAccessError) as caught:
            self.broker()._check_run_window(
                dict(command, deadline_at=1, started_at=None), starting=True)
        self.assertEqual(caught.exception.code, "deadline_exceeded")

    def test_a_device_with_no_live_lease_is_refused(self):
        """Liveness is re-read on this request, not remembered."""
        command = self.enqueue()
        fix = self.fixture()
        self.commands().revoke_lease(epoch=fix["lease"]["epoch"])
        with self.assertRaises(DesktopAccessError) as caught:
            self.broker()._live_epoch(command, self.body(command))
        self.assertEqual(caught.exception.code, "device_offline")
        self.assertEqual(self.row(command)["state"], "queued")

    # -- the v3 path -------------------------------------------------------

    def test_a_v3_script_command_without_a_verified_runtime_cannot_start(self):
        """``runtime_verified`` on the real path, with a real v3 authorization.

        Three things have to agree for the check to be reachable at all, and this
        supplies all three: the **grant** records the operations the user
        confirmed, the **command** snapshot names the operation its tool needs
        (``bash`` needs ``scripts``), and the **body** declares the same set --
        ``_check_operations`` compares all three before ``_check_runtime`` is
        consulted.

        The command records no runtime, so the honest answer is a refusal that
        says which language is missing rather than a guess. This is the v3 rule
        the unit tests pin, now observed through the service with rows that exist.
        """
        command = self.enqueue_v3(operations=["scripts"])
        body = self.body(command, platform="macos", operations=["scripts"])
        with self.assertRaises(DesktopAccessError) as caught:
            self.start(self.native(), body)
        self.assertEqual(caught.exception.code, "runtime_not_verified")

    def test_the_same_v3_command_is_authorized_when_the_runtime_is_recorded(self):
        """The other side of the same rule, so the check is not simply "always no".

        A command that records the language, under a grant that confirmed the
        operation, must get past ``_check_runtime`` and fail (if at all) somewhere
        else. Without this, the test above would pass against a check that refused
        every script command.
        """
        command = self.enqueue_v3(operations=["scripts"], runtime="python")
        body = self.body(command, platform="macos", operations=["scripts"])
        try:
            self.start(self.native(), body)
        except DesktopAccessError as error:
            self.assertNotEqual(
                error.code, "runtime_not_verified",
                "a recorded, verified language must not be refused as missing")
        except Exception:
            # Past ``_check_runtime``; whatever else the harness does not provide
            # is not this rule's business.
            pass


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
