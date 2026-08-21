from __future__ import annotations

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
        self.assertIn("WEFT_RELEASE_SHA", self.text)
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

    def test_permissions_are_read_only_for_repository_contents(self) -> None:
        self.assertIn("permissions:\n  contents: read", self.text)
        self.assertNotIn("contents: write", self.text)
        self.assertNotIn("actions: write", self.text)


if __name__ == "__main__":
    unittest.main()
