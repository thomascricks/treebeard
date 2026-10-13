#!/usr/bin/env python3
"""dn_write.py -- append ONE dev note to long-term memory AND to an on-disk
dev_notes.md. Note text arrives on stdin. Dual-write so the note is
retrievable by `cat` today (ptt/memscan recall is currently broken) and by
semantic recall once that is fixed. Run as the treebeard user so the memory
lands in longterm_treebeard. Invoked by the `dn` bash function.
"""
import sys, os, unicodedata, datetime, textwrap
import os as _tb_os
_TB_BASE = _tb_os.environ.get("TREEBEARD_BASE", "/opt/treebeard")
_TB_ETC = _tb_os.environ.get("TREEBEARD_ETC", "/etc/treebeard")
sys.path.insert(0, f"{_TB_BASE}/shared/lib")
import longterm

AGENT = 'treebeard'
NOTES_FILE = f"{_TB_BASE}/shared/memory/longterm/dev_notes.md"
EMBED_SAFE = 2800  # stay well under the embedder's ~28k-char HTTP-500 ceiling


def to_ascii(s):
    repl = {'\u2014': '--', '\u2013': '-', '\u2018': "'", '\u2019': "'",
            '\u201c': '"', '\u201d': '"', '\u2026': '...', '\u2192': '->',
            '\u2190': '<-', '\u00a0': ' ', '\u2011': '-', '\u2022': '*',
            '\u2010': '-', '\u00ad': ''}
    for k, v in repl.items():
        s = s.replace(k, v)
    return unicodedata.normalize('NFKD', s).encode('ascii', 'ignore').decode('ascii')


def chunk(s, size=EMBED_SAFE):
    if len(s) <= size:
        return [s]
    return textwrap.wrap(s, size, break_long_words=False, replace_whitespace=False)


def main():
    raw = sys.stdin.read().strip()
    if not raw:
        sys.exit('dn: empty note, nothing written.')
    note = to_ascii(raw)
    stamp = (datetime.datetime.now(datetime.timezone.utc)
             .isoformat(timespec='seconds').replace('+00:00', 'Z'))

    # 1) on-disk append FIRST -- must never be lost even if the LTM write fails
    disk_ok = False
    try:
        with open(NOTES_FILE, 'a', encoding='utf-8') as f:
            f.write("\n## %s\n%s\n" % (stamp, note))
            f.flush()
            os.fsync(f.fileno())
        disk_ok = True
    except Exception as e:
        sys.stderr.write("dn: WARNING disk append failed (%s: %s)\n"
                         % (type(e).__name__, e))

    # 2) long-term memory write (chunk only if a note is unexpectedly large)
    pieces = chunk(note)
    written = 0
    for i, piece in enumerate(pieces, 1):
        prov = ("dev-note:%s" % stamp) if len(pieces) == 1 \
            else ("dev-note:%s#chunk%dof%d" % (stamp, i, len(pieces)))
        r = longterm.consolidate(AGENT, piece, provenance=prov,
                                 source_kind='dev-note')
        if getattr(r, 'ok', False) and getattr(r, 'memory_id', None):
            written += 1
        else:
            sys.stderr.write("dn: LTM chunk %d/%d failed: %r\n"
                             % (i, len(pieces), getattr(r, 'error', None)))

    count = longterm._collection(AGENT).count()
    print("dn: disk=%s | LTM %d/%d chunk(s) | longterm_%s count=%d | %s"
          % ('ok' if disk_ok else 'FAIL', written, len(pieces), AGENT, count, stamp))


if __name__ == '__main__':
    main()
