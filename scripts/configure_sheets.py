"""Install a downloaded service-account key into the local ignored .env."""

import argparse
import json
import re
import sys
from pathlib import Path

from channel_scout.config import ConfigError, Settings
from channel_scout.integrations import GoogleToken


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("downloaded_json", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    info = json.loads(args.downloaded_json.read_text(encoding="utf-8-sig"))
    if (not isinstance(info, dict) or info.get("type") != "service_account"
            or not str(info.get("client_email", "")).endswith(".iam.gserviceaccount.com")):
        raise ConfigError("Expected a Google service-account key JSON file.")
    settings = Settings(root)
    compact = json.dumps(info, separators=(",", ":"), ensure_ascii=True)
    settings.values["GOOGLE_SHEETS_SERVICE_ACCOUNT_JSON"] = compact
    GoogleToken.sheets(settings)
    env = root / ".env"
    if not env.is_file():
        raise ConfigError("Create the local .env from .env.example first.")
    content = env.read_text(encoding="utf-8-sig")
    key = "GOOGLE_SHEETS_SERVICE_ACCOUNT_JSON"
    pattern = re.compile(rf"^{key}=.*$", re.M)
    matches = pattern.findall(content)
    if len(matches) != 1 or matches[0] != f"{key}=":
        raise ConfigError("The local Sheets credential is already configured or its setting is missing.")
    env.write_text(pattern.sub(lambda _: f"{key}={compact}", content), encoding="utf-8")
    print("Sheets service-account credential installed in .env.")


if __name__ == "__main__":
    try:
        main()
    except (ConfigError, OSError, ValueError) as error:
        sys.exit(str(error) if isinstance(error, ConfigError) else f"Configuration failed ({type(error).__name__})")
