#!/usr/bin/env python3
"""umbrel-ssh service: web UI + API + authorized_keys sync.

Python 3 stdlib only. Manages a collection of public SSH keys and renders
them into the authorized_keys files of the host users "root" and "umbrel".
"""

import ipaddress
import json
import os
import socket
import subprocess
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MANAGED_BEGIN = "# BEGIN managed by umbrel-ssh"
MANAGED_END = "# END managed by umbrel-ssh"

TARGETS = {
    "root": {"home": os.environ.get("ROOT_HOME", "/root"), "uid": 0, "gid": 0},
    "umbrel": {"home": os.environ.get("UMBREL_HOME", "/home/umbrel"), "uid": 1000, "gid": 1000},
}

KEYS_FILE = os.environ.get("KEYS_FILE", "/data/keys.json")
LOCK = threading.Lock()


# ---------------------------------------------------------------- helpers

def read_keys():
    """Load the key collection, returning an empty structure on error."""
    try:
        with open(KEYS_FILE, "r") as f:
            data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("keys"), list):
                return data
    except (OSError, ValueError):
        pass
    return {"keys": []}


def write_keys(data):
    """Persist the key collection atomically."""
    os.makedirs(os.path.dirname(KEYS_FILE), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(KEYS_FILE), prefix=".keys-")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, KEYS_FILE)


def validate_key(key_line):
    """Validate a public key line with ssh-keygen. Returns fingerprint or None."""
    if not key_line or len(key_line) > 4096:
        return None
    # reject anything that is not a plain key line
    if key_line.startswith("#") or key_line.startswith("-"):
        return None
    parts = key_line.split()
    if len(parts) < 2 or not all(
        c.isalnum() or c in "+/=" for c in parts[1]
    ):
        return None
    with tempfile.NamedTemporaryFile("w", suffix=".pub", delete=False) as f:
        f.write(key_line + "\n")
        path = f.name
    try:
        result = subprocess.run(
            ["ssh-keygen", "-l", "-f", path],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip()
        return None
    except (OSError, subprocess.SubprocessError):
        return None
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def parse_targets(raw):
    """Normalize a target list to valid targets; None means "all"."""
    if raw in (None, "", [""]):
        return list(TARGETS.keys())
    if not isinstance(raw, list):
        return None
    targets = [t for t in raw if t in TARGETS]
    return targets or None


def key_entry_line(entry):
    """The authorized_keys line for a collection entry."""
    comment = entry.get("comment") or entry["id"][:8]
    return f'{entry["key"]} {comment}'


def render_section(entries):
    lines = [MANAGED_BEGIN]
    for entry in entries:
        lines.append(key_entry_line(entry))
    lines.append(MANAGED_END)
    return "\n".join(lines)


def read_authorized(path):
    try:
        with open(path, "r") as f:
            return f.read()
    except OSError:
        return ""


def write_authorized(path, content, uid, gid):
    """Write authorized_keys with strict permissions and correct owner."""
    ssh_dir = os.path.dirname(path)
    os.makedirs(ssh_dir, mode=0o700, exist_ok=True)
    os.chmod(ssh_dir, 0o700)
    fd, tmp = tempfile.mkstemp(dir=ssh_dir, prefix=".authorized_keys-")
    with os.fdopen(fd, "w") as f:
        f.write(content)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    os.chmod(path, 0o600)
    try:
        os.chown(path, uid, gid)
        os.chown(ssh_dir, uid, gid)
    except (OSError, PermissionError):
        pass


def sync_target(target, entries):
    """Rebuild the managed section of one user's authorized_keys."""
    info = TARGETS[target]
    path = os.path.join(info["home"], ".ssh", "authorized_keys")
    existing = read_authorized(path)
    lines = existing.splitlines()

    # drop previous managed section, keep everything else
    if MANAGED_BEGIN in lines:
        begin = lines.index(MANAGED_BEGIN)
        end = lines.index(MANAGED_END) if MANAGED_END in lines else len(lines) - 1
        lines = lines[:begin] + lines[end + 1:]

    # remove trailing empty lines, append fresh managed section
    while lines and not lines[-1].strip():
        lines.pop()
    section = render_section(entries)
    content = "\n".join(lines + [section]) + "\n" if lines else section + "\n"
    write_authorized(path, content, info["uid"], info["gid"])
    return path


def sync_all():
    """Sync every target; returns dict target -> authorized_keys path."""
    data = read_keys()
    results = {}
    for target in TARGETS:
        entries = [k for k in data["keys"] if target in (k.get("targets") or TARGETS.keys())]
        results[target] = sync_target(target, entries)
    return results


def client_ip(handler):
    """Best-effort client IP for the UI (informational only)."""
    return handler.client_address[0]


# ---------------------------------------------------------------- http

class SshHandler(BaseHTTPRequestHandler):
    server_version = "umbrel-ssh/1.0"

    def send_body(self, code, content_type, body):
        self.send_response(code)
        self.send_header("Content-type", content_type)
        self.end_headers()
        self.wfile.write(body.encode())

    def send_json(self, code, obj):
        self.send_body(code, "application/json", json.dumps(obj, indent=2))

    def read_json_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 64 * 1024:
            return None
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None

    # --- API ---

    def api_keys(self, method):
        with LOCK:
            data = read_keys()
            if method == "GET":
                self.send_json(200, data)
                return

            body = self.read_json_body()
            if body is None:
                self.send_json(400, {"error": "invalid JSON body"})
                return

            if method == "POST":
                key_line = (body.get("key") or "").strip()
                fingerprint = validate_key(key_line)
                if fingerprint is None:
                    self.send_json(400, {"error": "invalid public key"})
                    return
                entry = {
                    "id": str(uuid.uuid4()),
                    "key": key_line,
                    "comment": (body.get("comment") or "").strip()[:100],
                    "targets": parse_targets(body.get("targets")) or list(TARGETS.keys()),
                    "added_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "fingerprint": fingerprint,
                }
                data["keys"].append(entry)
                write_keys(data)
                sync_all()
                self.send_json(201, entry)
                return

            key_id = self.path.split("/")[-1]
            found = next((k for k in data["keys"] if k["id"] == key_id), None)
            if not found:
                self.send_json(404, {"error": "key not found"})
                return

            if method == "PATCH":
                if "targets" in body:
                    targets = parse_targets(body["targets"])
                    if not targets:
                        self.send_json(400, {"error": "no valid targets"})
                        return
                    found["targets"] = targets
                if "comment" in body:
                    found["comment"] = (body["comment"] or "").strip()[:100]
                write_keys(data)
                sync_all()
                self.send_json(200, found)

    # --- routing ---

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/health":
            self.send_json(200, {"status": "ok"})
        elif path == "/api/keys":
            self.api_keys("GET")
        elif path == "/api/sync":
            with LOCK:
                results = sync_all()
            self.send_json(200, {"synced": results})
        elif path == "/":
            self.send_body(200, "text/html", render_page())
        else:
            self.send_error(404, "File Not Found")

    def do_POST(self):
        if self.path.split("?")[0] == "/api/keys":
            self.api_keys("POST")
        else:
            self.send_error(404, "File Not Found")

    def do_PATCH(self):
        if self.path.split("?")[0].startswith("/api/keys/"):
            self.api_keys("PATCH")
        else:
            self.send_error(404, "File Not Found")

    def do_DELETE(self):
        if self.path.split("?")[0].startswith("/api/keys/"):
            key_id = self.path.split("?")[0].split("/")[-1]
            with LOCK:
                data = read_keys()
                found = next((k for k in data["keys"] if k["id"] == key_id), None)
                if not found:
                    self.send_json(404, {"error": "key not found"})
                    return
                data["keys"].remove(found)
                write_keys(data)
                sync_all()
            self.send_json(200, {"deleted": key_id})
        else:
            self.send_error(404, "File Not Found")

    def log_message(self, fmt, *args):
        print(f"[{time.strftime('%H:%M:%S')}] {self.address_string()} {fmt % args}")


def render_page():
    return """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>umbrel-ssh</title>
<style>
  :root { color-scheme: light dark; }
  body { font-family: system-ui, sans-serif; max-width: 720px; margin: 32px auto; padding: 0 16px; line-height: 1.5; }
  h1 { font-size: 1.3rem; }
  form { display: flex; flex-direction: column; gap: 8px; margin: 16px 0; }
  textarea { font-family: ui-monospace, monospace; min-height: 80px; padding: 8px; }
  input[type=text] { padding: 6px 8px; }
  .row { display: flex; gap: 16px; align-items: center; }
  button { padding: 6px 14px; cursor: pointer; }
  .key { border: 1px solid color-mix(in srgb, currentColor 20%, transparent); border-radius: 8px; padding: 10px 12px; margin: 8px 0; }
  .key .meta { opacity: .7; font-size: .85rem; }
  .key .fingerprint { font-family: ui-monospace, monospace; font-size: .85rem; }
  .targets { display: flex; gap: 12px; margin-top: 6px; }
  .del { background: none; border: 1px solid #cf222e; color: #cf222e; border-radius: 6px; }
  .muted { opacity: .7; font-size: .85rem; }
  .error { color: #cf222e; }
</style>
</head>
<body>
<h1>SSH Keys</h1>
<p class="muted">Managed keys are written to <code>root</code> and <code>umbrel</code> authorized_keys on this host.</p>

<form id="add">
  <textarea id="key" placeholder="ssh-ed25519 AAAA... user@host" required></textarea>
  <div class="row">
    <input type="text" id="comment" placeholder="label (optional)">
    <label><input type="checkbox" id="t-root" checked> root</label>
    <label><input type="checkbox" id="t-umbrel" checked> umbrel</label>
    <button type="submit">Add key</button>
  </div>
  <p id="msg" class="error"></p>
</form>

<div id="list"></div>

<p class="muted">
  <a href="/api/keys">JSON</a> &middot;
  <button onclick="fetch('/api/sync',{method:'GET'}).then(load)">Re-sync now</button>
</p>

<script>
const esc = s => (s||'').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
async function load() {
  const data = await (await fetch('/api/keys')).json();
  const list = document.getElementById('list');
  list.innerHTML = data.keys.length ? '' : '<p class="muted">No keys yet.</p>';
  for (const k of data.keys) {
    const div = document.createElement('div');
    div.className = 'key';
    div.innerHTML = `
      <div class="fingerprint">${esc(k.fingerprint)}</div>
      <div class="meta">${esc(k.comment)} &middot; added ${esc(k.added_at)}</div>
      <div class="targets">
        <label><input type="checkbox" ${k.targets.includes('root')?'checked':''} onchange="setTargets('${k.id}', this.checked, document.getElementById('u-${k.id}').checked)"> root</label>
        <label><input type="checkbox" id="u-${k.id}" ${k.targets.includes('umbrel')?'checked':''} onchange="setTargets('${k.id}', document.getElementById('r-${k.id}')?.checked ?? this.checked, this.checked)"> umbrel</label>
        <button class="del" onclick="delKey('${k.id}')">Remove</button>
      </div>`;
    list.appendChild(div);
  }
}
async function addKey(e) {
  e.preventDefault();
  const msg = document.getElementById('msg');
  msg.textContent = '';
  const targets = [];
  if (document.getElementById('t-root').checked) targets.push('root');
  if (document.getElementById('t-umbrel').checked) targets.push('umbrel');
  const res = await fetch('/api/keys', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ key: document.getElementById('key').value, comment: document.getElementById('comment').value, targets })
  });
  if (!res.ok) { msg.textContent = (await res.json()).error; return; }
  document.getElementById('key').value = '';
  document.getElementById('comment').value = '';
  load();
}
async function setTargets(id, root, umbrel) {
  const targets = [];
  if (root) targets.push('root');
  if (umbrel) targets.push('umbrel');
  await fetch('/api/keys/' + id, {
    method: 'PATCH',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ targets })
  });
  load();
}
async function delKey(id) {
  await fetch('/api/keys/' + id, { method: 'DELETE' });
  load();
}
document.getElementById('add').addEventListener('submit', addKey);
load();
</script>
</body>
</html>
"""


def main():
    port = int(os.environ.get("SSH_PORT", 16161))
    # boot sync happens in entrypoint; run periodic re-sync as safety net
    def periodic():
        while True:
            time.sleep(300)
            with LOCK:
                sync_all()
    threading.Thread(target=periodic, daemon=True).start()
    httpd = ThreadingHTTPServer(("", port), SshHandler)
    print(f"umbrel-ssh listening on port {port}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
