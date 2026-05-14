"""
AlphaPress Photo Tool - Autonomous Image Processor
===================================================
Fully autonomous: give it one or more PhotoShelter image IDs (or search-result
URLs) and it downloads, aligns, composites clean reconstructed images, embeds
IPTC/EXIF metadata from the source file, and saves with a meaningful filename.

Key technical findings
----------------------
  • img-get2 filename segment is purely cosmetic — any value works.
  • sec= token is NOT enforced at CDN edge — no auth needed.
  • The crop=999999x2040 render has full IPTC metadata embedded in Photoshop
    8BIM blocks (resource 0x0404). Extracted via pure Python — no libraries.
  • IPTC fields present: caption, copyright, credit, author, city, keywords,
    date created, time created, title (ObjectName), job ref, source.

Composite algorithm (confirmed spec)
-------------------------------------
  Base  : Watermarked full render  — fit=9999/image.jpg
            → whole composition, largest canvas, capped at 2040 px long edge
  Clean : Untagged cropped render  — crop=999999x2040/image.jpg
            → top-left anchored crop at 2040 px height, cleaner region
  Steps :
    1. Download clean crop; extract IPTC metadata from its embedded 8BIM block.
    2. Download watermarked full render.
    3. Scale the watermarked full image so its shortest side matches the
       shortest side of the clean crop (typically 2040 px).
    4. Paste the clean crop at (0, 0) top-left over the scaled WM image.
    5. Embed extracted metadata as EXIF tags in the output JPEG.
    6. Save using a meaningful filename (job_ref + title, or {ID}_clean.jpg).

Usage
-----
    python process.py <ID_or_URL> [ID_or_URL ...]

    python process.py I0000u1JEFwZeIoI
    python process.py I0000u1JEFwZeIoI I0000AqfSxQrUv7Q I00003xBkWmMLNbQ
    python process.py "https://alphapress.photoshelter.com/search/result/I0000u1JEFwZeIoI?terms=totp"

Run with no arguments to be prompted for IDs interactively.
Output is saved to ./output/
"""

import sys
import io
import re
import time
from pathlib import Path

# Force UTF-8 output on Windows (cp1252 codepage chokes on box-drawing chars)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ── dependency check ──────────────────────────────────────────────────────────
try:
    from PIL import Image
except ImportError:
    print("\n[ERROR] Pillow is not installed.")
    print("        Run:       pip install Pillow")
    print("        Or:        re-run launch.bat — it installs Pillow automatically.\n")
    sys.exit(1)

try:
    import piexif
    PIEXIF_OK = True
except ImportError:
    PIEXIF_OK = False

import urllib.request
import urllib.error

# ── constants ─────────────────────────────────────────────────────────────────
PHOTOSHELTER_BASE = "https://alphapress.photoshelter.com"
IMG_GET2_BASE     = f"{PHOTOSHELTER_BASE}/img-get2"
OUTPUT_DIR        = Path("output")
FILENAME          = "image.jpg"   # cosmetic — any value works with img-get2

HEADERS = {
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept":          "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
    "Referer":         "https://alphapress.photoshelter.com/",
}

# IPTC dataset numbers (record 2) → friendly field name
IPTC_DATASET = {
     5: "title",          # ObjectName / Headline
    15: "category",
    20: "supplemental_category",  # repeatable
    25: "keywords",               # repeatable
    40: "special_instructions",
    55: "date_created",   # YYYYMMDD
    60: "time_created",   # HHMMSS±HHMM
    80: "author",         # Byline
    85: "author_title",   # BylineTitle
    90: "city",
   101: "province",
   103: "job_ref",        # OriginalTransmissionReference
   110: "credit",
   115: "source",
   116: "copyright",
   120: "caption",        # Caption / Abstract
   122: "caption_writer",
}
IPTC_REPEATABLE = {25, 20}       # dataset numbers that may appear multiple times


# ── IPTC extraction ───────────────────────────────────────────────────────────
def extract_iptc(img_bytes: bytes) -> dict:
    """
    Extract IPTC metadata from JPEG bytes via the embedded Photoshop
    8BIM 0x0404 block.  Pure Python — no external libraries required.
    Returns an empty dict if no IPTC data is found.
    """
    marker = b"8BIM\x04\x04"
    idx = img_bytes.find(marker)
    if idx < 0:
        return {}

    name_len = img_bytes[idx + 6]
    padded   = name_len + 1 if name_len % 2 == 0 else name_len
    off      = idx + 6 + 1 + padded
    iptc_len = int.from_bytes(img_bytes[off:off + 4], "big")
    iptc     = img_bytes[off + 4: off + 4 + iptc_len]

    raw: dict[int, list[str]] = {}
    i = 0
    while i < len(iptc):
        if iptc[i] != 0x1C:
            i += 1
            continue
        record  = iptc[i + 1]
        dataset = iptc[i + 2]
        length  = int.from_bytes(iptc[i + 3:i + 5], "big")
        value   = iptc[i + 5:i + 5 + length]
        if record == 2:
            try:
                val_str = value.decode("utf-8")
            except UnicodeDecodeError:
                val_str = value.decode("latin-1", errors="replace")
            raw.setdefault(dataset, []).append(val_str)
        i += 5 + length

    meta: dict = {}
    for ds, values in raw.items():
        field = IPTC_DATASET.get(ds)
        if not field:
            continue
        if ds in IPTC_REPEATABLE:
            meta[field] = values          # keep as list for repeatable fields
        else:
            meta[field] = values[0]       # single value

    # Flatten keyword list to a semicolon-separated string for embedding
    if "keywords" in meta and isinstance(meta["keywords"], list):
        meta["keywords_list"] = meta["keywords"]
        meta["keywords"] = "; ".join(meta["keywords"])
    if "supplemental_category" in meta and isinstance(meta["supplemental_category"], list):
        meta["supplemental_category"] = "; ".join(meta["supplemental_category"])

    # Format date_created: YYYYMMDD → YYYY-MM-DD
    if "date_created" in meta and len(meta["date_created"]) == 8:
        d = meta["date_created"]
        meta["date_created_fmt"] = f"{d[:4]}-{d[4:6]}-{d[6:]}"

    return meta


def print_metadata(meta: dict) -> None:
    if not meta:
        print("    [WARN] No IPTC metadata found in clean crop render")
        return
    pairs = [
        ("Title",      meta.get("title")),
        ("Date",       meta.get("date_created_fmt") or meta.get("date_created")),
        ("City",       meta.get("city")),
        ("Credit",     meta.get("credit") or meta.get("author")),
        ("Copyright",  meta.get("copyright")),
        ("Keywords",   meta.get("keywords")),
        ("Job ref",    meta.get("job_ref")),
    ]
    caption = meta.get("caption", "")
    for label, value in pairs:
        if value:
            print(f"    {label:12s}: {value[:100]}")
    if caption:
        short = caption.replace("\n", " ")[:110]
        print(f"    {'Caption':12s}: {short}{'…' if len(caption) > 110 else ''}")


# ── output filename ───────────────────────────────────────────────────────────
def make_output_filename(image_id: str, meta: dict) -> str:
    """
    Derive a meaningful output filename from IPTC metadata.
    Pattern: {job_ref}_{safe_title}_clean.jpg, fallback: {ID}_clean.jpg
    """
    job_ref = meta.get("job_ref", "").strip()
    title   = meta.get("title", "").strip()

    if job_ref and title:
        safe_title = re.sub(r"[^\w\- ]", "", title).strip().replace(" ", "_")
        return f"{job_ref}_{safe_title}_clean.jpg"
    if job_ref:
        return f"{job_ref}_clean.jpg"
    return f"{image_id}_clean.jpg"


# ── URL builders ──────────────────────────────────────────────────────────────
def build_urls(image_id: str) -> dict[str, str]:
    """Return the two render URLs required for reconstruction."""
    base = f"{IMG_GET2_BASE}/{image_id}"
    return {
        "wm_full":    f"{base}/fit=9999/{FILENAME}",
        "clean_crop": f"{base}/crop=999999x2040/{FILENAME}",
    }


# ── raw image fetching ────────────────────────────────────────────────────────
def fetch_bytes(url: str, label: str, retries: int = 2) -> bytes:
    """Fetch raw bytes from url with retry logic."""
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"HTTP {e.code} — {label}") from e
        except (urllib.error.URLError, OSError) as e:
            if attempt < retries:
                print(f"    Retry {attempt + 1}/{retries} for {label} ...")
                time.sleep(1.5)
            else:
                raise RuntimeError(f"Network error — {label}: {e}") from e
    raise RuntimeError(f"Failed after {retries} retries — {label}")


def bytes_to_image(data: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(data))
    img.load()
    return img.convert("RGBA")


# ── compositing ───────────────────────────────────────────────────────────────
def composite(wm_img: Image.Image, clean_crop: Image.Image) -> Image.Image:
    """
    Scale the watermarked full image so its shortest side matches the shortest
    side of the clean crop, then paste the clean crop at (0, 0) top-left.
    """
    wm_w,  wm_h  = wm_img.size
    cr_w,  cr_h  = clean_crop.size

    crop_shortest = min(cr_w, cr_h)
    wm_shortest   = min(wm_w, wm_h)
    scale         = crop_shortest / wm_shortest
    new_wm_w      = round(wm_w * scale)
    new_wm_h      = round(wm_h * scale)

    print(f"    WM full size     : {wm_w} x {wm_h}")
    print(f"    Clean crop size  : {cr_w} x {cr_h}")
    print(f"    WM scale factor  : {scale:.4f}  →  {new_wm_w} x {new_wm_h}")

    wm_scaled = wm_img.resize((new_wm_w, new_wm_h), Image.LANCZOS)
    result = wm_scaled.copy()
    result.paste(clean_crop, (0, 0))

    print(f"    Output canvas    : {result.size[0]} x {result.size[1]}")
    return result.convert("RGB")


# ── EXIF embedding ────────────────────────────────────────────────────────────
def save_with_exif(img: Image.Image, path: Path, meta: dict) -> None:
    """Save JPEG with IPTC fields re-embedded as EXIF tags."""
    if not PIEXIF_OK:
        img.save(path, "JPEG", quality=95, subsampling=0)
        return

    def enc(s: str) -> bytes:
        return s.encode("utf-8", errors="replace")

    caption   = (meta.get("caption", "")).replace("\n", " ")
    copyright_= meta.get("copyright", "")
    author    = meta.get("credit") or meta.get("author", "")
    keywords  = meta.get("keywords", "")
    title     = meta.get("title", "")

    exif_0th: dict  = {}
    exif_exif: dict = {}

    if caption:
        exif_0th[piexif.ImageIFD.ImageDescription] = enc(caption)
        exif_exif[piexif.ExifIFD.UserComment] = b"ASCII\x00\x00\x00" + enc(caption)
    if copyright_:
        exif_0th[piexif.ImageIFD.Copyright] = enc(copyright_)
    if author:
        exif_0th[piexif.ImageIFD.Artist] = enc(author)
    if keywords:
        exif_0th[40094] = keywords.encode("utf-16-le")   # XPKeywords
    if title:
        exif_0th[40092] = title.encode("utf-16-le")       # XPComment

    try:
        exif_bytes = piexif.dump({"0th": exif_0th, "Exif": exif_exif, "GPS": {}, "1st": {}})
        img.save(path, "JPEG", quality=95, subsampling=0, exif=exif_bytes)
        embedded = [f for f in ("caption", "copyright", "author/credit", "keywords") if meta.get(f.split("/")[0]) or (f == "author/credit" and (meta.get("credit") or meta.get("author")))]
        print(f"    EXIF embedded  : {', '.join(embedded) if embedded else 'none'}")
    except Exception as e:
        print(f"    [WARN] EXIF embed failed ({e}) — saving without EXIF")
        img.save(path, "JPEG", quality=95, subsampling=0)


# ── per-image pipeline ────────────────────────────────────────────────────────
def process_id(image_id: str) -> bool:
    """Download, composite, embed metadata, and save one image. Returns True on success."""
    print(f"\n  [{image_id}]")
    urls = build_urls(image_id)

    # Step 1 — clean crop (contains IPTC metadata)
    print(f"  Downloading clean crop render ...")
    try:
        clean_bytes = fetch_bytes(urls["clean_crop"], "clean crop (crop=999999x2040)")
    except RuntimeError as e:
        print(f"  [SKIP] {e}")
        return False

    # Extract IPTC from the clean crop before converting to PIL
    print(f"  Extracting IPTC metadata ...")
    meta = extract_iptc(clean_bytes)
    print_metadata(meta)

    clean_img = bytes_to_image(clean_bytes)

    # Step 2 — watermarked full render (base layer)
    print(f"  Downloading watermarked render ...")
    try:
        wm_bytes = fetch_bytes(urls["wm_full"], "watermarked full (fit=9999)")
    except RuntimeError as e:
        print(f"  [SKIP] {e}")
        return False

    wm_img = bytes_to_image(wm_bytes)

    # Step 3 — composite
    print(f"  Compositing ...")
    result = composite(wm_img, clean_img)

    # Step 4 — save with EXIF
    OUTPUT_DIR.mkdir(exist_ok=True)
    out_name = make_output_filename(image_id, meta)
    out_path = OUTPUT_DIR / out_name

    if not PIEXIF_OK:
        print(f"    [INFO] piexif not installed — saving without EXIF.")
        print(f"           Install with:  pip install piexif")

    save_with_exif(result, out_path, meta)
    print(f"  Saved  →  {out_path}")
    return True


# ── ID extraction ─────────────────────────────────────────────────────────────
def extract_id(raw: str) -> str | None:
    raw = raw.strip().rstrip("/")
    if "photoshelter.com" in raw or "img-get2" in raw:
        m = re.search(r"(I[0-9A-Za-z]{14,})", raw)
        return m.group(1) if m else None
    m = re.match(r"^(I[0-9A-Za-z]{14,})$", raw)
    return m.group(1) if m else None


# ── entry point ───────────────────────────────────────────────────────────────
def main() -> None:
    raw_args = [a for a in sys.argv[1:] if not a.startswith("-")]

    if not raw_args:
        print(__doc__)
        user_input = input(
            "Enter image ID(s) or search-result URL(s),\n"
            "separated by spaces or one per line:\n> "
        ).strip()
        raw_args = re.split(r"[\s,]+", user_input)

    seen: set[str] = set()
    ids:  list[str] = []
    for raw in raw_args:
        if not raw:
            continue
        eid = extract_id(raw)
        if eid and eid not in seen:
            seen.add(eid)
            ids.append(eid)
        elif not eid:
            print(f"  [WARN] Cannot parse image ID from: {raw!r} — skipping")

    if not ids:
        print("\n[ERROR] No valid image IDs found. Exiting.")
        sys.exit(1)

    OUTPUT_DIR.mkdir(exist_ok=True)
    print(f"\nProcessing {len(ids)} image(s)  →  output in ./{OUTPUT_DIR}/\n")
    if not PIEXIF_OK:
        print("[INFO] piexif not installed — EXIF metadata will not be embedded.")
        print("       Install with:  pip install piexif\n")
    print("-" * 56)

    ok = sum(process_id(image_id) for image_id in ids)
    failed = len(ids) - ok

    print("\n" + "-" * 56)
    print(f"Done.  {ok} succeeded  |  {failed} failed")
    print(f"Output folder: {OUTPUT_DIR.resolve()}")

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
