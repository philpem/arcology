import re
import xml.etree.ElementTree as ET
from pathlib import Path

from scour.scour import scourString

SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"

ET.register_namespace("", SVG_NS)
ET.register_namespace("xlink", XLINK_NS)


# --- Stage 1: Pre-sanitise (TEXT LEVEL, NO XML PARSING) ---

def _fix_svg_xml_text(text: str) -> str:
    """
    Make SVG minimally well-formed so XML parsers won't choke.
    This MUST NOT assume valid XML.
    """

    if "<svg" not in text:
        return text

    # Remove legacy/broken DOCTYPE (safe for SVG)
    text = re.sub(r'<!DOCTYPE[^>]*>', '', text, flags=re.IGNORECASE)

    # Ensure default SVG namespace
    if 'xmlns="' not in text:
        text = text.replace(
            "<svg",
            f'<svg xmlns="{SVG_NS}"',
            1
        )

    # Ensure xlink namespace if used
    if "xlink:" in text and "xmlns:xlink" not in text:
        text = text.replace(
            "<svg",
            f'<svg xmlns:xlink="{XLINK_NS}"',
            1
        )

    return text


# --- Stage 2: Parse + minimal structural safety ---

def _safe_parse(svg_text: str) -> ET.ElementTree:
    """
    Parse after sanitisation. If this fails, the SVG is truly broken.
    """
    return ET.ElementTree(ET.fromstring(svg_text))


def _ensure_root_namespace(root: ET.Element) -> None:
    """
    Guarantee root is in SVG namespace.
    """
    if not root.tag.startswith("{"):
        root.tag = f"{{{SVG_NS}}}svg"


# --- Stage 3: Optional light cleanup (safe, deterministic) ---

def _strip_editor_metadata(root: ET.Element) -> None:
    """
    Remove known non-standard attributes (safe for rendering).
    """
    SODIPODI = "http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd"

    for el in root.iter():
        for attr in list(el.attrib):
            if attr.startswith(f"{{{SODIPODI}}}"):
                del el.attrib[attr]


# --- Stage 4: Scour optimisation ---

def _run_scour(svg_text: str) -> str:
    """
    Run Scour with conservative, production-safe options.
    """

    # Scour expects CLI-style options object; we emulate minimal config
    class Options:
        strip_xml_prolog = False
        remove_metadata = True
        remove_descriptive_elements = False
        remove_titles = False
        remove_descriptions = False
        remove_comments = True
        shorten_ids = False
        indent_type = "none"
        indent_depth = 0
        newlines = False
        enable_viewboxing = True
        simplify_colors = True
        strip_ids = False

    return scourString(svg_text, Options())


# --- Public API ---

def postprocess_svg(svg_path: Path) -> None:
    """
    Production-grade SVG fixer + optimiser.

    Safe for:
    - WMF/EMF conversions
    - broken namespace exports
    - archival pipelines
    """

    # 1. Read raw
    raw = svg_path.read_text(encoding="utf-8", errors="replace")

    # 2. Fix XML at text level
    fixed = _fix_svg_xml_text(raw)

    # 3. Parse
    tree = _safe_parse(fixed)
    root = tree.getroot()

    # 4. Structural guarantees
    _ensure_root_namespace(root)
    _strip_editor_metadata(root)

    # 5. Serialize back to string (clean XML)
    interim = ET.tostring(root, encoding="unicode")

    # 6. Run Scour
    cleaned = _run_scour(interim)

    # 7. Write final output
    svg_path.write_text(cleaned, encoding="utf-8")
    