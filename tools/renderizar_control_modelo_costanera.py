from __future__ import annotations

import json
import re
from pathlib import Path

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "tmp" / "pdfs" / "costanera_18_agosto" / "modelo_generado_qa.png"


def main() -> None:
    source = (ROOT / "costanera-acacias-aall.js").read_text(encoding="utf-8")
    project = json.loads(
        re.search(r"const project = (\{.*?\n\});\n  if", source, re.S).group(1)
    )
    network = project["snapshot"]["network"]
    nodes = {node["id"]: node for node in network["nodes"]}
    view = network["viewBox"]
    size = 1800
    margin = 45
    scale = min(
        (size - margin * 2) / view["width"],
        (size - margin * 2) / view["height"],
    )

    def point(item):
        return (
            margin + (item["x"] - view["x"]) * scale,
            margin + (item["y"] - view["y"]) * scale,
        )

    image = Image.new("RGB", (size, size), "white")
    draw = ImageDraw.Draw(image)
    site = network["siteGeometry"]
    draw.line(
        [point(item) for item in site["boundary"]["points"]] + [point(site["boundary"]["points"][0])],
        fill="#22c55e",
        width=4,
    )
    for area in site["areas"]:
        draw.polygon([point(item) for item in area["points"]], fill="#dcfce7", outline="#16a34a", width=4)
    for line in network["lines"]:
        draw.line(
            [point(nodes[line["from"]]), point(nodes[line["to"]])],
            fill="#2563eb" if line["elementType"] == "collector" else "#ef4444",
            width=4 if line["elementType"] == "collector" else 2,
        )
    colors = {"chamber": "#e000d9", "sump": "#65d61e", "discharge": "#111827"}
    radii = {"chamber": 5, "sump": 3, "discharge": 4}
    for node in nodes.values():
        x, y = point(node)
        radius = radii[node["elementType"]]
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=colors[node["elementType"]])
    for control in site["controlPoints"]:
        x, y = point(control)
        draw.ellipse((x - 7, y - 7, x + 7, y + 7), outline="#14532d", width=2)
        draw.text((x + 8, y - 8), control["id"], fill="#14532d")
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    image.save(OUTPUT)
    print(OUTPUT)


if __name__ == "__main__":
    main()
