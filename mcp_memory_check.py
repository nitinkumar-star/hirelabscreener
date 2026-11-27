"""
Manual check of the 4 Memory tools against the LIVE ATS (Wave 2).

It calls the same functions Claude's connector uses (mcp_server.tool_*), logged
in with the connector's own service account, so a PASS here means the tools
work end to end. It creates ONE test note and archives it at the end (nothing
is hard-deleted; the archived test note stays visible under "show archived").

Easiest place to run it: Render -> your MCP service -> "Shell" tab (the env
vars are already set there):
    python mcp_memory_check.py

Or on your own computer (Python 3.10+):
    pip install httpx starlette uvicorn
    set ATS_BASE_URL=https://hirelabscreener.onrender.com      (Windows; use export on Mac/Linux)
    set ATS_USERNAME=<service account username>
    set ATS_PASSWORD=<service account password>
    python mcp_memory_check.py
"""
import os
import sys
import json
import datetime

os.environ.setdefault("MCP_SECRET", "local-check")       # not used here, avoids a warning

import mcp_server as m                                    # noqa: E402

results = []


def step(name, fn):
    try:
        out = fn()
        results.append((name, True, ""))
        print(f"PASS  {name}")
        return out
    except Exception as e:                                # show the reason, keep going
        results.append((name, False, str(e)))
        print(f"FAIL  {name}: {e}")
        return None


def main():
    missing = [k for k in ("ATS_BASE_URL", "ATS_USERNAME", "ATS_PASSWORD") if not os.environ.get(k)]
    if missing:
        print("Set these first:", ", ".join(missing))
        sys.exit(2)
    print(f"ATS: {m.ATS_BASE_URL}   (memory writes switch on this service: {m.ALLOW_MEMORY_WRITES})\n")
    stamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    tomorrow = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()

    def recall():
        r = m.tool_recall_memory(limit=5)
        assert "count" in r, r
        return r
    step("recall_memory (list)", recall)

    def context():
        ms = m.tool_list_mandates("active").get("mandates") or []
        if not ms:
            r = m.tool_get_memory_context(client="test")
            assert r.get("type") == "client", r
            return r
        r = m.tool_get_memory_context(mandate_id=ms[0]["id"])
        assert r.get("type") == "mandate", r
        return r
    step("get_memory_context", context)

    def bad_context():
        try:
            m.tool_get_memory_context()
        except RuntimeError as e:
            assert "exactly one" in str(e)
            return True
        raise AssertionError("expected a refusal")
    step("get_memory_context refuses no argument", bad_context)

    saved = step("save_memory (create)", lambda: m.tool_save_memory(
        scope="general", title=f"Connector check {stamp}",
        content=f"Automated Wave 2 check note {stamp}. Safe to ignore.", tags=["connector-check"],
        importance="low", valid_until=tomorrow))
    nid = (saved or {}).get("id")
    if saved:
        assert saved["action"] == "created" and saved["note"].get("source") == "claude", saved

    def dedupe():
        r = m.tool_save_memory(scope="general", title=f"connector check {stamp}",
                               content=f"Automated Wave 2 check note {stamp} (updated). Safe to ignore.")
        assert r["id"] == nid and r["action"] == "updated", r
        return r
    if nid:
        step("save_memory (dedupe -> same note)", dedupe)

        def update():
            r = m.tool_save_memory(note_id=nid, importance="normal")
            assert r["action"] == "updated" and r["note"]["importance"] == "normal", r
            return r
        step("save_memory (note_id update)", update)

        def find():
            r = m.tool_recall_memory(query=stamp)
            assert any(n["id"] == nid for n in r["notes"]), r
            return r
        step("recall_memory finds the note", find)

        def archive():
            r = m.tool_archive_memory(nid)
            assert r["status"] == "archived", r
            assert not any(n["id"] == nid for n in m.tool_recall_memory(query=stamp)["notes"])
            return r
        step("archive_memory", archive)

    print()
    bad = [r for r in results if not r[1]]
    print(f"{len(results) - len(bad)}/{len(results)} passed")
    if bad:
        print(json.dumps([{"step": n, "error": e} for n, _, e in bad], indent=2))
        sys.exit(1)


if __name__ == "__main__":
    main()
