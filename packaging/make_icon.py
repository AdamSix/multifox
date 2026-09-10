#!/usr/bin/env python3
"""Generate packaging/multifox.icns — two foxes on an orange rounded square.

Run with the project venv (needs pyobjc, already pulled in by pywebview):
  .venv/bin/python packaging/make_icon.py
"""

import subprocess
from pathlib import Path

import AppKit

ROOT = Path(__file__).resolve().parent.parent
ICONSET = ROOT / "packaging" / "multifox.iconset"
OUT = ROOT / "packaging" / "multifox.icns"


def draw_fox(px, cx, cy, size):
    font = AppKit.NSFont.fontWithName_size_("AppleColorEmoji", size)
    attrs = {AppKit.NSFontAttributeName: font}
    text = AppKit.NSAttributedString.alloc().initWithString_attributes_("🦊", attrs)
    bounds = text.size()
    text.drawAtPoint_((cx - bounds.width / 2, cy - bounds.height / 2))


def render(path, px):
    image = AppKit.NSImage.alloc().initWithSize_((px, px))
    image.lockFocus()

    inset = px * 0.03
    rect = AppKit.NSMakeRect(inset, inset, px - 2 * inset, px - 2 * inset)
    clip = AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
        rect, px * 0.23, px * 0.23
    )
    gradient = AppKit.NSGradient.alloc().initWithStartingColor_endingColor_(
        AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(0.99, 0.62, 0.20, 1.0),
        AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(0.87, 0.38, 0.06, 1.0),
    )
    gradient.drawInBezierPath_angle_(clip, -90)

    draw_fox(px, px * 0.44, px * 0.50, px * 0.60)  # big fox, center-left
    draw_fox(px, px * 0.74, px * 0.28, px * 0.34)  # kit, lower right

    image.unlockFocus()
    rep = AppKit.NSBitmapImageRep.alloc().initWithData_(image.TIFFRepresentation())
    png = rep.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, None)
    png.writeToFile_atomically_(str(path), True)


def render_social(path, width=1280, height=640):
    """GitHub social preview: full-bleed gradient, foxes left, wordmark right."""
    image = AppKit.NSImage.alloc().initWithSize_((width, height))
    image.lockFocus()

    rect = AppKit.NSMakeRect(0, 0, width, height)
    gradient = AppKit.NSGradient.alloc().initWithStartingColor_endingColor_(
        AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(0.99, 0.62, 0.20, 1.0),
        AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(0.87, 0.38, 0.06, 1.0),
    )
    gradient.drawInRect_angle_(rect, -90)

    draw_fox(height, height * 0.40, height * 0.52, height * 0.62)  # big fox
    draw_fox(height, height * 0.80, height * 0.28, height * 0.36)  # kit

    font = AppKit.NSFont.boldSystemFontOfSize_(height * 0.22)
    attrs = {
        AppKit.NSFontAttributeName: font,
        AppKit.NSForegroundColorAttributeName: AppKit.NSColor.whiteColor(),
    }
    wordmark = AppKit.NSAttributedString.alloc().initWithString_attributes_("multifox", attrs)
    bounds = wordmark.size()
    wordmark.drawAtPoint_((height * 1.05, (height - bounds.height) / 2))

    image.unlockFocus()
    rep = AppKit.NSBitmapImageRep.alloc().initWithData_(image.TIFFRepresentation())
    png = rep.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, None)
    png.writeToFile_atomically_(str(path), True)


def main():
    ICONSET.mkdir(exist_ok=True)
    for base in (16, 32, 128, 256, 512):
        render(ICONSET / f"icon_{base}x{base}.png", base)
        render(ICONSET / f"icon_{base}x{base}@2x.png", base * 2)
    subprocess.run(["iconutil", "-c", "icns", str(ICONSET), "-o", str(OUT)], check=True)
    print(f"wrote {OUT}")
    social = ROOT / "packaging" / "social-preview.png"
    render_social(social)
    print(f"wrote {social}")


if __name__ == "__main__":
    main()
