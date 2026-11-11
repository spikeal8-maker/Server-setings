import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SSH = ROOT / "templates/asa-school-mcp-ssh-tunnel.service"
COMPOSE = ROOT / "templates/asa-school-mcp.compose.yaml"


class SchoolMCPConfigContract(unittest.TestCase):
    def test_ssh_tunnel_is_loopback_only(self):
        text = SSH.read_text(encoding="utf-8")
        self.assertIn("-L 127.0.0.1:39454:127.0.0.1:39443", text)
        self.assertIn("StrictHostKeyChecking=yes", text)
        self.assertIn("ExitOnForwardFailure=yes", text)
        self.assertIn("User=admins", text)
        self.assertIn("IPAddressDeny=any", text)
        self.assertIn("100.72.211.28/32", text)
        self.assertNotIn("0.0.0.0:39454", text)

    def test_gateway_has_independent_oauth_and_loopback(self):
        text = COMPOSE.read_text(encoding="utf-8")
        self.assertIn("container_name: asa-school-mcp-gateway", text)
        self.assertIn("LISTEN: 127.0.0.1:39453", text)
        self.assertIn("EXTERNAL_URL: https://school-mcp.asa-lab.ru", text)
        self.assertIn("http://127.0.0.1:39454", text)
        self.assertIn("school_oauth_data:/data", text)
        self.assertIn("PROXY_BEARER_TOKEN: ${SCHOOL_MCP_BACKEND_BEARER:?}", text)
        self.assertIn("PASSWORD: ${SCHOOL_MCP_LOGIN_PASSWORD:?}", text)
        self.assertIn("read_only: true", text)
        self.assertIn("no-new-privileges:true", text)
        self.assertIn("cap_drop:", text)
        self.assertNotIn("PANEL_ACCESS_TOKEN", text)
        self.assertNotIn("privileged: true", text)
        self.assertNotIn("0.0.0.0:39453", text)
        self.assertNotIn("asa-1panel-mcp-gateway", text)

    def test_references_keep_separate_test_and_school_endpoints(self):
        doc = (ROOT / "docs/29-SCHOOL-MCP-OAUTH.md").read_text(encoding="utf-8")
        self.assertIn("https://school-mcp.asa-lab.ru/mcp", doc)
        self.assertIn("https://test.asa-lab.ru/mcp", doc)
        self.assertIn("CLIENT OAUTH LOGIN NOT YET ACCEPTED", doc)
        self.assertIn("without `-v`", doc)


if __name__ == "__main__":
    unittest.main()
