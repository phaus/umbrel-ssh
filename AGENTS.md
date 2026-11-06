# AGENTS.md — umbrel-ssh

Instructions for AI agents working in this repository.

## Project overview

Umbrel app (`phaus-ssh`) that manages SSH public keys for the host users
`root` and `umbrel`. Python 3 stdlib only — no external dependencies, no
build step.

## Structure

```
umbrel-app.yml        # umbrel app manifest, id: phaus-ssh, port: 16161
docker-compose.yml    # privileged, mounts /root and /home, port 16161
docker/
  umbrel_ssh_service.py   # HTTP server: web UI + API + key sync
  entrypoint.sh           # boot sync, then run service in foreground
keys.json             # (runtime, mounted) key collection — DO NOT commit real keys
```

## Key concepts

- **Key collection** (`keys.json`) is the single source of truth. Schema:

```json
{
  "keys": [
    {
      "id": "<uuid>",
      "key": "ssh-ed25519 AAAA... comment",
      "comment": "optional label from the UI",
      "targets": ["root", "umbrel"],
      "added_at": "2026-11-06T14:00:00Z"
    }
  ]
}
```

- **Sync** = deterministic rebuild of `authorized_keys` from the collection.
  Runs at container start (boot phase of umbrel) and after every add/remove.
  Managed markers (comment prefix `# managed by umbrel-ssh`) wrap the app's
  section so foreign keys in the same file are preserved.
- **Validation**: every key is checked with `ssh-keygen -l -f` before it
  enters the collection. Invalid keys are rejected with a 400 response.
- **Permissions**: `.ssh` dir `700`, `authorized_keys` `600`. Files for
  `umbrel` get `uid 1000/gid 1000` (chown), `root` files stay `0:0`.

## Conventions

- Python: stdlib only (`http.server`, `json`, `subprocess`, `os`, `uuid`).
- Keep the web UI dependency-free: inline CSS, no JS frameworks, vanilla
  `fetch` calls against `/api/*`.
- Port: `16161` (env `SSH_PORT`). Follow the naming pattern of the
  sibling app `qemu-ga-container` (STATS_PORT=8080 → here SSH_PORT).
- Commits: conventional commits (`feat:`, `fix:`, `docs:`, `ci:`).
- Version in `umbrel-app.yml` and the released docker tag must match.

## Never do this

- Never commit real public keys or `keys.json` contents (use fixtures with
  `AAA...` placeholder bodies in tests).
- Never weaken file permissions or write `authorized_keys` without the
  managed marker section.
- Never run the service on a port below 1024 or map it outside the host.
- Never remove or reorder keys in `authorized_keys` that are outside the
  managed section (Umbrel itself or the user may have placed them there).

## Testing

```bash
# local run without mounts (mock mode, writes to ./mock-home)
docker build -t umbrel-ssh . && docker run --rm -p 16161:16161 umbrel-ssh
curl http://localhost:16161/api/keys
```

CI (GitHub Actions) builds the image and runs a smoke test against the
`/api/health` endpoint. Releases are tagged `v<version>` and pushed to
`ghcr.io/phaus/umbrel-ssh`.
