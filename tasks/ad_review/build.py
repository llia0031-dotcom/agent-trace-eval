"""Validate the ad review tasks, draw their images and read them once with OCR.

Uses the ad review project's own code: its label checks (every span is a verbatim part of the ad,
every clause id exists in the ad's policy library), its renderer and its OCR set-up. Run it with a
Python that has that project's dependencies (pillow, rapidocr):

  python tasks/ad_review/build.py            validate, draw, OCR
  python tasks/ad_review/build.py --check    validate only (what the offline tests run)

Writes images/<id>.png (and <id>-rev.png for a revised image) and ocr.json: for every image the
text the OCR read, the seconds it took and the character error rate against the known words.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))
from bench import config  # noqa: E402

TIERS = {"L1", "L2", "L3"}


def load_items() -> list[dict]:
    items = yaml.safe_load((HERE / "items.yaml").read_text(encoding="utf-8"))
    for it in items:
        it.setdefault("labels", [])
        it["split"] = "bench"
    return items


def validate(items: list[dict]) -> list[str]:
    sys.path.insert(0, str(config.AD_REVIEW_DIR / "policies"))
    sys.path.insert(0, str(config.AD_REVIEW_DIR / "testset"))
    from load import _check_labels, ad_text  # the ad review project's checks

    problems, seen = [], set()
    for it in items:
        if it["id"] in seen:
            problems.append(f"{it['id']}: duplicate id")
        seen.add(it["id"])
        if it.get("tier") not in TIERS:
            problems.append(f"{it['id']}: tier")
        _check_labels(it, it["labels"], ad_text(it), problems, it["id"])
        if "trap" in it and (it["labels"] or not it.get("basis")):
            problems.append(f"{it['id']}: a trap has no labels and names a basis")
        if (it["tier"] == "L3") != ("revision" in it):
            problems.append(f"{it['id']}: L3 tasks, and only they, have a revision")
        if "revision" in it:
            _check_labels(it, it["revision"]["labels"], ad_text(it, revised=True), problems, it["id"] + "/rev")
            problems += [f"{it['id']}: must_keep {f} not in the ad" for f in it.get("must_keep", []) if f not in ad_text(it)]
            problems += [f"{it['id']}: must_keep {f} not in the revision" for f in it.get("must_keep", [])
                         if f not in ad_text(it, revised=True)]
    return problems


def draw_and_read(items: list[dict]) -> None:
    sys.path.insert(0, str(config.AD_REVIEW_DIR / "agent"))
    import ocr  # noqa: E402
    import render  # noqa: E402
    from load import image_words  # noqa: E402

    out_dir = HERE / "images"
    out_dir.mkdir(exist_ok=True)
    ocr.engine()
    cache = {}
    for it in items:
        if it["kind"] != "image":
            continue
        variants = [(it["id"], it["image"]["lines"], image_words(it))]
        if it.get("revision", {}).get("image_lines"):
            variants.append((it["id"] + "-rev", it["revision"]["image_lines"], image_words(it, revised=True)))
        for name, lines, words in variants:
            path = out_dir / f"{name}.png"
            render.render_item(it, lines, path)
            text, secs = ocr.read_text(path)
            cache[name] = {"text": text, "seconds": round(secs, 3), "cer": round(ocr.cer("\n".join(words), text), 4)}
            print(name, "CER", cache[name]["cer"], "|", text.replace("\n", " / "))
    (HERE / "ocr.json").write_text(json.dumps(cache, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")


def main():
    items = load_items()
    problems = validate(items)
    if problems:
        sys.exit("\n".join(problems))
    by = lambda k: {v: sum(1 for it in items if it[k] == v) for v in sorted({it[k] for it in items})}
    print(f"{len(items)} items valid:", by("lang"), by("tier"), by("kind"),
          "violating", sum(bool(it["labels"]) for it in items), "compliant", sum(not it["labels"] for it in items))
    if "--check" not in sys.argv:
        draw_and_read(items)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main()
