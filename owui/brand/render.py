"""Render the Open WebUI brand files from nomad-owui-logo.svg (#1218).

    uv run --no-project --with cairosvg --with pillow python owui/brand/render.py

docker-compose.yml mounts each output over /app/build/static/<name> (and favicon.png over
/app/build/favicon.png). Icons that the OS masks or rounds itself (apple-touch, PWA maskable)
get a full-bleed square; the icon stays inside the 80% safe zone. Tab icons (favicon.ico, 96 px)
draw the icon at 75% with a bolder stroke, so it still reads at 16 px.
"""
import io
import os

import cairosvg
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
SVG = open(os.path.join(HERE, "nomad-owui-logo.svg")).read()
SQUARE = SVG.replace('rx="128" ', "")                       # full bleed, for masked icons
SMALL = (SVG.replace("translate(128 128) scale(10.6667)", "translate(64 64) scale(16)")
         .replace('stroke-width="2"', 'stroke-width="2.4"'))  # tab icons: bigger, bolder
OUT = os.path.join(HERE, "static")

FILES = {                                                   # name -> (svg, size)
    "favicon.png": (SVG, 512), "favicon-96x96.png": (SMALL, 96), "logo.png": (SVG, 500),
    "splash.png": (SVG, 500), "splash-dark.png": (SVG, 500),
    "apple-touch-icon.png": (SQUARE, 180),
    "web-app-manifest-192x192.png": (SQUARE, 192), "web-app-manifest-512x512.png": (SQUARE, 512),
}


def png(svg: str, size: int) -> bytes:
    return cairosvg.svg2png(bytestring=svg.encode(), output_width=size, output_height=size)


def main():
    os.makedirs(OUT, exist_ok=True)
    for name, (svg, size) in FILES.items():
        open(os.path.join(OUT, name), "wb").write(png(svg, size))
    Image.open(io.BytesIO(png(SMALL, 256))).save(os.path.join(OUT, "favicon.ico"),
                                                sizes=[(16, 16), (32, 32), (48, 48)])
    open(os.path.join(OUT, "favicon.svg"), "w").write(SVG)
    print("\n".join(sorted(os.listdir(OUT))))


if __name__ == "__main__":
    main()
