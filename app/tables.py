"""Spreadsheets Oscar looks things up in: a price list, stock, orders, bookings.

The owner uploads a sheet (Excel or CSV, read into rows by app/sheets.py) under
Agent settings. On a call, the decision that already chooses between the web,
the fact sheet and Oscar's own reply (app/search.py) is shown each sheet's name,
what it holds, its columns and a row or two, and can answer "look in the
spreadsheets for these words". The rows are found here, in plain Python (no
model, a few milliseconds), and a text model writes the answer from them, which
the speech model says word for word, as for the fact sheet.

- A small sheet is handed over whole, so "what's the cheapest?" or "how many
  do you have in blue?" can be answered from every row. A big one is searched:
  the rows that match the words best, weighed so that a rare word (a product
  name, an order number) counts for more than a common one.
- A sheet about people can be private: then a caller is only told about rows
  that carry the number they are calling from. A browser call is the owner
  trying Oscar out, so it sees every row.

Only the standard library here: the WhatsApp process (its own environment,
without openpyxl) looks rows up with the same code.
"""

import csv
import io
import math
import re
import unicodedata
from dataclasses import dataclass, field

# What one sheet may hold, and how many sheets an agent may have.
TABLE_ROWS_MOST = 10_000
TABLE_COLUMNS_MOST = 50
TABLE_CELL_CHARS = 300
TABLE_TEXT_MOST = 2_000_000
TABLES_MOST = 10
TABLE_NAME_CHARS = 80
TABLE_DESCRIPTION_CHARS = 500
# A sheet whose rows fit in this many characters (as CSV) is read whole.
WHOLE_TABLE_CHARS = 16_000
# Otherwise the best-matching rows, up to this many and this much text.
MATCHED_ROWS_MOST = 40
MATCHED_ROWS_CHARS = 8_000
# A row matches when it scores at least this share of the best row's score.
MATCH_SHARE_OF_BEST = 0.34
# The rows that carry the caller's own number, put first.
OWN_ROWS_MOST = 5
# A caller's number matches a cell that ends with the same last digits.
OWN_NUMBER_DIGITS = 8
# What the deciding model is shown of each sheet.
SAMPLE_ROWS = 2
SAMPLE_CHARS = 300
# How far down the file the headings are looked for (below a title, say).
HEADING_ROWS_LOOKED_AT = 10

# Words that say nothing about which row is wanted.
_COMMON = frozenset(
    """a an the and or of for to in on at by with from about as is are was were be been do does did
    have has had i you he she it we they me my your our their this that these those what which who
    whom whose when where why how can could would should will shall may might must please tell
    know want need get got give any some much many there here just also not no yes if then than
    so too very one ones do don't is it's i'm im let us me there's thats that's""".split()
)
_WORD = re.compile(r"[^\W_]+", flags=re.UNICODE)


class TableError(ValueError):
    """A sheet that cannot be made into rows, said in words."""


@dataclass
class Table:
    """A spreadsheet Oscar can look things up in."""

    id: str
    name: str
    description: str
    headings: list[str]
    rows: list[list[str]]
    private: bool = False
    # Filled in on first use (index_of): each row's words, and how many rows have each word.
    _words: list[frozenset[str]] = field(default_factory=list, repr=False, compare=False)
    _rows_with: dict[str, int] = field(default_factory=dict, repr=False, compare=False)
    _digits: list[list[str]] = field(default_factory=list, repr=False, compare=False)


def tables_from(records: list[dict]) -> list[Table]:
    """The sheets as Store.lookup_tables_with_rows gives them, ready to look things up in."""
    return [
        Table(
            id=record["id"],
            name=record["name"],
            description=record["description"],
            headings=record["headings"],
            rows=record["rows"],
            private=record["private"],
        )
        for record in records
    ]


# Reading a sheet


def _clean(cell: str) -> str:
    return " ".join(str(cell).split())[:TABLE_CELL_CHARS]


def read_table(text: str) -> tuple[list[str], list[list[str]]]:
    """(headings, rows) of a sheet given as CSV text (an Excel sheet comes as that too).

    The headings are the first of the first ten rows filled in about as fully as
    the fullest of them, so a title above them is passed over. A heading left empty becomes
    "Column N", and one used twice gets a number. Raises TableError.
    """
    if len(text) > TABLE_TEXT_MOST:
        raise TableError("That sheet has too much text. Keep the columns Oscar needs, or split it.")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    try:
        rows = [
            [_clean(cell) for cell in row[:TABLE_COLUMNS_MOST]]
            for row in csv.reader(io.StringIO(text, newline=""), dialect)
        ]
    except csv.Error as exc:
        raise TableError(f"The file could not be read as rows ({exc}). Save it again as CSV or Excel.") from None
    rows = [row for row in rows if any(row)]
    if not rows:
        raise TableError("The sheet has no rows.")
    filled = [sum(1 for cell in row if cell) for row in rows[:HEADING_ROWS_LOOKED_AT]]
    # A title ("Price list, November") fills a cell or two; the headings fill most.
    at = next((i for i, n in enumerate(filled) if 4 * n >= 3 * max(filled)), 0)
    headings, body = rows[at], rows[at + 1 :]
    width = max([len(headings)] + [len(row) for row in body])
    named: list[str] = []
    for i in range(width):
        heading = headings[i] if i < len(headings) and headings[i] else f"Column {i + 1}"
        base, n = heading[:60], 2
        while heading.casefold() in (seen.casefold() for seen in named):
            heading, n = f"{base} {n}", n + 1
        named.append(heading)
    body = [row + [""] * (width - len(row)) for row in body]
    if not body:
        raise TableError("The sheet has headings but no rows under them.")
    if len(body) > TABLE_ROWS_MOST:
        raise TableError(f"That sheet has {len(body):,} rows. Oscar can look things up in {TABLE_ROWS_MOST:,} at most.")
    # A column with nothing in it says nothing.
    used = [i for i in range(width) if any(row[i] for row in body)]
    return [named[i] for i in used], [[row[i] for i in used] for row in body]


# Finding rows


def words_of(text: str) -> list[str]:
    """The words in `text`, as compared: lower case, without accents, a plural's "s" off."""
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode() or text
    found = []
    for word in _WORD.findall(plain.casefold()):
        if len(word) > 3 and word.endswith("s") and not word.endswith("ss") and not word.isdigit():
            word = word[:-1]
        found.append(word)
    return found


def digits_of(text: str) -> str:
    return re.sub(r"\D", "", text)


def index_of(table: Table) -> Table:
    """The table with its rows' words worked out (once)."""
    if len(table._words) != len(table.rows):
        table._words = [frozenset(words_of(" ".join(row))) for row in table.rows]
        table._rows_with = {}
        for words in table._words:
            for word in words:
                table._rows_with[word] = table._rows_with.get(word, 0) + 1
        table._digits = [[digits_of(cell) for cell in row] for row in table.rows]
    return table


def _weight(table: Table, word: str) -> float:
    """A rare word says more about which row is meant than a common one."""
    return math.log((len(table.rows) + 1) / (table._rows_with.get(word, 0) + 0.5)) + 1


def matching_rows(table: Table, words: str, *, most: int = MATCHED_ROWS_MOST) -> list[int]:
    """The rows that match `words` best, best first (their positions in table.rows).

    A word counts in full when a row has it, and in part when a row has a word
    that starts with it ("choc" for "chocolate"); a number only counts whole.
    """
    index_of(table)
    asked = [word for word in dict.fromkeys(words_of(words)) if word not in _COMMON]
    if not asked:
        return []
    vocabulary = table._rows_with
    scores: list[float] = [0.0] * len(table.rows)
    for word in asked:
        if word in vocabulary:
            weight = _weight(table, word)
            for i, row_words in enumerate(table._words):
                if word in row_words:
                    scores[i] += weight
        if len(word) >= 3 and not word.isdigit():
            longer = [known for known in vocabulary if known != word and known.startswith(word)]
            for known in longer[:50]:
                weight = 0.6 * _weight(table, known)
                for i, row_words in enumerate(table._words):
                    if known in row_words and word not in row_words:
                        scores[i] += weight
    best = max(scores, default=0.0)
    # Rows that only share a word every row has ("ORD" of ORD-10234) are not matches.
    ranked = sorted(
        (i for i, score in enumerate(scores) if score > 0 and score >= MATCH_SHARE_OF_BEST * best),
        key=lambda i: (-scores[i], i),
    )
    return ranked[:most]


def own_rows(table: Table, caller: str) -> list[int]:
    """The rows with a cell holding the caller's number (its last eight digits)."""
    index_of(table)
    tail = digits_of(caller)[-OWN_NUMBER_DIGITS:]
    if len(tail) < OWN_NUMBER_DIGITS:
        return []
    return [i for i, cells in enumerate(table._digits) if any(len(c) >= OWN_NUMBER_DIGITS and c.endswith(tail) for c in cells)]


def rows_text(headings: list[str], rows: list[list[str]]) -> str:
    """The rows as the reader is shown them: a line each, cells between " | ".

    Not commas: a phone number straight after one is not seen whole by
    app/search.long_numbers, which keeps long numbers away from the model."""
    def line(cells: list[str]) -> str:
        return " | ".join(cell.replace("|", "/") for cell in cells)

    return "\n".join([line(headings), *(line(row) for row in rows)])


@dataclass(frozen=True)
class Found:
    """What a lookup found in one sheet, as the reader is shown it."""

    table: str  # its name
    text: str  # the reader's view of it
    rows: int  # how many rows of it were shown
    # Those rows, for the dashboard to show what Oscar read.
    headings: list[str] = field(default_factory=list)
    cells: list[list[str]] = field(default_factory=list)


def look_up(tables: list[Table], words: str, caller: str | None) -> list[Found]:
    """What to show the reader of each sheet for `words`.

    `caller` is the number the other person is on ("" when it is not known);
    None for the owner (a browser call), who may see every row.
    """
    found = []
    room = WHOLE_TABLE_CHARS  # for sheets read whole, between them
    for table in tables:
        index_of(table)
        mine = own_rows(table, caller) if caller else []
        if table.private and caller is not None and not mine:
            found.append(Found(table.name, (
                f'The spreadsheet "{table.name}" is private: its rows are only told to the person '
                "they are about, and none of them has the number this caller is calling from."
            ), 0))
            continue
        # A private sheet shows a caller only their own rows.
        visible = mine if table.private and caller is not None else list(range(len(table.rows)))
        whole = rows_text(table.headings, [table.rows[i] for i in visible])
        if len(whole) <= room:
            room -= len(whole)
            shown = visible
            if table.private and caller is not None:
                about = f'The spreadsheet "{table.name}": the {len(visible)} row(s) about this caller.'
            else:
                about = f'The spreadsheet "{table.name}": all {len(visible):,} of its rows.'
        else:
            allowed = set(visible)
            matched = [i for i in matching_rows(table, words, most=len(table.rows)) if i in allowed]
            shown = list(dict.fromkeys(mine[:OWN_ROWS_MOST] + matched))[:MATCHED_ROWS_MOST]
            if not shown:
                found.append(Found(table.name, (
                    f'The spreadsheet "{table.name}" has {len(visible):,} rows, and none matches "{words}".'
                ), 0))
                continue
            # Whole rows only, as many as there is room for.
            while len(shown) > 1 and len(rows_text(table.headings, [table.rows[i] for i in shown])) > MATCHED_ROWS_CHARS:
                shown = shown[: len(shown) * 3 // 4]
            about = (
                f'The spreadsheet "{table.name}" has {len(visible):,} rows. These are the {len(shown)} that '
                f'best match "{words}", best first.'
            )
        if table.description:
            about += f" It holds: {table.description}"
        headings = list(table.headings)
        cells = [list(table.rows[i]) for i in shown]
        if mine:
            about += ' The caller is calling from a number in the rows marked "yes" under "Calling from this number".'
            headings.append("Calling from this number")
            own = set(mine)
            cells = [row + ["yes" if i in own else ""] for i, row in zip(shown, cells)]
        found.append(Found(table.name, f"{about}\n{rows_text(headings, cells)}", len(shown), headings, cells))
    return found


# What the deciding model is shown


def sample(table: Table) -> str:
    """A row or two of the sheet, as "heading: value; ..." (cut short)."""
    lines = []
    for row in table.rows[:SAMPLE_ROWS]:
        line = "; ".join(f"{h}: {v}" for h, v in zip(table.headings, row) if v)
        lines.append(line[:SAMPLE_CHARS])
    return "\n".join(f"  e.g. {line}" for line in lines)


def tables_summary(tables: list[Table]) -> str:
    """Each sheet as the deciding model is shown it: name, what it holds, columns, an example."""
    parts = []
    for table in tables:
        head = f'- "{table.name}" ({len(table.rows):,} rows)'
        if table.description:
            head += f": {table.description}"
        parts.append(f"{head}\n  Columns: {', '.join(table.headings)}\n{sample(table)}")
    return "\n".join(parts)
