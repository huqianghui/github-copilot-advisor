import os
import unittest
from unittest.mock import patch

from teams_adapter.config import TeamsBotConfig


class TeamsBotConfigTests(unittest.TestCase):
    def test_tenant_id_selects_single_tenant_authentication(self):
        env = {
            "TEAMS_APP_ID": "app-id",
            "TEAMS_APP_PASSWORD": "secret",
            "TEAMS_APP_TENANT_ID": "tenant-id",
        }
        with patch.dict(os.environ, env, clear=True):
            config = TeamsBotConfig()

        self.assertEqual(config.APP_ID, "app-id")
        self.assertEqual(config.APP_PASSWORD, "secret")
        self.assertEqual(config.APP_TYPE, "SingleTenant")
        self.assertEqual(config.APP_TENANTID, "tenant-id")

    def test_config_preserves_legacy_multi_tenant_default(self):
        with patch.dict(os.environ, {}, clear=True):
            config = TeamsBotConfig()

        self.assertEqual(config.APP_TYPE, "MultiTenant")
        self.assertEqual(config.APP_TENANTID, "")
