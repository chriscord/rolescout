#!/usr/bin/env python3
"""Build a compact Word resume from a validator-clean Markdown draft.

Uses only the standard library so RoleNavi does not depend on a global
python-docx install. The output uses A4 geometry, Times New Roman, real Word
bullet numbering, and bordered section headings.
"""

from __future__ import annotations

import argparse
import html
import re
import zipfile
from pathlib import Path


def x(value: str) -> str:
    return html.escape(value, quote=False)


def clean_inline(value: str) -> str:
    value = re.sub(r"\[([^]]+)\]\([^)]+\)", r"\1", value)
    value = re.sub(r"[*_`]", "", value)
    return re.sub(r"\s+", " ", value).strip()


def parse_markdown(text: str) -> list[tuple[str, str]]:
    if re.search(r"\bEV-\d+\b", text, re.I):
        raise ValueError("resume draft contains internal EV IDs")
    if re.search(r"^#{1,3}\s+(?:Evidence Gaps?|Validation|Target)\b", text,
                 re.I | re.M):
        raise ValueError("resume draft contains an audit-only section")
    blocks: list[tuple[str, str]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(">"):
            continue
        if line.startswith("# "):
            blocks.append(("name", clean_inline(line[2:])))
        elif re.match(r"^#{2,3}\s+", line):
            value = clean_inline(re.sub(r"^#{2,3}\s+", "", line))
            blocks.append(("heading" if value.lower() in {
                "work experience", "education", "skills",
            } else "body_bold", value))
        elif re.match(r"^[-*•]\s+", line):
            blocks.append(("bullet", clean_inline(re.sub(r"^[-*•]\s+", "", line))))
        elif line.startswith("|"):
            continue
        elif re.fullmatch(r"\*\*.+\*\*\s*", line):
            blocks.append(("body_bold", clean_inline(line)))
        elif re.fullmatch(r"\*[^*].*\*\s*", line):
            blocks.append(("body_italic", clean_inline(line)))
        else:
            blocks.append(("body", clean_inline(line)))
    if not blocks:
        raise ValueError("resume draft contains no renderable content")
    return blocks


def run_xml(text: str, *, bold: bool = False, italic: bool = False,
            size: int = 17) -> str:
    # WordprocessingML w:sz is measured in half-points (17 = 8.5 pt), not
    # twentieths of a point. Using 174 here renders at 87 pt and explodes a
    # one-page resume into dozens of pages in Microsoft Word.
    props = (
        '<w:rPr><w:rFonts w:ascii="Times New Roman" w:hAnsi="Times New Roman" '
        'w:eastAsia="Times New Roman"/>'
        + ('<w:b/>' if bold else '')
        + ('<w:i/>' if italic else '')
        + f'<w:sz w:val="{size}"/><w:szCs w:val="{size}"/></w:rPr>'
    )
    return f'<w:r>{props}<w:t xml:space="preserve">{x(text)}</w:t></w:r>'


def paragraph_xml(kind: str, text: str) -> str:
    if kind == "name":
        ppr = '<w:pPr><w:jc w:val="center"/><w:spacing w:after="40"/></w:pPr>'
        return f'<w:p>{ppr}{run_xml(text, bold=True, size=27)}</w:p>'
    if kind == "heading":
        ppr = (
            '<w:pPr><w:keepNext/><w:spacing w:before="70" w:after="28"/>'
            '<w:pBdr><w:bottom w:val="single" w:sz="5" w:space="2" '
            'w:color="444444"/></w:pBdr></w:pPr>'
        )
        return f'<w:p>{ppr}{run_xml(text.upper(), bold=True, size=18)}</w:p>'
    if kind == "bullet":
        ppr = (
            '<w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr>'
            '<w:spacing w:after="16" w:line="190" w:lineRule="auto"/></w:pPr>'
        )
        return f'<w:p>{ppr}{run_xml(text)}</w:p>'
    if kind in {"body_bold", "body_italic"}:
        ppr = '<w:pPr><w:spacing w:after="12" w:line="190" w:lineRule="auto"/></w:pPr>'
        return f'<w:p>{ppr}{run_xml(text, bold=kind == "body_bold", italic=kind == "body_italic")}</w:p>'
    centered = "" if kind != "body" else ""
    ppr = f'<w:pPr>{centered}<w:spacing w:after="22" w:line="190" w:lineRule="auto"/></w:pPr>'
    return f'<w:p>{ppr}{run_xml(text)}</w:p>'


def document_xml(blocks: list[tuple[str, str]]) -> str:
    paras = "".join(paragraph_xml(kind, text) for kind, text in blocks)
    section = (
        '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
        '<w:pgMar w:top="700" w:right="760" w:bottom="650" w:left="760" '
        'w:header="300" w:footer="300" w:gutter="0"/>'
        '<w:cols w:space="720"/><w:docGrid w:linePitch="312"/></w:sectPr>'
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f'<w:body>{paras}{section}</w:body></w:document>'
    )


STYLES = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Times New Roman" w:hAnsi="Times New Roman"/><w:sz w:val="17"/><w:szCs w:val="17"/></w:rPr></w:rPrDefault>
  <w:pPrDefault><w:pPr><w:spacing w:after="22" w:line="190" w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults>
  <w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/><w:qFormat/></w:style>
</w:styles>'''

NUMBERING = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:numbering xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:abstractNum w:abstractNumId="0"><w:multiLevelType w:val="singleLevel"/><w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="bullet"/><w:lvlText w:val="•"/><w:lvlJc w:val="left"/><w:pPr><w:tabs><w:tab w:val="num" w:pos="300"/></w:tabs><w:ind w:left="300" w:hanging="180"/></w:pPr><w:rPr><w:rFonts w:ascii="Times New Roman" w:hAnsi="Times New Roman"/></w:rPr></w:lvl></w:abstractNum>
  <w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>
</w:numbering>'''

CONTENT_TYPES = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
  <Override PartName="/word/numbering.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"/>
</Types>'''

ROOT_RELS = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>'''

DOC_RELS = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/numbering" Target="numbering.xml"/>
</Relationships>'''


def build(source: Path, output: Path) -> None:
    blocks = parse_markdown(source.read_text(encoding="utf-8"))
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_name(output.name + ".tmp")
    with zipfile.ZipFile(temp, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", CONTENT_TYPES)
        archive.writestr("_rels/.rels", ROOT_RELS)
        archive.writestr("word/document.xml", document_xml(blocks))
        archive.writestr("word/styles.xml", STYLES)
        archive.writestr("word/numbering.xml", NUMBERING)
        archive.writestr("word/_rels/document.xml.rels", DOC_RELS)
    temp.replace(output)
    with zipfile.ZipFile(output) as archive:
        required = {"word/document.xml", "word/styles.xml", "word/numbering.xml"}
        if not required.issubset(archive.namelist()):
            raise ValueError("generated DOCX package is incomplete")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    try:
        build(args.source, args.output)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        print(f"FAIL: {exc}")
        return 1
    print(f"PASS: built {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
