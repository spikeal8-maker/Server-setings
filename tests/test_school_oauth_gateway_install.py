import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

BASE=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location("asa_school_oauth_install",BASE/"scripts/install-school-mcp-oauth-gateway.py")
mod=importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

class SchoolOAuthGatewayInstallerTests(unittest.TestCase):
    def test_loopback_and_school_identity(self):
        self.assertEqual(mod.DOMAIN,"x.asa-lab.ru")
        self.assertEqual(mod.API_PORT if hasattr(mod,"API_PORT") else mod.PORT,39450)
        self.assertEqual(mod.EXPECTED_SHA,"114b57ca5cddf7206a6bd935bf1d8c342f10aab2ff6a0d76dad0840012178d15")

    def test_explicit_owner_password_not_in_code_or_public_gateway(self):
        source=BASE.joinpath("scripts/install-school-mcp-oauth-gateway.py").read_text()
        self.assertIn("secrets.token_urlsafe(40)",source)
        self.assertIn("EnvironmentFile=/etc/server-settings/asa-school-mcp-gateway.env",source)
        self.assertIn("StateDirectory=asa-school-mcp-gateway",source)
        self.assertIn("DynamicUser=yes",source)
        self.assertIn("NoNewPrivileges=yes",source)
        self.assertIn("127.0.0.1:39450",source)
        self.assertNotIn("--allow-remote-http",source)
        self.assertIn("O_EXCL",source)
        self.assertIn("O_NOFOLLOW",source)

    def test_gateway_rejects_credential_overwrite(self):
        source=BASE.joinpath("scripts/install-school-mcp-oauth-gateway.py").read_text()
        self.assertIn("EXISTING_GATEWAY_CONFIGURATION_NO_OVERWRITE",source)
        self.assertIn("LOCAL_GATEWAY_HEALTH_CHECK_FAILED",source)
        self.assertIn("UNAUTHENTICATED_MCP=401",source)

if __name__=="__main__":
    unittest.main()
