from __future__ import annotations

import re
import unittest
from pathlib import Path


WORKFLOW = (
    Path(__file__).resolve().parents[1]
    / ".github"
    / "workflows"
    / "deploy-production.yml"
)
PREFLIGHT = WORKFLOW.parents[2] / "scripts" / "deploy_preflight.py"


class ProductionDeployWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = WORKFLOW.read_text(encoding="utf-8")

    def _step_block(self, name: str) -> str:
        pattern = rf"(?ms)^      - name: {re.escape(name)}\n.*?(?=^      - name:|\Z)"
        match = re.search(pattern, self.text)
        self.assertIsNotNone(match, f"workflow step missing: {name}")
        return match.group(0)  # type: ignore[union-attr]

    def _step_order(self, *names: str) -> list[int]:
        return [self.text.index(f"      - name: {name}\n") for name in names]

    def _step_env_value(self, block: str, name: str) -> str:
        match = re.search(rf"(?m)^\s+{re.escape(name)}: (.+)$", block)
        self.assertIsNotNone(match, f"step environment value missing: {name}")
        return match.group(1).strip()  # type: ignore[union-attr]

    def test_workflow_is_manual_only(self) -> None:
        self.assertIn("\non:\n  workflow_dispatch:\n", self.text)
        self.assertNotIn("pull_request:", self.text)
        self.assertNotIn("push:", self.text)

    def test_workflow_is_approval_gated_and_serialized(self) -> None:
        self.assertIn("environment:\n      name: production", self.text)
        self.assertIn("group: weft-production-deploy", self.text)
        self.assertIn("cancel-in-progress: false", self.text)
        self.assertIn("timeout-minutes: 45", self.text)

    def test_workflow_deploys_merged_main_without_checkout_credentials(self) -> None:
        self.assertIn("ref: ${{ github.sha }}", self.text)
        self.assertIn("persist-credentials: false", self.text)
        self.assertNotIn("github.head_ref", self.text)
        self.assertNotIn("github.ref_name", self.text)

    def test_required_configuration_fails_closed(self) -> None:
        self.assertIn("scripts/deploy_preflight.py", self.text)
        self.assertIn("WEFT_RELEASE_REF", self.text)
        self.assertIn("WEFT_RELEASE_SHA", self.text)
        self.assertIn('--release-ref "$WEFT_RELEASE_REF"', self.text)
        self.assertIn("github.ref", self.text)
        self.assertTrue(PREFLIGHT.exists())
        preflight = PREFLIGHT.read_text(encoding="utf-8")
        for name in (
            "WEFT_VM",
            "WEFT_NGINX_CONF",
            "WEFT_NGINX_SERVER_NAME",
            "PUBLIC_ORIGIN",
            "WEFT_API_ORIGIN",
            "WEFT_SITE_URL",
            "WEFT_SSH_PRIVATE_KEY_CONTENT",
            "WEFT_SSH_KNOWN_HOSTS_CONTENT",
            "WEFT_MCP_PROBE_TOKEN",
        ):
            self.assertIn(name, preflight)

    def test_ssh_material_uses_verified_known_hosts_and_ephemeral_cleanup(self) -> None:
        self.assertIn("secrets.WEFT_SSH_PRIVATE_KEY", self.text)
        self.assertIn("secrets.WEFT_SSH_KNOWN_HOSTS", self.text)
        job_env = self.text.split("jobs:", 1)[1].split("steps:", 1)[0]
        self.assertNotIn("runner.temp", job_env)
        self.assertIn("${{ runner.temp }}/weft-deploy-key", self.text)
        self.assertIn("${{ runner.temp }}/weft-known-hosts", self.text)
        self.assertIn("chmod 600", self.text)
        self.assertIn("if: always()", self.text)
        self.assertIn('rm -f -- "$WEFT_SSH_KEY" "$WEFT_SSH_KNOWN_HOSTS"', self.text)
        self.assertNotIn("ssh-keyscan", self.text)
        self.assertNotIn("StrictHostKeyChecking=no", self.text)

    def test_cutover_is_followed_by_both_live_release_gates(self) -> None:
        self.assertIn("actions/upload-artifact@v4", self.text)
        self.assertIn("weft-preflight.json", self.text)
        self.assertIn("scripts/push-code-to-vm.sh", self.text)
        self.assertIn("scripts/probe_live_release.py", self.text)
        self.assertIn("scripts/probe_hosted_mcp_surface.py", self.text)
        self.assertIn("secrets.WEFT_MCP_PROBE_TOKEN", self.text)
        self.assertIn("WEFT_API_ORIGIN", self.text)
        self.assertIn("WEFT_SITE_URL", self.text)
        self.assertIn("scripts/rollback-weft.sh", self.text)

    def test_reached_cutover_failure_rolls_back_before_credential_cleanup(self) -> None:
        order = self._step_order(
            "Run the fail-closed VM cutover",
            "Verify public API and site release",
            "Verify authenticated hosted MCP catalog",
            "Roll back failed cutover or release gate",
            "Remove ephemeral SSH credentials",
        )
        self.assertEqual(order, sorted(order))
        cutover = self._step_block("Run the fail-closed VM cutover")
        rollback = self._step_block("Roll back failed cutover or release gate")
        self.assertIn("id: cutover", cutover)
        self.assertIn("failure()", rollback)
        self.assertIn("steps.cutover.outcome != 'skipped'", rollback)
        self.assertIn("StrictHostKeyChecking=yes", rollback)
        self.assertIn('UserKnownHostsFile=$WEFT_SSH_KNOWN_HOSTS', rollback)
        self.assertIn("bash /opt/weft/scripts/rollback-weft.sh", rollback)
        self.assertNotIn("continue-on-error: true", rollback)

    def test_preflight_artifact_and_credential_setup_precede_cutover(self) -> None:
        order = self._step_order(
            "Validate deployment configuration",
            "Upload redacted deployment preflight",
            "Prepare ephemeral SSH credentials",
            "Run the fail-closed VM cutover",
        )
        self.assertEqual(order, sorted(order))
        upload = self._step_block("Upload redacted deployment preflight")
        self.assertIn("if: always()", upload)
        self.assertIn("if-no-files-found: error", upload)

    def test_live_probes_follow_cutover_and_cleanup_runs_last(self) -> None:
        order = self._step_order(
            "Run the fail-closed VM cutover",
            "Verify public API and site release",
            "Verify authenticated hosted MCP catalog",
            "Roll back failed cutover or release gate",
            "Remove ephemeral SSH credentials",
        )
        self.assertEqual(order, sorted(order))
        cleanup = self._step_block("Remove ephemeral SSH credentials")
        self.assertIn("if: always()", cleanup)
        self.assertIn('rm -f -- "$WEFT_SSH_KEY" "$WEFT_SSH_KNOWN_HOSTS"', cleanup)

    def test_probe_steps_fail_closed_before_workflow_can_finish(self) -> None:
        public_probe = self._step_block("Verify public API and site release")
        mcp_probe = self._step_block("Verify authenticated hosted MCP catalog")
        self.assertIn("scripts/probe_live_release.py", public_probe)
        self.assertIn("scripts/probe_hosted_mcp_surface.py", mcp_probe)
        self.assertIn("set -euo pipefail", mcp_probe)
        self.assertIn('[[ -n "${WEFT_MCP_PROBE_TOKEN:-}" ]]', mcp_probe)

    def test_validation_cutover_and_probes_cannot_become_fail_open(self) -> None:
        guarded_steps = (
            "Validate deployment configuration",
            "Prepare ephemeral SSH credentials",
            "Run the fail-closed VM cutover",
            "Verify public API and site release",
            "Verify authenticated hosted MCP catalog",
        )
        for name in guarded_steps:
            with self.subTest(step=name):
                block = self._step_block(name)
                self.assertNotIn("if: always()", block)
                self.assertNotIn("continue-on-error: true", block)
                self.assertNotIn("|| true", block)

    def test_preflight_receives_probe_token_before_ssh_materialization(self) -> None:
        validation = self._step_block("Validate deployment configuration")
        validation_index, ssh_index = self._step_order(
            "Validate deployment configuration", "Prepare ephemeral SSH credentials",
        )
        self.assertLess(validation_index, ssh_index)
        self.assertIn("WEFT_MCP_PROBE_TOKEN: ${{ secrets.WEFT_MCP_PROBE_TOKEN }}", validation)

    def test_checkout_sha_and_preflight_expected_sha_are_one_data_flow(self) -> None:
        checkout = self._step_block("Check out merged main")
        validation = self._step_block("Validate deployment configuration")
        checkout_index, validation_index = self._step_order(
            "Check out merged main", "Validate deployment configuration",
        )
        self.assertLess(checkout_index, validation_index)
        self.assertIn("ref: ${{ github.sha }}", checkout)
        self.assertIn("WEFT_RELEASE_SHA: ${{ github.sha }}", validation)
        self.assertIn('--expected-sha "$WEFT_RELEASE_SHA"', validation)

    def test_ssh_cleanup_reuses_preparation_paths_exactly(self) -> None:
        prepare = self._step_block("Prepare ephemeral SSH credentials")
        cleanup = self._step_block("Remove ephemeral SSH credentials")
        for name in ("WEFT_SSH_KEY", "WEFT_SSH_KNOWN_HOSTS"):
            with self.subTest(name=name):
                self.assertEqual(
                    self._step_env_value(prepare, name),
                    self._step_env_value(cleanup, name),
                )

    def test_permissions_are_read_only_for_repository_contents(self) -> None:
        self.assertIn("permissions:\n  contents: read", self.text)
        self.assertNotIn("contents: write", self.text)
        self.assertNotIn("actions: write", self.text)


if __name__ == "__main__":
    unittest.main()
