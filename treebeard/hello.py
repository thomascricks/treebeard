#!/usr/bin/env python3
"""
hello_treebeard.py — first light.

Target path:  /opt/treebeard/treebeard/hello.py
Run as:       the treebeard admin user, by hand, from the venv:
              /opt/treebeard/shared/venv/bin/python /opt/treebeard/treebeard/hello.py

What this proves, end to end, in one run:
  1. Constitution gate     — refuses to call any model if the law is absent
  2. Live local cognition  — real phi3:mini via Ollama (first non-echo response)
  3. Telemetry ledger      — the call lands as a row, printed back as proof

Deliberate scope limits (this is a hello world, not the agent):
  - Reads /etc/treebeard/constitution.txt directly with a hard exit, mirroring
    the shared enforcer's behavior. Production code goes through
    /opt/treebeard/shared/lib/constitution.py; this script stays dependency-
    light so first light can't be blocked by an API mismatch.
  - No charter, no router, no memory. One agent, one tier, one call.
  - stdlib only (urllib to localhost Ollama — no egress, no new deps).
"""

import json
import sys
import urllib.request
import urllib.error
import os as _tb_os
_TB_BASE = _tb_os.environ.get("TREEBEARD_BASE", "/opt/treebeard")
_TB_ETC = _tb_os.environ.get("TREEBEARD_ETC", "/etc/treebeard")

sys.path.insert(0, f"{_TB_BASE}/shared/lib")
import telemetry  # noqa: E402

CONSTITUTION_PATH = f"{_TB_ETC}/constitution.txt"
OLLAMA_URL = "http://127.0.0.1:11434/api/chat"
MODEL = "phi3:mini"
AGENT = "treebeard"
CHARTER_PATH = f"{_TB_ETC}/treebeard.charter.txt"

IDENTITY = (
    "You are Treebeard, the overseer agent of a small self-hosted multi-agent "
    "system. Temperament: the patience of an Ent, the craftsmanship of a dwarf. "
    "This is your first live exchange. Answer briefly."
)

DEFAULT_PROMPT = "Introduce yourself in two sentences, then say hello to the operator."


def load_constitution():
    """The gate. No law, no cognition. Mirrors the shared enforcer: hard exit."""
    try:
        with open(CONSTITUTION_PATH, "r", encoding="utf-8") as f:
            text = f.read().strip()
    except OSError as e:
        print(f"FATAL: constitution unreadable ({e}). Refusing to call any model.",
              file=sys.stderr)
        sys.exit(1)
    if not text:
        print("FATAL: constitution file is empty. Refusing to call any model.",
              file=sys.stderr)
        sys.exit(1)
    return text

def load_charter():
    """The charter. Identity, not law — softer failure: warn and continue."""
    try:
        with open(CHARTER_PATH, "r", encoding="utf-8") as f:
            text = f.read().strip()
        if not text:
            print(f"WARN: charter empty at {CHARTER_PATH}", file=sys.stderr)
            return ""
        return text
    except OSError as e:
        print(f"WARN: charter unreadable ({e}); proceeding without.", file=sys.stderr)
        return ""


def ollama_chat(system_prompt, user_prompt):
    """One non-streaming chat call to local Ollama. Returns the parsed response."""
    payload = {
        "model": MODEL,
        "stream": False,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    req = urllib.request.Request(
        OLLAMA_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "Treebeard-Hello/0.1",  # house style; harmless on localhost
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main():
    prompt = " ".join(sys.argv[1:]).strip() or DEFAULT_PROMPT

    # 1. Gate
    constitution = load_constitution()
    charter = load_charter()
    parts = [constitution]
    if charter:
        parts.append("=== CHARTER ===\n" + charter)
    parts.append(IDENTITY)
    system_prompt = "\n\n".join(parts)
    # 2. Cognition, wrapped in 3. Telemetry
    print(f"-> {MODEL} (local), constitution loaded "
          f"({len(constitution)} chars), prompt: {prompt!r}\n")
    try:
        with telemetry.timed_call(AGENT, "local", MODEL,
                                  router_reason="hello-world") as rec:
            resp = ollama_chat(system_prompt, prompt)
            rec.tokens_in = resp.get("prompt_eval_count")
            rec.tokens_out = resp.get("eval_count")
    except urllib.error.URLError as e:
        print(f"FATAL: Ollama unreachable at {OLLAMA_URL} ({e}).\n"
              f"Check: systemctl status ollama", file=sys.stderr)
        sys.exit(2)

    reply = (resp.get("message") or {}).get("content", "").strip()
    print("TREEBEARD SAYS:\n" + ("-" * 60))
    print(reply or "(empty response)")
    print("-" * 60)

    # Proof the ledger caught it: read back the newest row.
    conn = telemetry._connect()
    try:
        row = conn.execute(
            """SELECT ts_utc, agent, tier, model, tokens_in, tokens_out,
                      latency_ms, status
               FROM llm_calls ORDER BY id DESC LIMIT 1"""
        ).fetchone()
    finally:
        conn.close()
    keys = ("ts_utc", "agent", "tier", "model", "tokens_in",
            "tokens_out", "latency_ms", "status")
    print("\nLEDGER ROW:")
    print(json.dumps(dict(zip(keys, row)), indent=2))


if __name__ == "__main__":
    main()
