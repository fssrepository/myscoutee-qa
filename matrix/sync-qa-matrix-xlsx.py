#!/usr/bin/env python3
"""Synchronize the reviewable QA workbook from its canonical CSV source."""

from __future__ import annotations

import argparse
import csv
import os
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET


CSV_PATH = None
XLSX_PATH = None
REPOSITORY_ROOT = None

SHEET_XML = "xl/worksheets/sheet1.xml"
SHARED_STRINGS_XML = "xl/sharedStrings.xml"
STYLES_XML = "xl/styles.xml"
WORKBOOK_XML = "xl/workbook.xml"
NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"

ROW_FILL_COLORS = {
    "pass": "FF92D050",
    "partial": "FFFFC000",
    "fail": "FFFF6666",
    "not-run": "FFB4C6E7",
    "legend-x": "FFBFBFBF",
    "legend-delta": "FF5B9BD5",
    "legend-indirect": "FFB4A7D6",
    "legend-gap": "FFFF7C80",
    "progress-complete": "FF92D050",
    "progress-high": "FFC6E0B4",
    "progress-medium": "FFFFC000",
    "progress-low": "FFF4B183",
    "progress-zero": "FFB4C6E7",
}
HEADER_FILL_COLOR = "FF1F4E78"
LEGEND_SECTION_FILL_COLOR = "FF44546A"
TEST_SECTION_FILL_COLOR = "FF4472C4"
EFFECT_MARKERS_TITLE = "EFFECT / SCOPE MARKERS"
QA_TEST_CASES_TITLE = "QA TEST CASES"
DOMAIN_PRIORITY_TITLE = "DOMAIN PRIORITY"
ENVIRONMENTS_TITLE = "ENVIRONMENTS"
DOMAIN_COLUMN = "QA domain"
DEV_COLUMN = "Dev"
LOCAL_COLUMN = "Local"
COMPLEXITY_COLUMN = "Remaining QA complexity (min/max)"
DOMAIN_PRIORITY_BLOCK_WIDTH = 5
DOMAIN_PRIORITY_BLOCK_STARTS = (0, 5)
DOMAIN_PRIORITY_VALUE_OFFSETS = (0, 2, 3)
COMPACT_VISIBLE_COLUMN_WIDTHS = (
    14.0, 16.0, 12.0, 12.0, 14.0,
    12.0, 14.0, 9.0, 12.0, 14.0,
    12.0, 4.0, 4.0, 10.0, 12.0,
    12.0, 8.0, 16.0, 10.0, 12.0,
)

# This is the execution order of whole QA domains. The canonical CSV carries
# the normalized domain on every testcase row; the workbook shows the mapping
# in a separate priority table and uses it to keep domains contiguous.
DOMAIN_METADATA = (
    ("API test", 15, "FFD9EAF7", "API"),
    ("QA infrastructure", 14, "FFE7E6E6", "CLEANUP"),
    ("Identity & access", 13, "FFE4DFEC", "ENTRY"),
    ("Event", 12, "FFFCE4D6", "EVENT"),
    ("Tournament", 11, "FFF4CCCC", "TOURNAMENT"),
    ("Chat", 10, "FFDDEBF7", "CHAT"),
    ("Asset", 9, "FFFFE5CC", "ASSET"),
    ("Group", 8, "FFEAF7BE", "GROUP"),
    ("Content", 7, "FFFFF2CC", "ARTICLE"),
    ("Notifications", 6, "FFE2F0D9", "BELL"),
    ("Administration", 5, "FFEAD1DC", "ADMIN"),
    ("General UI", 4, "FFD9EAD3", "GUIDE"),
    ("Operator", 3, "FF7FC8C8", "OPERATOR"),
)
DOMAIN_PRIORITY = {domain: priority for domain, priority, _, _ in DOMAIN_METADATA}
DOMAIN_STYLE_KEY = {
    domain: f"domain-{priority}"
    for domain, priority, _, _ in DOMAIN_METADATA
}
ROW_FILL_COLORS.update({
    DOMAIN_STYLE_KEY[domain]: color
    for domain, _, color, _ in DOMAIN_METADATA
})

# Exact exceptions cover consolidated journeys whose stable ID prefix does not
# identify the owning capability. Local Admin work is distributed to its real
# owner. Deployment qualification is not Pair QA. Operator remains a normal
# manual-QA domain and uses the same domain-priority table as every other row.
ET.register_namespace("", NS)
ET.register_namespace("r", "http://schemas.openxmlformats.org/officeDocument/2006/relationships")
ET.register_namespace("xdr", "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing")
ET.register_namespace("x14", "http://schemas.microsoft.com/office/spreadsheetml/2009/9/main")
ET.register_namespace("mc", "http://schemas.openxmlformats.org/markup-compatibility/2006")


def cell_column(index: int) -> str:
    value = index + 1
    result = ""
    while value:
        value, remainder = divmod(value - 1, 26)
        result = chr(ord("A") + remainder) + result
    return result


def order_source_rows(source_rows: list[list[str]]) -> list[list[str]]:
    headers = source_rows[0]
    legend_rows = [row for row in source_rows[1:] if row[0].startswith("LEGEND-")]
    test_rows = [row for row in source_rows[1:] if not row[0].startswith("LEGEND-")]
    ordered_test_rows = [
        row
        for source_index, row in sorted(
            enumerate(test_rows),
            key=lambda indexed: domain_sort_key(indexed[1], headers, indexed[0]),
        )
    ]
    return [headers, *legend_rows, *ordered_test_rows]


def write_source_rows(source_rows: list[list[str]]) -> None:
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{CSV_PATH.stem}.",
        suffix=".csv",
        dir=CSV_PATH.parent,
    )
    os.close(file_descriptor)
    temporary_path = Path(temporary_name)
    try:
        with temporary_path.open("w", encoding="utf-8", newline="") as target:
            writer = csv.writer(target, quoting=csv.QUOTE_ALL, lineterminator="\n")
            writer.writerows(source_rows)
        os.replace(temporary_path, CSV_PATH)
    finally:
        temporary_path.unlink(missing_ok=True)


def domain_sort_key(row: list[str], headers: list[str], source_index: int) -> tuple[int, int]:
    item = dict(zip(headers, row))
    domain = item.get(DOMAIN_COLUMN, "").strip()
    identifier = item.get("ID", "").strip()
    if domain not in DOMAIN_PRIORITY:
        raise RuntimeError(f"QA domain is not configured: {domain!r}.")
    return -DOMAIN_PRIORITY[domain], source_index


def load_rows() -> list[list[str]]:
    with CSV_PATH.open("r", encoding="utf-8", newline="") as source:
        rows = list(csv.reader(source))
    if not rows:
        raise RuntimeError("QA matrix CSV is empty.")
    column_count = len(rows[0])
    if column_count == 0 or any(len(row) != column_count for row in rows):
        raise RuntimeError("QA matrix CSV has inconsistent column counts.")
    if DOMAIN_COLUMN not in rows[0]:
        raise RuntimeError(f"QA matrix CSV is missing the {DOMAIN_COLUMN!r} column.")
    for environment_column in (DEV_COLUMN, LOCAL_COLUMN):
        if environment_column not in rows[0]:
            raise RuntimeError(
                f"QA matrix CSV is missing the {environment_column!r} column."
            )
    if COMPLEXITY_COLUMN not in rows[0]:
        raise RuntimeError(f"QA matrix CSV is missing the {COMPLEXITY_COLUMN!r} column.")

    headers = rows[0]
    complexity_index = headers.index(COMPLEXITY_COLUMN)
    for row in rows[1:]:
        item = dict(zip(headers, row))
        identifier = item.get("ID", "").strip()
        complexity = row[complexity_index].strip()
        if identifier.startswith("LEGEND-"):
            if complexity:
                raise RuntimeError(
                    f"Legend row {identifier} must not carry remaining QA complexity."
                )
            continue
        status = qa_status_bucket(item)
        if status == "done":
            if complexity:
                raise RuntimeError(
                    f"Completed testcase {identifier} must have blank remaining QA complexity."
                )
            continue
        try:
            minimum, maximum = parse_complexity(complexity)
        except ValueError as error:
            raise RuntimeError(
                f"Open testcase {identifier} must have a positive five-minute "
                f"MIN/MAX {COMPLEXITY_COLUMN} value, got {complexity!r}."
            ) from error
        if minimum > maximum:
            raise RuntimeError(
                f"Open testcase {identifier} has minimum complexity greater than "
                f"maximum: {complexity!r}."
            )
    return rows


def parse_complexity(value: str) -> tuple[int, int]:
    parts = value.split("/")
    if len(parts) != 2 or any(not part.isdigit() for part in parts):
        raise ValueError("complexity must use MIN/MAX numeric form")
    minimum, maximum = (int(part) for part in parts)
    if minimum <= 0 or maximum <= 0 or minimum % 5 or maximum % 5:
        raise ValueError("complexity bounds must be positive five-minute values")
    return minimum, maximum


def workbook_rows(source_rows: list[list[str]]) -> list[list[str]]:
    headers = source_rows[0]
    legend_source_rows = [row for row in source_rows[1:] if row[0].startswith("LEGEND-")]
    test_rows = [row for row in source_rows[1:] if not row[0].startswith("LEGEND-")]
    indexed_test_rows = list(enumerate(test_rows))
    ordered_test_rows = [
        row
        for source_index, row in sorted(
            indexed_test_rows,
            key=lambda indexed: domain_sort_key(indexed[1], headers, indexed[0]),
        )
    ]
    if test_rows != ordered_test_rows:
        raise RuntimeError(
            "QA matrix CSV domain blocks are not in configured execution-priority order."
        )

    def compact_row(*values: str) -> list[str]:
        return [*values, *("" for _ in range(len(headers) - len(values)))]

    status_counts = {"done": 0, "pending": 0, "not-started": 0, "failed": 0}
    for row in test_rows:
        item = dict(zip(headers, row))
        status_counts[qa_status_bucket(item)] += 1
    legend_items = [
        (
            item["ID"].removeprefix("LEGEND-"),
            f"{item['Scenario']} — {item['Next manual oracle']}",
        )
        for item in (dict(zip(headers, row)) for row in legend_source_rows)
    ]
    status_items = [
        (
            "Done",
            f"{status_counts['done']} ({(status_counts['done'] / len(test_rows)):.0%})"
            if test_rows
            else "0 (0%)",
        ),
        ("Pending", str(status_counts["pending"])),
        ("Not started", str(status_counts["not-started"])),
        ("Failed", str(status_counts["failed"])),
        ("Total", str(len(test_rows))),
    ]
    domain_totals = {domain: 0 for domain in DOMAIN_PRIORITY}
    domain_done = {domain: 0 for domain in DOMAIN_PRIORITY}
    for row in test_rows:
        item = dict(zip(headers, row))
        domain = item[DOMAIN_COLUMN]
        domain_totals[domain] += 1
        if qa_status_bucket(item) == "done":
            domain_done[domain] += 1
    domain_priority_items = []
    for domain, priority, _, _ in DOMAIN_METADATA:
        done = domain_done[domain]
        total = domain_totals[domain]
        percentage = (done / total) if total else 0
        open_complexities = [
            parse_complexity(item[COMPLEXITY_COLUMN])
            for item in (dict(zip(headers, row)) for row in test_rows)
            if item[DOMAIN_COLUMN] == domain and qa_status_bucket(item) != "done"
        ]
        remaining_minimum = sum(value[0] for value in open_complexities)
        remaining_maximum = sum(value[1] for value in open_complexities)
        domain_priority_items.append(
            (
                domain,
                str(priority),
                f"{done} passed / {total} total ({percentage:.0%})"
                f" · {remaining_minimum}/{remaining_maximum}",
            )
        )
    overview_rows = [compact_row(
        EFFECT_MARKERS_TITLE,
        "",
        "",
        status_items[0][0],
        status_items[0][1],
    )]
    remaining_status_items = status_items[1:]
    first_block_size = (len(domain_priority_items) + 1) // 2
    domain_priority_blocks = [
        domain_priority_items[:first_block_size],
        domain_priority_items[first_block_size:],
    ]
    domain_priority_block_size = max(len(block) for block in domain_priority_blocks)
    domain_priority_rows = []
    for priority_row_index in range(domain_priority_block_size + 1):
        priority_row = ["" for _ in headers]
        for block_index, block in enumerate(domain_priority_blocks):
            if priority_row_index == 0:
                priority_values = (
                    DOMAIN_COLUMN,
                    "Priority",
                    "Progress / remaining min/max",
                )
            elif priority_row_index <= len(block):
                priority_values = block[priority_row_index - 1]
            else:
                priority_values = ("", "", "")
            block_start = DOMAIN_PRIORITY_BLOCK_STARTS[block_index]
            for value_offset, priority_value in zip(
                DOMAIN_PRIORITY_VALUE_OFFSETS,
                priority_values,
            ):
                priority_row[block_start + value_offset] = priority_value
        domain_priority_rows.append(priority_row)
    for row_index in range(max(
        len(legend_items),
        len(remaining_status_items),
    )):
        marker, marker_explanation = (
            legend_items[row_index] if row_index < len(legend_items) else ("", "")
        )
        status, count = (
            remaining_status_items[row_index]
            if row_index < len(remaining_status_items)
            else ("", "")
        )
        overview_rows.append(
            compact_row(
                marker,
                marker_explanation,
                "",
                status,
                count,
            )
        )

    environment_header = ["" for _ in headers]
    environment_header[headers.index(DEV_COLUMN)] = ENVIRONMENTS_TITLE

    return [
        *overview_rows,
        compact_row(DOMAIN_PRIORITY_TITLE),
        *domain_priority_rows,
        compact_row(QA_TEST_CASES_TITLE),
        environment_header,
        headers,
        *test_rows,
    ]


def qa_status_bucket(item: dict[str, str]) -> str:
    manual_status = item.get("Manual status", "").upper()
    result = item.get("Result", "").upper()
    combined = f"{manual_status} {result}"
    if "FAIL" in combined or "CLEAN RETEST REQUIRED" in combined:
        return "failed"
    if "NOT RUN" in combined or "NOT IMPLEMENTED" in combined:
        return "not-started"
    pending_markers = (
        "PARTIAL",
        "PENDING",
        "IN PROGRESS",
        "OPEN",
        "RETEST REQUIRED",
        "IMPLEMENTATION UPDATED",
        "REQUIRED EVERY SESSION",
    )
    if any(marker in combined for marker in pending_markers):
        return "pending"
    if result == "PASS" or "PASS" in manual_status:
        return "done"
    return "pending"


def progress_style_key(progress: str) -> str:
    try:
        percentage = int(progress.rsplit("(", 1)[1].split("%", 1)[0])
    except (IndexError, ValueError):
        return "default"
    if percentage >= 100:
        return "progress-complete"
    if percentage >= 75:
        return "progress-high"
    if percentage >= 50:
        return "progress-medium"
    if percentage > 0:
        return "progress-low"
    return "progress-zero"


def qa_status_formula(row_index: int, manual_column: str, result_column: str) -> str:
    combined = f'{manual_column}{row_index}&" "&{result_column}{row_index}'

    def contains(marker: str) -> str:
        return f'ISNUMBER(SEARCH("{marker}",{combined}))'

    failed = ",".join((contains("FAIL"), contains("CLEAN RETEST REQUIRED")))
    not_started = ",".join((contains("NOT RUN"), contains("NOT IMPLEMENTED")))
    pending = ",".join(
        contains(marker)
        for marker in (
            "PARTIAL",
            "PENDING",
            "IN PROGRESS",
            "OPEN",
            "RETEST REQUIRED",
            "IMPLEMENTATION UPDATED",
            "REQUIRED EVERY SESSION",
        )
    )
    done = ",".join(
        (f'{result_column}{row_index}="PASS"', contains("PASS"))
    )
    return (
        f'IF(OR({failed}),"Failed",'
        f'IF(OR({not_started}),"Not started",'
        f'IF(OR({pending}),"Pending",'
        f'IF(OR({done}),"Done","Pending"))))'
    )


def domain_progress_formula(
    domain_reference: str,
    domain_column: str,
    status_helper_column: str,
    complexity_min_helper_column: str,
    complexity_max_helper_column: str,
    first_test_row: int,
    last_test_row: int,
) -> str:
    domain_range = f"${domain_column}${first_test_row}:${domain_column}${last_test_row}"
    status_range = (
        f"${status_helper_column}${first_test_row}:"
        f"${status_helper_column}${last_test_row}"
    )
    total = f"COUNTIF({domain_range},{domain_reference})"
    done = (
        f'COUNTIFS({domain_range},{domain_reference},'
        f'{status_range},"Done")'
    )
    complexity_min_range = (
        f"${complexity_min_helper_column}${first_test_row}:"
        f"${complexity_min_helper_column}${last_test_row}"
    )
    complexity_max_range = (
        f"${complexity_max_helper_column}${first_test_row}:"
        f"${complexity_max_helper_column}${last_test_row}"
    )
    remaining_minimum = (
        f"SUMIFS({complexity_min_range},{domain_range},{domain_reference},"
        f'{status_range},"<>Done")'
    )
    remaining_maximum = (
        f"SUMIFS({complexity_max_range},{domain_range},{domain_reference},"
        f'{status_range},"<>Done")'
    )
    return (
        f'{done}&" passed / "&{total}&" total ("&'
        f'IF({total}=0,"0%",TEXT({done}/{total},"0%"))&") · "&'
        f'{remaining_minimum}&"/"&{remaining_maximum}'
    )


def complexity_bound_formula(
    row_index: int,
    complexity_column: str,
    bound: str,
) -> str:
    reference = f"{complexity_column}{row_index}"
    separator = f'SEARCH("/",{reference})'
    if bound == "minimum":
        value = f"LEFT({reference},{separator}-1)"
    elif bound == "maximum":
        value = f"MID({reference},{separator}+1,LEN({reference}))"
    else:
        raise ValueError(f"Unsupported complexity bound: {bound}")
    return f'IF({reference}="","",VALUE({value}))'


def request_full_workbook_recalculation(existing: bytes) -> bytes:
    root = ET.fromstring(existing)
    calculation = root.find(f"{{{NS}}}calcPr")
    if calculation is None:
        calculation = ET.SubElement(root, f"{{{NS}}}calcPr")
    calculation.set("calcMode", "auto")
    calculation.set("fullCalcOnLoad", "1")
    calculation.set("forceFullCalc", "1")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def shared_string_table(rows: list[list[str]]) -> tuple[list[str], dict[str, int]]:
    strings: list[str] = []
    indexes: dict[str, int] = {}
    for row in rows:
        for value in row:
            if value not in indexes:
                indexes[value] = len(strings)
                strings.append(value)
    return strings, indexes


def build_shared_strings(strings: list[str], value_count: int) -> bytes:
    root = ET.Element(f"{{{NS}}}sst", {
        "count": str(value_count),
        "uniqueCount": str(len(strings)),
    })
    xml_space = "{http://www.w3.org/XML/1998/namespace}space"
    for value in strings:
        item = ET.SubElement(root, f"{{{NS}}}si")
        text = ET.SubElement(item, f"{{{NS}}}t")
        if value != value.strip():
            text.set(xml_space, "preserve")
        text.text = value
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def ensure_fill(fills: ET.Element, rgb: str) -> int:
    for index, fill in enumerate(fills.findall(f"{{{NS}}}fill")):
        pattern = fill.find(f"{{{NS}}}patternFill")
        foreground = None if pattern is None else pattern.find(f"{{{NS}}}fgColor")
        if (
            pattern is not None
            and pattern.get("patternType") == "solid"
            and foreground is not None
            and foreground.get("rgb") == rgb
        ):
            return index

    fill = ET.SubElement(fills, f"{{{NS}}}fill")
    pattern = ET.SubElement(fill, f"{{{NS}}}patternFill", {"patternType": "solid"})
    ET.SubElement(pattern, f"{{{NS}}}fgColor", {"rgb": rgb})
    ET.SubElement(pattern, f"{{{NS}}}bgColor", {"indexed": "64"})
    fills.set("count", str(len(fills.findall(f"{{{NS}}}fill"))))
    return len(fills.findall(f"{{{NS}}}fill")) - 1


def ensure_header_font(fonts: ET.Element) -> int:
    for index, font in enumerate(fonts.findall(f"{{{NS}}}font")):
        color = font.find(f"{{{NS}}}color")
        if font.find(f"{{{NS}}}b") is not None and color is not None and color.get("rgb") == "FFFFFFFF":
            return index

    font = ET.SubElement(fonts, f"{{{NS}}}font")
    ET.SubElement(font, f"{{{NS}}}b")
    ET.SubElement(font, f"{{{NS}}}sz", {"val": "10"})
    ET.SubElement(font, f"{{{NS}}}color", {"rgb": "FFFFFFFF"})
    ET.SubElement(font, f"{{{NS}}}name", {"val": "Arial"})
    ET.SubElement(font, f"{{{NS}}}family", {"val": "2"})
    fonts.set("count", str(len(fonts.findall(f"{{{NS}}}font"))))
    return len(fonts.findall(f"{{{NS}}}font")) - 1


def ensure_thick_border(borders: ET.Element, side_names: tuple[str, ...]) -> int:
    for index, border in enumerate(borders.findall(f"{{{NS}}}border")):
        matches = True
        for side_name in ("left", "right", "top", "bottom"):
            side = border.find(f"{{{NS}}}{side_name}")
            expected_style = "medium" if side_name in side_names else None
            if side is None or side.get("style") != expected_style:
                matches = False
                break
        if matches:
            return index

    border = ET.SubElement(borders, f"{{{NS}}}border")
    for side_name in ("left", "right", "top", "bottom"):
        attributes = {"style": "medium"} if side_name in side_names else {}
        side = ET.SubElement(border, f"{{{NS}}}{side_name}", attributes)
        if side_name in side_names:
            ET.SubElement(side, f"{{{NS}}}color", {"rgb": "FF44546A"})
    ET.SubElement(border, f"{{{NS}}}diagonal")
    borders.set("count", str(len(borders.findall(f"{{{NS}}}border"))))
    return len(borders.findall(f"{{{NS}}}border")) - 1


def ensure_cell_style(
    cell_xfs: ET.Element,
    fill_id: int,
    font_id: int = 0,
    border_id: int = 0,
    horizontal: str | None = None,
) -> int:
    for index, style in enumerate(cell_xfs.findall(f"{{{NS}}}xf")):
        alignment = style.find(f"{{{NS}}}alignment")
        if (
            style.get("fillId") == str(fill_id)
            and style.get("fontId") == str(font_id)
            and style.get("borderId") == str(border_id)
            and style.get("applyFill") == "true"
            and style.get("applyAlignment") == "true"
            and alignment is not None
            and alignment.get("wrapText") == "1"
            and alignment.get("vertical") == "top"
            and alignment.get("horizontal") == horizontal
        ):
            return index

    style = ET.SubElement(cell_xfs, f"{{{NS}}}xf", {
        "numFmtId": "164",
        "fontId": str(font_id),
        "fillId": str(fill_id),
        "borderId": str(border_id),
        "xfId": "0",
        "applyFont": "true" if font_id else "false",
        "applyFill": "true",
        "applyBorder": "true" if border_id else "false",
        "applyAlignment": "true",
        "applyProtection": "false",
    })
    alignment_attributes = {
        "vertical": "top",
        "wrapText": "1",
    }
    if horizontal is not None:
        alignment_attributes["horizontal"] = horizontal
    ET.SubElement(style, f"{{{NS}}}alignment", alignment_attributes)
    cell_xfs.set("count", str(len(cell_xfs.findall(f"{{{NS}}}xf"))))
    return len(cell_xfs.findall(f"{{{NS}}}xf")) - 1


def build_styles(existing: bytes) -> tuple[bytes, dict[str, int]]:
    root = ET.fromstring(existing)
    fonts = root.find(f"{{{NS}}}fonts")
    fills = root.find(f"{{{NS}}}fills")
    borders = root.find(f"{{{NS}}}borders")
    cell_xfs = root.find(f"{{{NS}}}cellXfs")
    if fonts is None or fills is None or borders is None or cell_xfs is None:
        raise RuntimeError("QA workbook styles structure is unsupported.")

    styles = {
        key: ensure_cell_style(cell_xfs, ensure_fill(fills, color))
        for key, color in ROW_FILL_COLORS.items()
    }
    header_font = ensure_header_font(fonts)
    styles["header"] = ensure_cell_style(
        cell_xfs,
        ensure_fill(fills, HEADER_FILL_COLOR),
        header_font,
    )
    styles["legend-section"] = ensure_cell_style(
        cell_xfs,
        ensure_fill(fills, LEGEND_SECTION_FILL_COLOR),
        header_font,
    )
    styles["test-section"] = ensure_cell_style(
        cell_xfs,
        ensure_fill(fills, TEST_SECTION_FILL_COLOR),
        header_font,
    )
    styles["default"] = ensure_cell_style(cell_xfs, 0)

    for base_key in (
        "pass", "partial", "fail", "not-run", "test-section"
    ):
        base_style = cell_xfs.findall(f"{{{NS}}}xf")[styles[base_key]]
        styles[f"{base_key}-align-right"] = ensure_cell_style(
            cell_xfs,
            int(base_style.get("fillId", "0")),
            int(base_style.get("fontId", "0")),
            horizontal="right",
        )

    border_variants = {
        "top-left": ("top", "left"),
        "top": ("top",),
        "top-right": ("top", "right"),
        "left": ("left",),
        "right": ("right",),
        "bottom-left": ("bottom", "left"),
        "bottom": ("bottom",),
        "bottom-right": ("bottom", "right"),
    }
    for base_key in tuple(styles):
        base_style = cell_xfs.findall(f"{{{NS}}}xf")[styles[base_key]]
        fill_id = int(base_style.get("fillId", "0"))
        font_id = int(base_style.get("fontId", "0"))
        base_alignment = base_style.find(f"{{{NS}}}alignment")
        horizontal = (
            None if base_alignment is None else base_alignment.get("horizontal")
        )
        for suffix, side_names in border_variants.items():
            styles[f"{base_key}-{suffix}"] = ensure_cell_style(
                cell_xfs,
                fill_id,
                font_id,
                ensure_thick_border(borders, side_names),
                horizontal,
            )
    return ET.tostring(root, encoding="utf-8", xml_declaration=True), styles


def row_style(values: list[str], headers: list[str]) -> str:
    if values == headers:
        return "header"

    item = dict(zip(headers, values))
    identifier = item.get("ID", "")
    legend_styles = {
        "LEGEND-X": "legend-x",
        "LEGEND-DELTA": "legend-delta",
        "LEGEND-INDIRECT": "legend-indirect",
        "LEGEND-GAP": "legend-gap",
        "X": "legend-x",
        "DELTA": "legend-delta",
        "INDIRECT": "legend-indirect",
        "GAP": "legend-gap",
    }
    if identifier in legend_styles:
        return legend_styles[identifier]
    return {
        "done": "pass",
        "pending": "partial",
        "failed": "fail",
        "not-started": "not-run",
    }[qa_status_bucket(item)]


def build_sheet(
    existing: bytes,
    rows: list[list[str]],
    indexes: dict[str, int],
    style_indexes: dict[str, int],
) -> bytes:
    root = ET.fromstring(existing)
    dimension = root.find(f"{{{NS}}}dimension")
    sheet_data = root.find(f"{{{NS}}}sheetData")
    if dimension is None or sheet_data is None:
        raise RuntimeError("QA workbook sheet structure is unsupported.")

    last_visible_column = cell_column(len(rows[0]) - 1)
    status_helper_column_index = len(rows[0])
    status_helper_column = cell_column(status_helper_column_index)
    complexity_min_helper_column_index = len(rows[0]) + 1
    complexity_min_helper_column = cell_column(complexity_min_helper_column_index)
    complexity_max_helper_column_index = len(rows[0]) + 2
    complexity_max_helper_column = cell_column(complexity_max_helper_column_index)
    headers = next((row for row in rows if row and row[0] == "ID"), None)
    if headers is None:
        raise RuntimeError("QA workbook rows do not contain the column header.")
    header_row_index = next(
        row_index
        for row_index, values in enumerate(rows, start=1)
        if values == headers
    )
    first_test_row = header_row_index + 1
    last_test_row = len(rows)
    manual_column = cell_column(headers.index("Manual status"))
    result_column = cell_column(headers.index("Result"))
    domain_column_index = headers.index(DOMAIN_COLUMN)
    domain_column = cell_column(domain_column_index)
    complexity_column_index = headers.index(COMPLEXITY_COLUMN)
    complexity_column = cell_column(complexity_column_index)
    dev_column_index = headers.index(DEV_COLUMN)
    local_column_index = headers.index(LOCAL_COLUMN)
    if local_column_index != dev_column_index + 1:
        raise RuntimeError("Dev and Local columns must remain adjacent.")
    environment_header_row_index = header_row_index - 1
    dimension.set("ref", f"A1:{complexity_max_helper_column}{len(rows)}")
    sheet_data.clear()

    columns = root.find(f"{{{NS}}}cols")
    if columns is None:
        raise RuntimeError("QA workbook sheet does not contain column definitions.")
    overview_last_row = next(
        row_index - 1
        for row_index, values in enumerate(rows, start=1)
        if values[0] == QA_TEST_CASES_TITLE
    )
    columns.clear()
    for column_number, width in enumerate(COMPACT_VISIBLE_COLUMN_WIDTHS, start=1):
        ET.SubElement(
            columns,
            f"{{{NS}}}col",
            {
                "min": str(column_number),
                "max": str(column_number),
                "width": f"{width:g}",
                "hidden": "false",
                "customWidth": "true",
            },
        )
    for helper_column_index in (
        status_helper_column_index,
        complexity_min_helper_column_index,
        complexity_max_helper_column_index,
    ):
        ET.SubElement(
            columns,
            f"{{{NS}}}col",
            {
                "min": str(helper_column_index + 1),
                "max": str(helper_column_index + 1),
                "width": "0.1",
                "hidden": "true",
                "customWidth": "true",
            },
        )

    existing_merges = root.find(f"{{{NS}}}mergeCells")
    if existing_merges is not None:
        root.remove(existing_merges)

    merged_ranges: list[str] = []
    effect_markers_last_row = 1 + len([
        row for row in rows[1:overview_last_row] if row[0] in {"X", "DELTA", "INDIRECT", "GAP"}
    ])
    status_last_row = 1 + len({
        "Pending", "Not started", "Failed", "Total"
    })
    domain_priority_title_row = next(
        row_index
        for row_index, values in enumerate(rows, start=1)
        if values[0] == DOMAIN_PRIORITY_TITLE
    )
    domain_priority_header_row = domain_priority_title_row + 1
    domain_priority_block_size = (len(DOMAIN_METADATA) + 1) // 2
    domain_priority_last_row = domain_priority_header_row + domain_priority_block_size
    domain_priority_column_indexes = tuple(
        column_index
        for start in DOMAIN_PRIORITY_BLOCK_STARTS
        for column_index in range(start, start + DOMAIN_PRIORITY_BLOCK_WIDTH)
    )
    domain_priority_block_ends = tuple(
        start + DOMAIN_PRIORITY_BLOCK_WIDTH - 1
        for start in DOMAIN_PRIORITY_BLOCK_STARTS
    )

    marker_styles = {
        "X": "legend-x",
        "DELTA": "legend-delta",
        "INDIRECT": "legend-indirect",
        "GAP": "legend-gap",
    }
    status_styles = {
        "Total": "test-section",
        "Done": "pass",
        "Pending": "partial",
        "Not started": "not-run",
        "Failed": "fail",
    }
    summary_formulas = {
        "Done": (
            f'COUNTIF({status_helper_column}{first_test_row}:{status_helper_column}{last_test_row},"Done")'
            f'&" ("&IF(COUNTA(A{first_test_row}:A{last_test_row})=0,"0%",'
            f'TEXT(COUNTIF({status_helper_column}{first_test_row}:{status_helper_column}{last_test_row},"Done")/'
            f'COUNTA(A{first_test_row}:A{last_test_row}),"0%"))&")"'
        ),
        "Pending": f'COUNTIF({status_helper_column}{first_test_row}:{status_helper_column}{last_test_row},"Pending")',
        "Not started": f'COUNTIF({status_helper_column}{first_test_row}:{status_helper_column}{last_test_row},"Not started")',
        "Failed": f'COUNTIF({status_helper_column}{first_test_row}:{status_helper_column}{last_test_row},"Failed")',
        "Total": f"COUNTA(A{first_test_row}:A{last_test_row})",
    }
    helper_values = {
        "done": "Done",
        "pending": "Pending",
        "not-started": "Not started",
        "failed": "Failed",
    }

    for row_index, values in enumerate(rows, start=1):
        style_key = row_style(values, headers)
        is_overview_title = values[0] == EFFECT_MARKERS_TITLE
        is_domain_priority_title = values[0] == DOMAIN_PRIORITY_TITLE
        is_test_section = values[0] == QA_TEST_CASES_TITLE
        is_environment_header = row_index == environment_header_row_index
        explicit_line_count = max(value.count("\n") + 1 for value in values)
        is_multiline = explicit_line_count > 1
        row_height = (
            20.0
            if is_overview_title or is_domain_priority_title or is_test_section or is_environment_header
            else min(180.0, max(12.8, explicit_line_count * 15.0))
        )
        if is_overview_title:
            merged_ranges.append(f"A{row_index}:B{row_index}")
        elif is_domain_priority_title:
            merged_ranges.append(f"A{row_index}:{last_visible_column}{row_index}")
        elif domain_priority_header_row <= row_index <= domain_priority_last_row:
            for block_start in DOMAIN_PRIORITY_BLOCK_STARTS:
                domain_start = cell_column(block_start)
                domain_end = cell_column(block_start + 1)
                progress_start = cell_column(block_start + 3)
                progress_end = cell_column(block_start + 4)
                merged_ranges.append(
                    f"{domain_start}{row_index}:{domain_end}{row_index}"
                )
                merged_ranges.append(
                    f"{progress_start}{row_index}:{progress_end}{row_index}"
                )
        elif is_test_section:
            merged_ranges.append(f"A{row_index}:{last_visible_column}{row_index}")
        elif is_environment_header:
            merged_ranges.append(
                f"{cell_column(dev_column_index)}{row_index}:"
                f"{cell_column(local_column_index)}{row_index}"
            )
        row = ET.SubElement(sheet_data, f"{{{NS}}}row", {
            "r": str(row_index),
            "customFormat": "false",
            "ht": f"{row_height:g}",
            "hidden": "false",
            "customHeight": "true" if is_overview_title or is_domain_priority_title or is_test_section or is_environment_header or is_multiline else "false",
            "outlineLevel": "0",
            "collapsed": "false",
        })
        for column_index, value in enumerate(values):
            cell_style_key = style_key
            if is_overview_title:
                if column_index in (0, 1):
                    cell_style_key = "legend-section"
                elif column_index == 3:
                    cell_style_key = status_styles[values[3]]
                elif column_index == 4:
                    cell_style_key = f"{status_styles[values[3]]}-align-right"
                else:
                    cell_style_key = "default"
            elif is_domain_priority_title:
                cell_style_key = "test-section"
            elif is_test_section:
                cell_style_key = "test-section"
            elif is_environment_header:
                cell_style_key = (
                    "header"
                    if column_index in (dev_column_index, local_column_index)
                    else "default"
                )
            elif values[3] in status_styles:
                if column_index in (0, 1) and values[0] in marker_styles:
                    cell_style_key = marker_styles[values[0]]
                elif column_index == 3:
                    cell_style_key = status_styles[values[3]]
                elif column_index == 4:
                    cell_style_key = f"{status_styles[values[3]]}-align-right"
                else:
                    cell_style_key = "default"
            if (
                row_index == domain_priority_header_row
                and column_index in domain_priority_column_indexes
            ):
                cell_style_key = "header"
            elif (
                domain_priority_header_row < row_index <= domain_priority_last_row
                and column_index in domain_priority_column_indexes
            ):
                block_start = max(
                    start
                    for start in DOMAIN_PRIORITY_BLOCK_STARTS
                    if start <= column_index
                )
                domain_name = values[block_start]
                if column_index in (block_start + 3, block_start + 4):
                    cell_style_key = progress_style_key(values[block_start + 3])
                elif domain_name in DOMAIN_STYLE_KEY:
                    cell_style_key = DOMAIN_STYLE_KEY[domain_name]
            if (
                first_test_row <= row_index <= last_test_row
                and column_index == domain_column_index
            ):
                domain_name = values[domain_column_index]
                cell_style_key = DOMAIN_STYLE_KEY[domain_name]
            if row_index <= effect_markers_last_row and column_index in (0, 1):
                border_parts = []
                if row_index == 1:
                    border_parts.append("top")
                elif row_index == effect_markers_last_row:
                    border_parts.append("bottom")
                if column_index == 0:
                    border_parts.append("left")
                elif column_index == 1:
                    border_parts.append("right")
                if border_parts:
                    cell_style_key = f"{cell_style_key}-{'-'.join(border_parts)}"
            elif row_index <= status_last_row and column_index in (3, 4):
                border_parts = []
                if row_index == 1:
                    border_parts.append("top")
                elif row_index == status_last_row:
                    border_parts.append("bottom")
                if column_index == 3:
                    border_parts.append("left")
                elif column_index == 4:
                    border_parts.append("right")
                if border_parts:
                    cell_style_key = f"{cell_style_key}-{'-'.join(border_parts)}"
            elif (
                domain_priority_header_row <= row_index <= domain_priority_last_row
                and column_index in domain_priority_column_indexes
            ):
                border_parts = []
                if row_index == domain_priority_header_row:
                    border_parts.append("top")
                elif row_index == domain_priority_last_row:
                    border_parts.append("bottom")
                if column_index in DOMAIN_PRIORITY_BLOCK_STARTS:
                    border_parts.append("left")
                elif column_index in domain_priority_block_ends:
                    border_parts.append("right")
                if border_parts:
                    cell_style_key = f"{cell_style_key}-{'-'.join(border_parts)}"
            reference = f"{cell_column(column_index)}{row_index}"
            if column_index == 4 and values[3] in summary_formulas:
                cell_attributes = {
                    "r": reference,
                    "s": str(style_indexes[cell_style_key]),
                }
                if values[3] == "Done":
                    cell_attributes["t"] = "str"
                cell = ET.SubElement(row, f"{{{NS}}}c", cell_attributes)
                ET.SubElement(cell, f"{{{NS}}}f").text = summary_formulas[values[3]]
                ET.SubElement(cell, f"{{{NS}}}v").text = value
                continue
            if (
                domain_priority_header_row < row_index <= domain_priority_last_row
                and column_index in tuple(
                    start + DOMAIN_PRIORITY_VALUE_OFFSETS[2]
                    for start in DOMAIN_PRIORITY_BLOCK_STARTS
                )
            ):
                block_start = max(
                    start
                    for start in DOMAIN_PRIORITY_BLOCK_STARTS
                    if start <= column_index
                )
                if values[block_start]:
                    cell = ET.SubElement(row, f"{{{NS}}}c", {
                        "r": reference,
                        "s": str(style_indexes[cell_style_key]),
                        "t": "str",
                    })
                    ET.SubElement(cell, f"{{{NS}}}f").text = domain_progress_formula(
                        f"{cell_column(block_start)}{row_index}",
                        domain_column,
                        status_helper_column,
                        complexity_min_helper_column,
                        complexity_max_helper_column,
                        first_test_row,
                        last_test_row,
                    )
                    ET.SubElement(cell, f"{{{NS}}}v").text = value
                    continue
            cell = ET.SubElement(row, f"{{{NS}}}c", {
                "r": reference,
                "s": str(style_indexes[cell_style_key]),
                "t": "s",
            })
            ET.SubElement(cell, f"{{{NS}}}v").text = str(indexes[value])

        if first_test_row <= row_index <= last_test_row:
            bucket = qa_status_bucket(dict(zip(headers, values)))
            helper_cell = ET.SubElement(
                row,
                f"{{{NS}}}c",
                {
                    "r": f"{status_helper_column}{row_index}",
                    "s": str(style_indexes["default"]),
                    "t": "str",
                },
            )
            ET.SubElement(helper_cell, f"{{{NS}}}f").text = qa_status_formula(
                row_index,
                manual_column,
                result_column,
            )
            ET.SubElement(helper_cell, f"{{{NS}}}v").text = helper_values[bucket]

            item = dict(zip(headers, values))
            complexity = item[COMPLEXITY_COLUMN]
            complexity_bounds = parse_complexity(complexity) if complexity else ("", "")
            for helper_column, bound_name, cached_value in (
                (complexity_min_helper_column, "minimum", complexity_bounds[0]),
                (complexity_max_helper_column, "maximum", complexity_bounds[1]),
            ):
                complexity_cell = ET.SubElement(
                    row,
                    f"{{{NS}}}c",
                    {
                        "r": f"{helper_column}{row_index}",
                        "s": str(style_indexes["default"]),
                    },
                )
                ET.SubElement(complexity_cell, f"{{{NS}}}f").text = (
                    complexity_bound_formula(
                        row_index,
                        complexity_column,
                        bound_name,
                    )
                )
                ET.SubElement(complexity_cell, f"{{{NS}}}v").text = str(cached_value)

    merges = ET.Element(f"{{{NS}}}mergeCells", {"count": str(len(merged_ranges))})
    for merged_range in merged_ranges:
        ET.SubElement(merges, f"{{{NS}}}mergeCell", {
            "ref": merged_range,
        })
    sheet_position = list(root).index(sheet_data)
    root.insert(sheet_position + 1, merges)

    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def synchronize() -> None:
    loaded_source_rows = load_rows()
    source_rows = order_source_rows(loaded_source_rows)
    if source_rows != loaded_source_rows:
        write_source_rows(source_rows)
    rows = workbook_rows(source_rows)
    strings, indexes = shared_string_table(rows)
    with zipfile.ZipFile(XLSX_PATH, "r") as workbook:
        entries = {name: workbook.read(name) for name in workbook.namelist()}

    entries[STYLES_XML], style_indexes = build_styles(entries[STYLES_XML])
    entries[WORKBOOK_XML] = request_full_workbook_recalculation(entries[WORKBOOK_XML])
    entries[SHARED_STRINGS_XML] = build_shared_strings(
        strings,
        sum(len(row) for row in rows),
    )
    entries[SHEET_XML] = build_sheet(entries[SHEET_XML], rows, indexes, style_indexes)

    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{XLSX_PATH.stem}.",
        suffix=".xlsx",
        dir=XLSX_PATH.parent,
    )
    os.close(file_descriptor)
    temporary_path = Path(temporary_name)
    try:
        with zipfile.ZipFile(temporary_path, "w", zipfile.ZIP_DEFLATED) as workbook:
            for name, content in entries.items():
                workbook.writestr(name, content)
        with zipfile.ZipFile(temporary_path, "r") as workbook:
            if workbook.testzip() is not None:
                raise RuntimeError("Generated QA workbook failed ZIP validation.")
        os.replace(temporary_path, XLSX_PATH)
    finally:
        temporary_path.unlink(missing_ok=True)

    print(
        f"Synchronized {XLSX_PATH.relative_to(REPOSITORY_ROOT)} "
        f"from {CSV_PATH.name}: {len(source_rows) - 1} ledger rows"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--csv', type=Path, required=True)
    parser.add_argument('--xlsx', type=Path, required=True)
    parser.add_argument('--repository-root', type=Path, required=True)
    options = parser.parse_args()
    CSV_PATH = options.csv.resolve(strict=True)
    XLSX_PATH = options.xlsx.resolve(strict=True)
    REPOSITORY_ROOT = options.repository_root.resolve(strict=True)
    synchronize()
