"""Teams Bot Framework authentication configuration."""
import os


class TeamsBotConfig:
    def __init__(self):
        self.APP_ID = os.environ.get("TEAMS_APP_ID", "")
        self.APP_PASSWORD = os.environ.get("TEAMS_APP_PASSWORD", "")
        self.APP_TENANTID = os.environ.get("TEAMS_APP_TENANT_ID", "")
        self.APP_TYPE = "SingleTenant" if self.APP_TENANTID else "MultiTenant"
