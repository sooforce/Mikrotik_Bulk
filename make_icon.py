"""Convert assets/logo.png to assets/logo.ico for PyInstaller."""
import sys
try:
    from PIL import Image
except ImportError:
    print("Pillow not installed – skipping icon conversion.")
    sys.exit(1)

src = "assets/logo.png"
dst = "assets/logo.ico"

try:
    img = Image.open(src).convert("RGBA")
    img.save(dst, format="ICO",
             sizes=[(16, 16), (32, 32), (48, 48),
                    (64, 64), (128, 128), (256, 256)])
    print(f"  Icon saved: {dst}")
except FileNotFoundError:
    print(f"  WARNING: {src} not found – skipping icon conversion.")
    sys.exit(1)
except Exception as exc:
    print(f"  ERROR during icon conversion: {exc}")
    sys.exit(1)
