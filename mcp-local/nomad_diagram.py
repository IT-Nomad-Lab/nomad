"""NOMAD diagram/chart renderer (MCP) — render graphs from a TEXT spec, no image model.

Draw diagrams (Mermaid / Graphviz / D2 / PlantUML) and data charts (Vega-Lite) from text via the
local Kroki service, and save them into the project you're working in. A layout engine positions
everything — so arrows are routed and text never overflows, unlike hand-placed matplotlib diagrams
or a diffusion model (which can't render correct text/edges). Deterministic + free + reversible.

Tool:
  render_diagram(source, diagram_type, output_format, subdir, filename) → saves <project>/<subdir>/…
"""
import os
import time
import uuid

import requests
from mcp.server.fastmcp import FastMCP

# Kroki (host-published by compose). Probe loopback first, then the Windows-host gateway.
_CANDIDATES = [c for c in (
    os.environ.get("NOMAD_KROKI_URL"),
    "http://127.0.0.1:8231",
    "http://host.docker.internal:8231",
) if c]
_base = None

# Kroki renders these to PNG too; others (notably mermaid) are SVG-only there.
_PNG_OK = {"graphviz", "vegalite", "vega", "d2", "plantuml", "blockdiag", "bytefield", "nomnoml"}


def _kroki():
    global _base
    if _base:
        return _base
    for b in _CANDIDATES:
        try:
            requests.get(b.rstrip("/") + "/health", timeout=2)
            _base = b.rstrip("/")
            return _base
        except Exception:
            continue
    _base = _CANDIDATES[0].rstrip("/")
    return _base


mcp = FastMCP("nomad-diagram")


@mcp.tool()
def render_diagram(source: str, diagram_type: str = "mermaid", output_format: str = "svg",
                   subdir: str = "assets/diagrams", filename: str = "") -> str:
    """Render a diagram or data chart from a TEXT spec and save it into the CURRENT project.

    diagram_type: 'mermaid' | 'graphviz' | 'd2' | 'plantuml' (box/arrow diagrams) | 'vegalite'
      (data charts: bar/line/scatter…), plus other Kroki types. `source` is that language's text
      (a Mermaid flowchart, Graphviz DOT, or a Vega-Lite JSON spec).
    output_format: 'svg' (default — crisp, scalable, best for text + arrows) or 'png' (raster; not
      supported for mermaid, which falls back to svg).
    subdir: destination inside the repo (default 'assets/diagrams'; Unity e.g. 'Assets/Art/Diagrams').
    filename: optional (auto-named).

    A layout engine positions nodes/edges, so arrows are routed and labels never overflow — the fix
    for matplotlib diagrams. Returns the saved path (relative to the project)."""
    dt = (diagram_type or "mermaid").strip().lower()
    fmt = (output_format or "svg").strip().lower()
    if fmt not in ("svg", "png"):
        fmt = "svg"
    note = ""
    if fmt == "png" and dt not in _PNG_OK:
        fmt, note = "svg", " (png unsupported for this type → saved svg)"
    try:
        r = requests.post(_kroki() + "/",
                          json={"diagram_source": source, "diagram_type": dt, "output_format": fmt},
                          timeout=60)
    except Exception as e:
        return f"[render error: kroki unreachable — {str(e)[:200]}]"
    if r.status_code != 200:
        return f"[render failed ({dt}/{fmt}, HTTP {r.status_code}): {r.text[:300]}]"
    name = (filename or "").strip() or f"diagram_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    if not name.lower().endswith("." + fmt):
        name = f"{name}.{fmt}"
    dest_dir = os.path.join(os.getcwd(), subdir.strip("/"))
    os.makedirs(dest_dir, exist_ok=True)
    path = os.path.join(dest_dir, name)
    with open(path, "wb") as f:
        f.write(r.content)
    return f"Rendered {dt} → {os.path.relpath(path, os.getcwd())} ({len(r.content)} bytes, {fmt}){note}"


if __name__ == "__main__":
    mcp.run()
