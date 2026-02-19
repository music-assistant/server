#!/usr/bin/env bash
# Usage: MA_DEV_DATA=~/.musicassistant-dev ./scripts/seed-config.sh
# Requires env vars: YANDEX_TOKEN, KION_TOKEN, ZVUK_TOKEN

DATA_DIR="${MA_DEV_DATA:-$HOME/.musicassistant-dev}"
CONFIG="$DATA_DIR/settings.json"

if [ ! -f "$CONFIG" ]; then
  echo "→ Start MA once first to create settings.json"
  exit 1
fi

python3 - <<EOF
import json, pathlib, os

config_path = pathlib.Path("$CONFIG")
cfg = json.loads(config_path.read_text())

providers = cfg.setdefault("providers", {})
if "yandex_music_1" not in providers:
    providers["yandex_music_1"] = {
        "domain": "yandex_music",
        "instance_id": "yandex_music_1",
        "type": "music",
        "enabled": True,
        "values": {
            "token": os.environ.get("YANDEX_TOKEN", ""),
        }
    }

config_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
print("→ Config seeded")
EOF
