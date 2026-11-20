"""Beta keys: pure helpers (CSV parsing, how codes are shown). No Discord or database in here, so they are easy to test."""
import csv
import io
import re
from typing import NamedTuple

MAX_CODE_LEN = 200          # a longer "code" is almost certainly a wrong column or a broken file
INLINE_MAX_CODES = 25       # up to this many codes go straight into the message, more than that becomes a .txt file
INLINE_MAX_CHARS = 1500     # (Discord messages stop at 2000 characters)
FILE_NAME = "rivals-fantasy-beta-keys.txt"

# A header cell is something like "code", "Codes", "Beta Key", "redeem_code" ...
_HEADER_RE = re.compile(r"^(beta|redeem|activation|access|product|game|invite|promo)?(code|key)s?$")


class KeysError(Exception):
    """A plain-words reason a file can't be used."""


class ParsedCodes(NamedTuple):
    codes: list[str]        # clean, unique, in file order
    header_found: bool      # a header row was detected and skipped
    column: int             # 0-based column the codes were read from
    blank: int              # rows that had other cells but nothing in the code column
    invalid: int            # codes with spaces inside or an absurd length
    duplicates: int         # repeats of a code that appeared earlier in the same file


def parse_csv(data: bytes) -> ParsedCodes:
    """Read one code per row. If the first row has a column called code/key/beta key, that column is used (and the
    row skipped); otherwise the first column is used. Surrounding spaces and a UTF-8 BOM (Excel adds one) are ignored."""
    text = data.decode("utf-8-sig", errors="replace")
    if "\x00" in text:
        raise KeysError("That doesn't look like a CSV (it contains binary data). Export it as plain CSV and try again.")
    try:
        rows = list(csv.reader(io.StringIO(text)))
    except csv.Error as e:
        raise KeysError(f"I couldn't read that CSV ({e}).") from e

    rows = [r for r in rows if any(c.strip() for c in r)]       # drop empty lines
    column, header_found = 0, False
    if rows:
        for idx, cell in enumerate(rows[0]):
            if _HEADER_RE.match(re.sub(r"[^a-z]", "", cell.lower())):
                column, header_found = idx, True
                break
        if header_found:
            rows = rows[1:]

    codes: list[str] = []
    seen: set[str] = set()
    blank = invalid = duplicates = 0
    for row in rows:
        cell = row[column].strip() if len(row) > column else ""
        if not cell:
            blank += 1
        elif len(cell) > MAX_CODE_LEN or re.search(r"\s", cell):
            invalid += 1
        elif cell in seen:
            duplicates += 1
        else:
            seen.add(cell)
            codes.append(cell)
    return ParsedCodes(codes, header_found, column, blank, invalid, duplicates)


def mask(code: str) -> str:
    """A code with all but the first two characters hidden, for confirming an import without showing secrets."""
    return code[:2] + "•" * min(max(len(code) - 2, 0), 10)


def render_codes(codes: list[str]) -> tuple[str, bool]:
    """(text, fits_in_a_message). One code per line; big batches should be sent as a file instead."""
    text = "\n".join(codes)
    return text, len(codes) <= INLINE_MAX_CODES and len(text) <= INLINE_MAX_CHARS
