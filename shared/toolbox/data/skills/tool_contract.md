# TOOL CONTRACT (toolbox room)

Every promoted tool the toolbox runs must obey this. It is the room's
side of the bargain; the manifest is treebeard's.

ENTRY   The room executes `python3 /tool/main.py <args...>`. Your tool
        directory is mounted read-only at /tool. Working directory is
        /scratch (writable). /tmp is a small tmpfs.

DOOR    The only way out is the socket at $TOOLBOX_DOOR. Protocol: open
        a connection, send ONE JSON line, read ONE JSON line, close.
          {"v":1, "skill":"state", "args":{"card":"meeseeks"}}
          {"v":1, "skill":"router", "args":{"system":"...", "user":"...",
            "tier":"free", "max_tokens":800}}
          {"v":1, "skill":"web_search", "args":{"query":"...", "count":3}}
        Responses: {"ok":true, "result":...} or {"ok":false, "error":"..."}.
        A skill your manifest does not grant is refused; budgets are
        enforced host-side and attempts count. Handle "ok": false --
        refusal is a normal answer, not a crash.

RESULT  Write durable output under /scratch/result/ using temp-then-
        rename (write name.tmp, os.replace to name). The host records
        every result file with its sha256. stdout/stderr are captured to
        the run's logs; print progress there.

LIMITS  No network. No filesystem outside /tool (ro), /scratch, /tmp.
        A wall clock kills the run at manifest wall_seconds; if your
        work can die mid-write, temp-then-rename is what keeps the
        result pool uncorrupted.

STYLE   Plain ASCII. Comment intent, not noise. Standard library only
        unless your task said otherwise (the room image carries the
        document libraries below when it does).
