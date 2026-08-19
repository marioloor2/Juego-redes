from __future__ import annotations

import argparse
import collections
import json
from pathlib import Path

import pdfplumber


def normalize_color(value):
    if isinstance(value, (list, tuple)):
        return tuple(round(float(component), 6) for component in value)
    return value


def rounded(value):
    return round(float(value), 4)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--details", type=Path)
    args = parser.parse_args()

    with pdfplumber.open(args.pdf) as document:
        page = document.pages[0]
        object_counts = {name: len(items) for name, items in page.objects.items()}
        line_colors = collections.Counter(
            normalize_color(item.get("stroking_color")) for item in page.lines
        )
        curve_colors = collections.Counter(
            normalize_color(item.get("stroking_color")) for item in page.curves
        )
        char_colors = collections.Counter(
            normalize_color(item.get("non_stroking_color")) for item in page.chars
        )
        print(
            json.dumps(
                {
                    "pages": len(document.pages),
                    "width": page.width,
                    "height": page.height,
                    "rotation": page.rotation,
                    "objects": object_counts,
                    "line_colors": line_colors.most_common(),
                    "curve_colors": curve_colors.most_common(),
                    "char_colors": char_colors.most_common(),
                    "text": page.extract_text(x_tolerance=1, y_tolerance=1),
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )

        if args.details:
            payload = {
                "page": {
                    "width": page.width,
                    "height": page.height,
                    "rotation": page.rotation,
                },
                "lines": [
                    {
                        "x0": rounded(item["x0"]),
                        "y0": rounded(item["y0"]),
                        "x1": rounded(item["x1"]),
                        "y1": rounded(item["y1"]),
                        "linewidth": rounded(item.get("linewidth", 0)),
                        "stroking_color": normalize_color(item.get("stroking_color")),
                        "dash": item.get("dash"),
                    }
                    for item in page.lines
                ],
                "curves": [
                    {
                        "x0": rounded(item["x0"]),
                        "y0": rounded(item["y0"]),
                        "x1": rounded(item["x1"]),
                        "y1": rounded(item["y1"]),
                        "linewidth": rounded(item.get("linewidth", 0)),
                        "stroking_color": normalize_color(item.get("stroking_color")),
                        "fill": item.get("fill"),
                        "pts": [[rounded(x), rounded(y)] for x, y in item.get("pts", [])],
                        "path": item.get("path"),
                    }
                    for item in page.curves
                ],
                "words": page.extract_words(
                    x_tolerance=1,
                    y_tolerance=1,
                    keep_blank_chars=False,
                    extra_attrs=["fontname", "size", "non_stroking_color"],
                ),
            }
            args.details.parent.mkdir(parents=True, exist_ok=True)
            args.details.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )


if __name__ == "__main__":
    main()
