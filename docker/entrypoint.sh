#!/bin/sh
set -e

echo "umbrel-ssh: syncing authorized_keys from key collection..."
python3 /app/umbrel_ssh_service.py --sync-only 2>/dev/null || true

# boot sync: render the collection into both authorized_keys files.
# The service itself also re-syncs periodically and on every change.
python3 - <<'EOF'
import os, sys
sys.path.insert(0, "/app")
os.environ.setdefault("KEYS_FILE", os.environ.get("KEYS_FILE", "/data/keys.json"))
import umbrel_ssh_service as svc
results = svc.sync_all()
for target, path in results.items():
    print(f"umbrel-ssh: synced {target} -> {path}", flush=True)
EOF

echo "umbrel-ssh: starting service..."
exec python3 /app/umbrel_ssh_service.py
