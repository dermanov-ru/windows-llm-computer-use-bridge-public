"""Screenshot encoding and coordinate transforms, independent of Win32."""
import base64
import io
from PIL import Image, ImageDraw


def validate_options(options):
    if not isinstance(options, dict):
        raise ValueError("Observation options must be an object")
    if options.get("scope", "active_window") not in ("active_window", "desktop"):
        raise ValueError("scope must be active_window or desktop")
    if options.get("format", "jpeg") not in ("jpeg", "png"):
        raise ValueError("format must be jpeg or png")
    width = options.get("max_width", 1280)
    if not isinstance(width, int) or isinstance(width, bool) or not (width == 0 or 128 <= width <= 8192):
        raise ValueError("max_width must be 0 (native) or 128 to 8192")
    quality = options.get("quality", 80)
    if not isinstance(quality, int) or isinstance(quality, bool) or not 30 <= quality <= 95:
        raise ValueError("quality must be 30 to 95")
    if "window_id" in options and (not isinstance(options["window_id"], int) or isinstance(options["window_id"], bool)):
        raise ValueError("window_id must be an integer")
    if "point" in options:
        point = options["point"]
        if not isinstance(point, dict) or any(not isinstance(point.get(k), int) or isinstance(point[k], bool) for k in ("x", "y")):
            raise ValueError("point must contain physical integer x and y")
    if "draw_cursor" in options and not isinstance(options["draw_cursor"], bool):
        raise ValueError("draw_cursor must be boolean")


def encode(image, origin, options, cursor=None):
    source_width, source_height = image.size
    width = options.get("max_width", 1280)
    if width and image.width > width:
        image = image.resize((width, max(1, round(image.height * width / image.width))), getattr(Image, "Resampling", Image).LANCZOS)
    else:
        image = image.copy()
    scale = [source_width / image.width, source_height / image.height]
    pointer = dict(cursor) if cursor else None
    if pointer:
        ix = (pointer["x"] - origin[0]) / scale[0]
        iy = (pointer["y"] - origin[1]) / scale[1]
        pointer["in_capture"] = 0 <= ix < image.width and 0 <= iy < image.height
        pointer["image_position"] = [ix, iy] if pointer["in_capture"] else None
        if pointer["in_capture"] and options.get("draw_cursor", True):
            draw = ImageDraw.Draw(image)
            x, y = round(ix), round(iy)
            draw.ellipse((x-6, y-6, x+6, y+6), outline="black", width=3)
            draw.ellipse((x-5, y-5, x+5, y+5), outline="#ffff00", width=2)
            draw.line((x-9, y, x+9, y), fill="#ff2200", width=1)
            draw.line((x, y-9, x, y+9), fill="#ff2200", width=1)
            # HWND remains in JSON metadata; a text badge obscures small icons.
    format_name = options.get("format", "jpeg")
    stream = io.BytesIO()
    if format_name == "jpeg":
        image.convert("RGB").save(stream, "JPEG", quality=options.get("quality", 80))
    else:
        image.save(stream, "PNG")
    return {
        "width": image.width, "height": image.height, "source_width": source_width,
        "source_height": source_height, "origin": origin, "coordinate_scale": scale,
        "mime_type": "image/jpeg" if format_name == "jpeg" else "image/png",
        "image_base64": base64.b64encode(stream.getvalue()).decode("ascii"), "cursor": pointer,
    }
