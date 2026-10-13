#!/usr/bin/env python3
# agents.py -- UI-CHAT agent registry. Identity is CONFIGURATION, not
# presentation logic: display name, role, portrait and fallback all come
# from data so the same identity renders identically in the selector,
# direct chat, meetings, transcripts and attention UI.
#
# WHO EXISTS is not this module's opinion. An agent exists iff it has a
# charter file -- the same test meet.parse_agents enforces and the same
# file ask.py resolves identity from. Roster metadata for a name without a
# charter is ignored, so the registry can never offer a phantom
# participant the meeting layer would refuse (or worse, convene).
import html
import json
import os as _tb_os
_TB_BASE = _tb_os.environ.get("TREEBEARD_BASE", "/opt/treebeard")
_TB_ETC = _tb_os.environ.get("TREEBEARD_ETC", "/etc/treebeard")

ROSTER_PATH = _tb_os.environ.get(
    "TREEBEARD_UI_ROSTER", f"{_TB_ETC}/ui_agents.json")
PORTRAIT_DIR = f"{_TB_BASE}/shared/ui/portraits"

# Fallback metadata for chartered agents absent from the roster file. Role
# text is deliberately generic: the charter is the authority on what an
# agent IS, and paraphrasing it here would create a second description
# that silently drifts.
_DEFAULT_ROLE = "Treebeard agent"

_PALETTE = ("#3d8bfd", "#d9a441", "#5bbf8a", "#c678dd", "#e0533d", "#4dc4d9")


def charter_dir():
    return _TB_ETC


def existing_agents():
    """Agent ids with a charter on disk, sorted. The authoritative roster."""
    try:
        names = [f[:-len(".charter.txt")]
                 for f in _tb_os.listdir(charter_dir())
                 if f.endswith(".charter.txt")]
    except OSError:
        return []
    return sorted(n for n in names if n and n.replace("_", "").isalnum())


def _roster_file():
    try:
        with open(ROSTER_PATH) as fh:
            data = json.load(fh)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k).lower(): v for k, v in data.items()
            if isinstance(v, dict)}


def _initials(name):
    parts = [p for p in str(name).replace("-", " ").split() if p]
    return "".join(p[0] for p in parts[:2]).upper() or "?"


def _colour(agent_id):
    return _PALETTE[sum(ord(c) for c in agent_id) % len(_PALETTE)]


def portrait_for(agent_id):
    """Portrait URL path, or None. A missing file is not an error -- the
    caller renders the deterministic fallback instead."""
    for ext in ("png", "jpg", "jpeg", "webp", "svg"):
        p = _tb_os.path.join(PORTRAIT_DIR, "%s.%s" % (agent_id, ext))
        if _tb_os.path.isfile(p):
            return "/portrait/%s.%s" % (agent_id, ext)
    return None


def get(agent_id):
    """Identity record for one agent, or None if it has no charter."""
    agent_id = str(agent_id or "").strip().lower()
    if agent_id not in existing_agents():
        return None
    meta = _roster_file().get(agent_id, {})
    name = str(meta.get("name") or agent_id.capitalize())
    return {"id": agent_id,
            "name": name,
            "role": str(meta.get("role") or _DEFAULT_ROLE),
            "portrait": portrait_for(agent_id),
            "initials": _initials(name),
            "colour": str(meta.get("colour") or _colour(agent_id))}


def roster():
    """Every existing agent, as identity records."""
    return [get(a) for a in existing_agents()]


def avatar(agent_id, size=32):
    """Canonical avatar markup: portrait when one exists, deterministic
    initials otherwise. One implementation, every surface."""
    a = get(agent_id)
    if a is None:
        return ('<span class="avatar avatar-unknown" role="img" '
                'aria-label="Unknown agent" style="width:%dpx;height:%dpx">?'
                '</span>' % (size, size))
    if a["portrait"]:
        return ('<img class="avatar" src="%s" alt="%s" width="%d" '
                'height="%d">' % (html.escape(a["portrait"]),
                                  html.escape(a["name"]), size, size))
    return ('<span class="avatar" role="img" aria-label="%s" '
            'style="width:%dpx;height:%dpx;background:%s">%s</span>'
            % (html.escape(a["name"]), size, size,
               html.escape(a["colour"]), html.escape(a["initials"])))


def _selftest():
    real = existing_agents()
    assert real, "no chartered agents found"
    for aid in real:
        a = get(aid)
        assert a and a["id"] == aid and a["name"] and a["role"]
        assert a["initials"] and a["colour"].startswith("#")
        assert avatar(aid) == avatar(aid)
        assert 'class="avatar"' in avatar(aid)
    # A name without a charter is not an agent, whatever the roster says.
    assert get("nonexistent_agent_xyz") is None
    assert get("") is None and get(None) is None
    assert "Unknown agent" in avatar("nonexistent_agent_xyz")
    # Deterministic fallback: same id always the same colour/initials.
    assert _colour("clark") == _colour("clark")
    assert _initials("Clark Kent") == "CK" and _initials("han") == "H"
    # Identity strings are escaped: a roster entry cannot inject markup.
    import json as _json, tempfile as _tf
    global ROSTER_PATH
    _saved = ROSTER_PATH
    try:
        with _tf.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            _json.dump({real[0]: {"name": "<script>x</script>",
                                  "role": "a & b"}}, fh)
            ROSTER_PATH = fh.name
        ev = get(real[0])
        assert ev["name"] == "<script>x</script>"      # stored raw
        av = avatar(real[0])
        assert "<script>" not in av and "&lt;script&gt;" in av
    finally:
        ROSTER_PATH = _saved
        _tb_os.unlink(fh.name)
    assert len(roster()) == len(real)
    print("agents selftest: ALL PASS (%d chartered: %s)"
          % (len(real), ", ".join(real)))


if __name__ == "__main__":
    if not __debug__:
        raise SystemExit("agents: selftest requires asserts (no -O)")
    _selftest()
