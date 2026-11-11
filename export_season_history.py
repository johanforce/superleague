#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Export Super League season history from memory/super_league.db.

No third-party packages are required. The tool creates:
  * One Excel workbook, with exactly one worksheet per archived season.
  * Optionally (default: yes), one UTF-8 CSV file per season.

The exported sections mirror the app's History > Season detail dialog:
Overview, Awards, Leagues, Cup & C1, Players, and Matches.
Hall of Fame is intentionally excluded because it is a separate app tab.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sqlite3
import sys
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET
from urllib.parse import quote

APP_TITLE = "Super League · Lịch sử mùa"
TIER_LABELS = {
    "premier": "Premier League",
    "fc1": "Football Championship 1",
    "fc2": "Football Championship 2",
}
CUP_NAMES = {
    "r1": "Vòng 1",
    "r16": "Vòng 16",
    "qf": "Tứ kết",
    "sf": "Bán kết",
    "final": "Chung kết",
}

MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CONTENT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
XML_NS = "http://www.w3.org/XML/1998/namespace"
ET.register_namespace("", MAIN_NS)
ET.register_namespace("r", REL_NS)


def xml_tag(name: str) -> str:
    return f"{{{MAIN_NS}}}{name}"


def safe_xml_text(value: Any) -> str:
    text = str(value if value is not None else "")
    # XML 1.0 does not allow most C0 control characters.
    return "".join(ch for ch in text if ch in "\t\n\r" or ord(ch) >= 0x20)


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def safe_sheet_name(label: str, fallback: str) -> str:
    name = re.sub(r"[\\/*?:\[\]]", "-", str(label or fallback)).strip("' ")
    return (name or fallback)[:31]


def find_project_root() -> tuple[Path | None, Path | None]:
    """Find the project root when this file is placed in project/src or project/."""
    script_dir = Path(__file__).resolve().parent
    candidates: list[Path] = []
    for base in (script_dir, *script_dir.parents, Path.cwd(), *Path.cwd().parents):
        try:
            resolved = base.resolve()
        except OSError:
            resolved = base
        if resolved not in candidates:
            candidates.append(resolved)
    for root in candidates:
        db = root / "memory" / "super_league.db"
        if db.is_file():
            return root, db
    return None, None


def connect_readonly(db_path: Path) -> sqlite3.Connection:
    absolute = str(db_path.resolve()).replace(os.sep, "/")
    uri = "file:" + quote(absolute, safe="/:\\") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=15)
    conn.row_factory = sqlite3.Row