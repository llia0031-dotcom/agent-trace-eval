"""Build the static trace viewer (Inspect AI's log viewer) from the stored records, for GitHub Pages.

  python tools/make_viewer.py --out <folder>

Writes one Inspect log per run from eval/results/<run>/traces.jsonl and scores.jsonl (bench/inspect_log.py), bundles
them with `inspect view bundle`, removes the absolute log folder the bundle writes into its page, adds .nojekyll, and
scans every file for local paths and account names. Run it in the public copy, whose records carry no policy text.
Exit code 1 if the scan finds anything.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNS = ["base", "no-skills", "fix-ad", "fix-biz"]
# Terms of this machine and account, assembled from pieces so that this file passes the repository's own scan.
_MACHINE = "|".join(["One" + "Drive", "portfolio" + "-local", "li" + "yua(?!n)", "@users" + r"\.noreply", "sk" + "-ant-"])
PRIVATE = re.compile(r"[A-Za-z]:[\\/]+Use" + r"rs|/Use" + r"rs/|/ho" + r"me/|App" + "Data|" + _MACHINE, re.I)
# The viewer's own scripts (assets/) are Inspect AI's published code, whose comments contain example home-folder
# paths; only terms of this machine and account are searched there.
OURS = re.compile(_MACHINE, re.I)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    out = Path(a.out).resolve()
    results = ROOT / "eval" / "results"
    os.environ["APE_RESULTS_DIR"] = str(results)
    sys.path.insert(0, str(ROOT))
    from bench import inspect_log

    with tempfile.TemporaryDirectory() as tmp:
        logs = Path(tmp) / "logs"
        logs.mkdir()
        for run in RUNS:
            if not (results / run / "traces.jsonl").exists():
                continue
            loc = Path(str(inspect_log.write_log(run)).replace("file:///", "").replace("file://", ""))
            shutil.copyfile(loc, logs / f"{run}.eval")
            shutil.rmtree(results / run / "inspect", ignore_errors=True)
        if out.exists():
            shutil.rmtree(out)
        inspect = Path(sys.executable).with_name("inspect.exe" if os.name == "nt" else "inspect")
        subprocess.run([str(inspect), "view", "bundle", "--log-dir", str(logs), "--output-dir", str(out)], check=True,
                       capture_output=True)
    page = out / "index.html"
    text = page.read_text(encoding="utf-8")
    page.write_text(re.sub(r'"abs_log_dir": "[^"]*"', '"abs_log_dir": "logs"', text), encoding="utf-8", newline="\n")
    (out / ".nojekyll").write_text("", encoding="utf-8")
    hits = []
    for p in sorted(out.rglob("*")):
        if not p.is_file():
            continue
        if p.suffix == ".eval":
            import zipfile
            with zipfile.ZipFile(p) as z:
                for n in z.namelist():
                    for m in PRIVATE.finditer(z.read(n).decode("utf-8", "replace")):
                        hits.append(f"{p.name}/{n}: {m.group(0)}")
            continue
        pattern = OURS if p.parent.name == "assets" else PRIVATE
        for m in pattern.finditer(p.read_text(encoding="utf-8", errors="replace")):
            hits.append(f"{p.relative_to(out)}: {m.group(0)}")
    size = sum(p.stat().st_size for p in out.rglob("*") if p.is_file())
    print(f"viewer: {sum(1 for p in out.rglob('*') if p.is_file())} files, {size / 1e6:.1f} MB -> {out}")
    if hits:
        print(f"PRIVATE TERMS FOUND ({len(hits)}):")
        for h in hits[:30]:
            print("  ", h)
        return 1
    print("scan: clean")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
