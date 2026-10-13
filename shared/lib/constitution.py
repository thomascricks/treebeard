import sys
import os as _tb_os
_TB_BASE = _tb_os.environ.get("TREEBEARD_BASE", "/opt/treebeard")
_TB_ETC = _tb_os.environ.get("TREEBEARD_ETC", "/etc/treebeard")

CONSTITUTION_PATH = f"{_TB_ETC}/constitution.txt"

def load():
    try:
        with open(CONSTITUTION_PATH, "r") as f:
            return f.read().strip()
    except Exception as exc:
        print(f"FATAL: Constitution unreadable: {exc}", file=sys.stderr)
        sys.exit(1)

def prepend(system_prompt: str) -> str:
    constitution = load()
    return f"{constitution}\n\n---\n\n{system_prompt}"
