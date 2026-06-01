#!/usr/bin/env python3
"""Pre-submission safety scan for Fullhouse bots.

Checks each bot.py for:
  1. BANNED imports (the qualifier's actual ban list, from the org email):
     threading, multiprocessing, pickle, subprocess, sockets, requests
  2. DANGEROUS constructs: eval/exec/compile/__import__, os.system/popen,
     file-write opens, reflection (getattr/setattr/delattr) [warn-only].
  3. Python 3.10 compatibility: flags 3.11+/3.12+ syntax (except*, PEP 695
     `type X = ...` and generic type params) since the sandbox is Python 3.10.

Exit code 0 = clean (no BANNED/DANGEROUS hard failures). Non-zero = blockers.
Usage: python3 harden_scan.py <bot.py> [<bot.py> ...]
"""
import ast, sys

BANNED = {"threading", "multiprocessing", "pickle", "_pickle", "cPickle",
          "subprocess", "socket", "_socket", "ssl", "requests",
          "_thread", "asyncio"}
# Network/serialization adjacents — warn, not hard-fail (not on the email list
# but worth a human glance):
WARN_MODULES = {"urllib", "http", "ftplib", "telnetlib", "smtplib",
                "shelve", "marshal", "ctypes", "mmap"}
DANGER_CALLS = {"eval", "exec", "compile", "__import__"}
DANGER_ATTR = {("os", "system"), ("os", "popen"), ("os", "fork"),
               ("os", "spawnl"), ("os", "spawnv")}
REFLECT_CALLS = {"getattr", "setattr", "delattr"}  # warn-only
WRITE_MODES = {"w", "a", "x", "wb", "ab", "xb", "w+", "r+", "a+"}


def scan(path):
    src = open(path, encoding="utf-8", errors="replace").read()
    try:
        tree = ast.parse(src, filename=path)
    except SyntaxError as e:
        return [("BLOCK", f"SyntaxError: {e}")], []
    blockers, warns = [], []

    for node in ast.walk(tree):
        # --- imports ---
        if isinstance(node, ast.Import):
            for a in node.names:
                root = a.name.split(".")[0]
                if root in BANNED:
                    blockers.append(("BANNED import", a.name, node.lineno))
                elif root in WARN_MODULES:
                    warns.append(("warn import", a.name, node.lineno))
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root in BANNED:
                blockers.append(("BANNED from-import", node.module, node.lineno))
            elif root in WARN_MODULES:
                warns.append(("warn from-import", node.module, node.lineno))
        # --- calls ---
        elif isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name):
                if f.id in DANGER_CALLS:
                    blockers.append(("DANGEROUS call", f.id + "()", node.lineno))
                elif f.id in REFLECT_CALLS:
                    warns.append(("reflection", f.id + "()", node.lineno))
                elif f.id == "open":
                    mode = _open_mode(node)
                    if mode in WRITE_MODES:
                        blockers.append(("FILE WRITE", f"open(mode={mode!r})", node.lineno))
            elif isinstance(f, ast.Attribute):
                base = f.value
                if isinstance(base, ast.Name) and (base.id, f.attr) in DANGER_ATTR:
                    blockers.append(("DANGEROUS call", f"{base.id}.{f.attr}()", node.lineno))
        # --- Python 3.11+/3.12+ syntax (sandbox is 3.10) ---
        elif node.__class__.__name__ == "TryStar":              # 3.11 except*
            blockers.append(("PY3.11 syntax", "except* (TryStar)", getattr(node, "lineno", "?")))
        elif node.__class__.__name__ == "TypeAlias":            # 3.12 `type X = ...`
            blockers.append(("PY3.12 syntax", "type-alias statement", getattr(node, "lineno", "?")))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if getattr(node, "type_params", None):              # 3.12 PEP 695 generics
                blockers.append(("PY3.12 syntax", f"generic type params on {node.name}", node.lineno))

    return blockers, warns


def _open_mode(call):
    # positional arg 1 or keyword 'mode'
    if len(call.args) >= 2 and isinstance(call.args[1], ast.Constant):
        return call.args[1].value
    for kw in call.keywords:
        if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
            return kw.value.value
    return "r"  # default


def main(paths):
    overall_ok = True
    for p in paths:
        blockers, warns = scan(p)
        status = "CLEAN" if not blockers else "BLOCKED"
        if blockers:
            overall_ok = False
        print(f"\n[{status}] {p}")
        for kind, what, ln in blockers:
            print(f"   ✗ {kind}: {what}  (line {ln})")
        for kind, what, ln in warns:
            print(f"   ⚠ {kind}: {what}  (line {ln})")
        if not blockers and not warns:
            print("   (no banned imports, no dangerous constructs, 3.10-compatible)")
    print("\n" + ("=" * 60))
    print("RESULT:", "ALL CLEAN — safe to submit" if overall_ok
          else "BLOCKERS FOUND — fix before submitting")
    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
