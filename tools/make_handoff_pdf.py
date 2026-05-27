from __future__ import annotations

import datetime as _dt
import re
from pathlib import Path


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _wrap_lines(text: str, max_chars: int) -> list[str]:
    """
    Простая переноска по словам для моноширинной оценки.
    Для PDF (Helvetica) это приближение, но для хенд-оффа достаточно.
    """
    out: list[str] = []
    for raw in text.splitlines():
        line = raw.rstrip("\n")
        if not line.strip():
            out.append("")
            continue
        cur = line
        while len(cur) > max_chars:
            cut = cur.rfind(" ", 0, max_chars + 1)
            if cut <= 0:
                cut = max_chars
            out.append(cur[:cut].rstrip())
            cur = cur[cut:].lstrip()
        out.append(cur)
    return out


def _escape_pdf_text(s: str) -> str:
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _make_simple_pdf(lines: list[str], out_path: Path) -> None:
    """
    Минимальный PDF с одной или несколькими страницами.
    Шрифт: Helvetica 10pt.
    """
    page_w, page_h = 595.28, 841.89  # A4 pt
    left, top = 40, 50
    line_h = 12
    max_lines_per_page = int((page_h - top * 2) // line_h)

    pages: list[list[str]] = []
    for i in range(0, len(lines), max_lines_per_page):
        pages.append(lines[i : i + max_lines_per_page])

    objects: list[bytes] = []

    def add_obj(payload: bytes) -> int:
        objects.append(payload)
        return len(objects)

    # 1: Catalog, 2: Pages, далее Page + Contents
    kids_refs: list[int] = []

    # Font object
    font_obj = add_obj(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    # Contents and pages
    page_objs: list[int] = []
    content_objs: list[int] = []

    for page_lines in pages:
        y = page_h - top
        stream_lines: list[str] = [
            "BT",
            "/F1 10 Tf",
            f"{left} {y} Td",
        ]
        first = True
        for ln in page_lines:
            if not first:
                stream_lines.append(f"0 -{line_h} Td")
            first = False
            stream_lines.append(f"({_escape_pdf_text(ln)}) Tj")
        stream_lines.append("ET")
        stream = "\n".join(stream_lines).encode("utf-8")
        contents = b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream)
        content_ref = add_obj(contents)
        content_objs.append(content_ref)

        page_dict = (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %d %d] "
            b"/Resources << /Font << /F1 %d 0 R >> >> "
            b"/Contents %d 0 R >>"
            % (int(page_w), int(page_h), font_obj, content_ref)
        )
        page_ref = add_obj(page_dict)
        page_objs.append(page_ref)
        kids_refs.append(page_ref)

    # Pages object (id=2) will refer to kids. We must insert in correct order.
    # We'll build catalog/pages after we know kids.
    # But object indices are already assigned: catalog/pages not yet added.

    kids_str = " ".join(f"{k} 0 R" for k in kids_refs).encode("ascii")
    pages_obj = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids_str, len(kids_refs))
    # Insert Pages object as #2 by shifting: easiest is to rebuild objects list in order.
    # Current: [font, content..., page...]. We'll rebuild:
    orig = objects[:]
    objects = []

    def add_fixed(payload: bytes) -> int:
        objects.append(payload)
        return len(objects)

    # 1 Catalog placeholder later, 2 Pages, 3 Font, then contents/pages in same order.
    add_fixed(b"")  # catalog placeholder
    add_fixed(pages_obj)
    add_fixed(orig[0])  # font
    for payload in orig[1:]:
        add_fixed(payload)

    catalog_obj = b"<< /Type /Catalog /Pages 2 0 R >>"
    objects[0] = catalog_obj

    # Write file with xref
    header = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"
    out = bytearray(header)
    xref_offsets = [0]
    for i, obj in enumerate(objects, start=1):
        xref_offsets.append(len(out))
        out.extend(f"{i} 0 obj\n".encode("ascii"))
        out.extend(obj)
        out.extend(b"\nendobj\n")

    xref_pos = len(out)
    out.extend(f"xref\n0 {len(objects)+1}\n".encode("ascii"))
    out.extend(b"0000000000 65535 f \n")
    for off in xref_offsets[1:]:
        out.extend(f"{off:010d} 00000 n \n".encode("ascii"))
    out.extend(
        b"trailer\n"
        + f"<< /Size {len(objects)+1} /Root 1 0 R >>\n".encode("ascii")
        + b"startxref\n"
        + f"{xref_pos}\n".encode("ascii")
        + b"%%EOF\n"
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(out)


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    md_path = root / "HANDOFF.md"
    pdf_path = root / "HANDOFF.pdf"
    md = _read_text(md_path)

    # Уберём маркдауны, чтобы читалось в PDF
    cleaned = md
    cleaned = re.sub(r"^#{1,6}\s+", "", cleaned, flags=re.M)
    cleaned = cleaned.replace("**", "")
    cleaned = cleaned.replace("`", "")
    cleaned = cleaned.replace("---", "—" * 40)
    cleaned = cleaned.replace("```env", "")
    cleaned = cleaned.replace("```", "")

    stamp = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    cleaned = f"Handoff PDF generated: {stamp}\n\n{cleaned}"

    lines = _wrap_lines(cleaned, max_chars=95)
    _make_simple_pdf(lines, pdf_path)


if __name__ == "__main__":
    main()

