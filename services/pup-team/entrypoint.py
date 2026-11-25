#!/usr/bin/env python3
"""pup-team entrypoint: check the mounted OpenPup binary, write its config from the environment, start the runner.

OpenPup 0.2.x ignores the LLM env overrides on an empty workspace, so the config file is (re)written here on
every start from OPENPUP_API_KEY / OPENPUP_API_BASE / OPENPUP_MODEL / OPENPUP_MINI_MODEL / OPENPUP_LLM_PROVIDER.
Only the [llm] and [app] settings live in it; the pups' memory database in the same volume is kept.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def fail(msg: str) -> None:
    print(f"pup-team: {msg}", file=sys.stderr, flush=True)
    sys.exit(1)


def toml_str(v: str) -> str:
    return json.dumps(v, ensure_ascii=False)   # a JSON string literal is a valid TOML basic string


def main() -> None:
    env = lambda k, d="": os.environ.get(k) or d   # compose passes unset variables as empty strings
    binary = env("OPENPUP_BIN", "/opt/openpup/current/openpup")
    if not os.path.isfile(binary):
        fail(f"{binary} not found. On the host run: ./deploy/fetch-openpup.sh  (installs the OpenPup release "
             "under /opt/openpup), then restart this service.")
    try:
        version = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=20)
    except OSError as e:
        fail(f"{binary} cannot run here ({e}); wrong CPU architecture? Re-run deploy/fetch-openpup.sh on this host.")
    if version.returncode != 0:
        fail(f"{binary} --version failed: {version.stderr.strip()[:200]}")

    for var in ("AGORA_TOKEN", "OPENPUP_API_KEY"):
        if not os.environ.get(var):
            fail(f"{var} is not set (see .env.example: PUP_TEAM_TOKEN, OPENPUP_API_KEY)")

    home = Path(env("OPENPUP_HOME", "/data"))
    cfg_dir = home / ".openpup"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    model = env("OPENPUP_MODEL", "gpt-4o")
    lines = [
        "# written by pup-team/entrypoint.py on every start; edit the environment, not this file",
        "[llm]",
        f"provider   = {toml_str(env('OPENPUP_LLM_PROVIDER', 'openai'))}",
        f"model      = {toml_str(model)}",
        f"mini_model = {toml_str(env('OPENPUP_MINI_MODEL', model))}",
        f"api_key    = {toml_str(os.environ['OPENPUP_API_KEY'])}",
        f"api_base   = {toml_str(env('OPENPUP_API_BASE'))}",
        "",
        "[app]",
        'execution_mode = "leashed"',
        'language       = "zh"',
        "",
    ]
    cfg = cfg_dir / "config.toml"
    cfg.write_text("\n".join(lines), encoding="utf-8")
    cfg.chmod(0o600)
    print(f"pup-team: {version.stdout.strip()} · model {model} · arena {os.environ.get('AGORA_BASE_URL')}", flush=True)
    os.execvp(sys.executable, [sys.executable, "/app/runner/pup_team.py"])


if __name__ == "__main__":
    main()
