"""
RISC OS relocatable module parser.

Parses metadata from RISC OS relocatable module binaries, extracting
title, version, date, SWI names, star commands, and module flags.

Based on code by Peter Howkins, adapted for Arcology.
"""

import hashlib
import re
import struct

# ---------------------------------------------------------------------------
# Help string regular expressions
# ---------------------------------------------------------------------------

RE_HELP_TITLE   = r"(?P<help_title>.+)"
RE_VERSION      = r"(?P<version>[0-9]\.[0-9][0-9][a-z]?)"
RE_DATE         = r"\((?P<date>[0-9]{2} [A-Za-z]{3} [0-9]{4})\)"
RE_OTHER        = r"(?P<other>.*)"
RE_HELP_STRING  = re.compile(
    r"^" + RE_HELP_TITLE + r"[ \t]+" + RE_VERSION + " +" + RE_DATE + RE_OTHER + "$"
)

RE_DATE_STRING = re.compile(
    r"^(?P<day>[0-9]{2}) (?P<month>[A-Za-z]{3}) (?P<year>[0-9]{4})$"
)

MONTH_NAME_TO_NUMBER = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5,  "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}


# ---------------------------------------------------------------------------
# OS_PrettyPrint token dictionary
# ---------------------------------------------------------------------------

PRETTY_PRINT_TOKEN = {
    1:  "Syntax: *{}",
    2:  " the ",
    3:  "director",
    4:  "filing system",
    5:  "current",
    7:  "file",
    8:  "default ",
    9:  "tion",
    10: "*Configure ",
    11: "name",
    12: " server",
    13: "number",
    14: "Syntax: *{} <",
    15: " one or more files that match the given wildcard",
    16: " and ",
    17: "relocatable module",
    18: "\nC(onfirm)\tPrompt for confirmation of each ",
    19: "sets the ",
    20: "Syntax: *{} [<disc spec.>]",
    21: "}}\nV(erbose)\tPrint information on each file ",
    26: "printe",
    27: "Syntax: *{} <filename>",
    28: "select",
    29: "xpression",
    30: "Syntax: *{} [",
    31: "sprite",
    32: " displays",
    33: "free space",
    34: " {{off}}",
    35: "library",
    36: "parameter",
    37: "object",
    38: " all ",
    39: "disc",
    40: " to ",
    41: " is ",
}


# ---------------------------------------------------------------------------
# Quirks: SHA-256 -> corrected help string for known malformed modules
# ---------------------------------------------------------------------------

QUIRKS = {
    # Help string of BASIC in RISC OS 2.01 is missing completely (version/date comes from *Modules)
    "e02960384a93222f718434123b41703ebc5fd01adc78cf947661741c52179a7a": "BBC BASIC V\t1.04 (05 Oct 1988)",

    # Help string of WindowManager in RISC OS 2.01 is missing completely (version/date comes from *Modules)
    "d2d2a137304191f546f573cc500da28e80d1f3c3bbcf681d9e1203484505f253": "Window Manager\t2.05 (31 Jan 1990)",

    # "MOS Utilities\t3.19 (9. Jun 1993)" in RISC OS 3.19 (German)
    "8dfa8193eb096443e478487dbb1c9f40326e166f9d5541d3397bb5789f116ef3": "MOS Utilities\t3.19 (09 Jun 1993)",

    # "SoakTest\t0.20 (20-Sep-96)" in NC OS 0.10
    "744d28a74b8b2e80b4ccb4247dedf1bad70e9d0d3d71c78166aeeffb6170fec9": "SoakTest 0.20 (20 Sep 1996)",

    # "!NCKeyboard\t0.58 1.47.2.2 (03 Feb 2000)" in RISC OS-NC 5.13 (Bush IBX)
    "9fb67d3591f44a0f590419b4f671972deff526c1b39a36463379207573b1eae5": "!NCKeyboard\t0.58 (03 Feb 2000) 1.47.2.2",

    # "PCPS2Driver\t0.20 (14 July 1998)' in RISC OS 3.80 (Phoebe)
    "aab4afe12e6f0700c629efad4b3781dd873bfdb9c036b16f5f881a2702760d97": "PCPS2Driver\t0.20 (14 Jul 1998)",

    # "C Library\t4.88 (22 Sep 1999" in RISC OS 4.03 (Mico)
    "76ad4c8634df74cbfef79c647a3d52c462ee9b99db4dcce36274092e0142671a": "C Library\t4.88 (22 Sep 1999)",

    # "C Library\t4.88 (22 Sep 1999" in RISC OS 4.03 (R7500)
    "ac034f30c0cc4399d587b873524381eff91a859524eac6a2010f13cf74f5d7cc": "C Library\t4.88 (22 Sep 1999)",

    # "C Library\t4.88 (22 Sep 1999" in RISC OS 4.04
    "dc018289de9d13aff3eb95f9c732d3f3bfb91b57ebfbd775237842db15d63158": "C Library\t4.88 (22 Sep 1999)",

    # "Podule Manager\t1.55 (A9 05 May 2005)" in RISC OS 4.42 (A9home)
    "12365f0f1d2f4c2f4a969d59f51515631c86494857854a1700cfe18cff7a7a64": "Podule Manager\t1.55 (05 May 2005) A9",

    # "PCCardFSFiler\t0.11 (8th Sept 1995)" in RISC OS 4.71 (Acorn)
    "ab7eab0ab1ec935945f086c8c8b8de39d21a1c22e5411a1fd1c2f690435dede2": "PCCardFSFiler\t0.11 (08 Sep 1995)",
}


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class HelpParseError(Exception):
    """Raised when a module help string cannot be parsed."""
    pass


class PrettyPrintUnimplemented(Exception):
    """Raised when an unrecognised OS_PrettyPrint token is encountered."""
    pass


class ModuleParseError(Exception):
    """Raised when a module binary cannot be parsed at all."""
    pass


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _parse_module_date_string(d):
    """Parse a module date string like '01 Jan 1995' into ISO format."""
    m = RE_DATE_STRING.search(d)
    if not m:
        raise HelpParseError(f"Date string not matched: '{d}'")
    day = int(m.group("day"))
    mth = m.group("month").capitalize()
    if mth not in MONTH_NAME_TO_NUMBER:
        raise HelpParseError(f"Unknown month in date string: '{d}'")
    month = MONTH_NAME_TO_NUMBER[mth]
    year = int(m.group("year"))
    return f"{year:04}-{month:02}-{day:02}"


def _read_string(data, pos):
    """Read a zero-terminated string from data at pos. Returns (string, end_pos)."""
    result = []
    while pos < len(data):
        byte = data[pos]
        if byte == 0:
            return (''.join(result), pos)
        result.append(chr(byte))
        pos += 1
    return (''.join(result), pos)


def _read_pretty_print(data, pos, r2_string):
    """Read a zero-terminated OS_PrettyPrint string, detokenising as needed."""
    result = []
    esc = False
    while pos < len(data):
        byte = data[pos]
        if esc:
            if byte == 0:
                result.append(r2_string)
            elif byte in PRETTY_PRINT_TOKEN:
                result.append(PRETTY_PRINT_TOKEN[byte].format(r2_string))
            else:
                raise PrettyPrintUnimplemented(
                    f"Unknown escape token {byte} (so far = '{''.join(result)}')"
                )
            esc = False
        else:
            if byte == 0:
                return ''.join(result)
            elif byte == 13:
                result.append('\n')
            elif byte == 27:
                esc = True
            elif byte == 31:  # RISC OS hard space
                result.append('\u00a0')  # No-Break Space
            else:
                result.append(chr(byte))
        pos += 1
    return ''.join(result)


def _read_swis(data, pos):
    """Read SWI chunk name and individual SWI names from data at pos."""
    result = []
    (swi_chunk_name, pos) = _read_string(data, pos)
    result.append(swi_chunk_name)
    pos += 1
    while pos < len(data) and data[pos] != 0:
        (swi_name, pos) = _read_string(data, pos)
        result.append(swi_name)
        pos += 1
    return result


def _parse_help_string(help_string):
    """Parse a module help string into (help_title, version, date, other)."""
    m = RE_HELP_STRING.search(help_string)
    if not m:
        raise HelpParseError(f"Help string not matched: '{help_string}'")
    return (
        m.group("help_title").strip(),
        m.group("version"),
        m.group("date"),
        m.group("other").strip(),
    )


def _decode_command_table(data, command_table):
    """Decode the star command table starting at the given offset."""
    result = []
    pos = command_table
    while pos < len(data) and data[pos] != 0:
        (command_name, end) = _read_string(data, pos)
        pos = end + 1
        # Word-align
        pos = (pos + 3) & (~3)

        if pos + 16 > len(data):
            break

        (code, info, command_syntax_pos, command_help_pos) = struct.unpack(
            "< IIII", data[pos:pos + 16]
        )

        command_syntax = None
        if command_syntax_pos != 0 and command_syntax_pos < len(data):
            try:
                command_syntax = _read_pretty_print(data, command_syntax_pos, command_name)
            except PrettyPrintUnimplemented:
                pass

        command_help = None
        if (command_help_pos != 0 and command_help_pos < len(data)
                and (info & (1 << 29)) == 0):
            try:
                command_help = _read_pretty_print(data, command_help_pos, command_name)
            except PrettyPrintUnimplemented:
                pass

        result.append({
            'name': command_name,
            'syntax': command_syntax,
            'help': command_help,
        })

        pos += 16
    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def decode_module(data, module_hash=None):
    """Parse a RISC OS relocatable module binary.

    Args:
        data: Raw bytes of the module file.
        module_hash: Pre-computed SHA-256 hex digest (computed if not provided).

    Returns:
        Dict with keys: title_string, help_string, help_title, version, date,
        other, swi_chunk, swi_names, commands, module_flags, hash.

        When the help string is missing or cannot be parsed, the dict is still
        returned with help_title/version/date set to None.

    Raises:
        ModuleParseError: If the data is too small or structurally invalid.
    """
    if len(data) < 52:
        raise ModuleParseError("Data too small to be a RISC OS module (< 52 bytes)")

    # Compute hash if not provided
    if module_hash is None:
        module_hash = hashlib.sha256(data).hexdigest()

    # Read module header (13 words = 52 bytes)
    (start, init, final, service, title_string_off, help_string_off, command_table,
     swi_chunk, swi_handler, swi_decoding_table, swi_decoding_code,
     messages_file, module_flags_off) = struct.unpack("< IIIIIII IIII II", data[:52])

    # Validate title string offset
    if title_string_off >= len(data):
        raise ModuleParseError(
            f"Title string offset 0x{title_string_off:x} beyond module size 0x{len(data):x}"
        )

    (title_string, _) = _read_string(data, title_string_off)

    # Handle help string — check quirks first
    help_string = None
    help_title = None
    version = None
    date = None
    other = None

    if module_hash in QUIRKS:
        help_string = QUIRKS[module_hash]
    elif help_string_off != 0 and help_string_off < len(data):
        (help_string, _) = _read_string(data, help_string_off)

    # Parse help string into components — handle known special cases by hash
    if help_string is None:
        # No help string available — title_string is still valid
        pass
    elif module_hash == "c440c6d31d8ab3600c28ad4de2266719167e6b060f528bfd2462ee8ae0f7b497":
        # ARMBasicEditor in Arthur 0.30 — no version/date
        (help_title, version, date, other) = (help_string, None, None, None)
    elif module_hash == "cda6a12eb08f63aef1789a907ca626c6ef4b2a008af94e7681ac1df422b19dff":
        # STBPrint 0.07 (28  19 — truncated in RISC OS 3.61 (OM STB2)
        (help_title, version, date, other) = ("STBPrint", "0.07", None, None)
    elif module_hash == "d5354959eeacb20a857f307f92d224c3bdb4bf69162f5cc22c160d8db3a53eee":
        # IIC 0.17 (A9homeDummy) in RISC OS 4.42 (A9home)
        (help_title, version, date, other) = ("IIC", "0.17", None, "A9homeDummy")
    elif module_hash == "d7d82482bac7de4892eeb6db7d1c639b3d9e23eced9d9620ee43fff30a99f480":
        # Window Manager 4.100 (22 Feb 2006) — non-standard version format in RISC OS 5.11
        (help_title, version, date, other) = ("Window Manager", "4.100", "2006-02-22", None)
    else:
        try:
            (help_title, version, date_str, other) = _parse_help_string(help_string)
            if other == "":
                other = None
            date = _parse_module_date_string(date_str)
        except HelpParseError:
            # Help string present but doesn't match standard format —
            # use the raw help string as the title and leave version/date as None
            help_title = help_string.split('\t')[0].strip() if '\t' in help_string else help_string.strip()
            version = None
            date = None
            other = None

    # SWI chunk validation
    swi_chunk_garbage = ((swi_chunk & 0xff00003f) != 0) or (swi_handler >= len(data))
    if (swi_chunk == 0) or swi_chunk_garbage:
        swi_chunk = None

    # SWI names
    swi_names = None
    if swi_chunk is not None and swi_decoding_table != 0 and swi_decoding_table < len(data):
        try:
            swi_names = _read_swis(data, swi_decoding_table)
        except (IndexError, struct.error):
            pass

    # Star commands
    commands = None
    if (title_string != "UtilityModule" and command_table != 0
            and command_table < len(data)):
        try:
            commands = _decode_command_table(data, command_table)
        except (IndexError, struct.error):
            pass

    # Module flags
    if (not swi_chunk_garbage and messages_file < len(data)
            and module_flags_off < len(data)
            and (module_flags_off & 3) == 0 and module_flags_off != 0):
        try:
            (module_flags,) = struct.unpack("< I", data[module_flags_off:module_flags_off + 4])
        except struct.error:
            module_flags = None
    else:
        module_flags = None

    return {
        'title_string': title_string,
        'help_string': help_string,
        'help_title': help_title,
        'version': version,
        'date': date,
        'other': other,
        'swi_chunk': swi_chunk,
        'swi_names': swi_names,
        'commands': commands,
        'module_flags': module_flags,
        'hash': module_hash,
    }

# vim: ts=4 sw=4 et
