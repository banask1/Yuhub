"""Summarize Yuhub selftest JSON files.

Usage (run with the managed python, from the project root):

    python _summarize_selftests.py _fz_*.json          # frozen state
    python _summarize_selftests.py _st_*.json          # source state
    python _summarize_selftests.py --order update,theme,uninstall,gpu,share,node,memory,toast,screenshare,hosts,lan _fz_*.json

Why this exists: the PowerShell runner cannot parse the JSON itself -- calling
python from the PowerShell tool swallows stdout and a single stderr line aborts
the whole command (see project memory rule 9). So the runner only *runs* the
suites; all parsing happens here with bash python.

Exit code is 0 when every check passed and every expected json was present,
1 otherwise -- so it can be used as a gate.
"""
import glob
import json
import os
import sys

DEFAULT_ORDER = [
    "update", "theme", "uninstall", "gpu", "share", "node",
    "memory", "toast", "screenshare", "hosts",
]


def main(argv):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    order = list(DEFAULT_ORDER)
    args = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--order" and i + 1 < len(argv):
            order = [x for x in argv[i + 1].split(",") if x]
            i += 2
            continue
        args.append(a)
        i += 1

    paths = []
    for a in args:
        paths.extend(sorted(glob.glob(a)))
    if not paths:
        print("no json files matched")
        return 1

    # Map "<suite>" -> path by stripping the leading '_fz_' / '_st_' style
    # prefix (everything up to and including the last underscore before the
    # suite token is dropped; we simply match the known suite names).
    found = {}
    for p in paths:
        base = os.path.basename(p)[:-5]
        for s in order:
            if base == s or base.endswith("_" + s) or base == "_" + s:
                found[s] = p
                break
        else:
            found.setdefault(base, p)

    total_all = 0
    total_pass = 0
    bad = []
    missing = []
    for s in order:
        p = found.get(s)
        if p is None or not os.path.exists(p):
            missing.append(s)
            print("%-13s  MISSING" % s)
            continue
        with open(p, encoding="utf-8") as fh:
            d = json.load(fh)
        ck = d.get("checks", [])
        fails = [c for c in ck if not c.get("pass")]
        total_all += len(ck)
        total_pass += len(ck) - len(fails)
        print("%-13s %3d/%3d  %s" % (s, len(ck) - len(fails), len(ck),
                                     "OK" if not fails and d.get("ok") else "FAIL"))
        for c in fails[:10]:
            bad.append((s, c.get("name"), c.get("detail")))
    print("-" * 46)
    print("total %d/%d" % (total_pass, total_all))
    for s, n, dt in bad:
        print("  FAIL %s | %s | %s" % (s, n, dt))
    if missing:
        print("  MISSING: " + ", ".join(missing))
    return 0 if (not bad and not missing) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
