"""Language codes: compare `en`, `eng` and `en-US` as the same language.

Tracks carry an ISO 639-2 code (`language`) and usually a BCP 47 tag (`language_ietf`).
Users type whichever they remember. Everything is reduced to one key: the ISO 639-1 code
when the language has one, otherwise the ISO 639-2/3 code.
"""

from __future__ import annotations

import re

# ISO 639-2 has two codes for some languages; MKVToolNix lists only the bibliographic one.
_TERMINOLOGIC = {
    "fra": "fre", "deu": "ger", "zho": "chi", "nld": "dut", "ell": "gre", "fas": "per",
    "ces": "cze", "slk": "slo", "ron": "rum", "sqi": "alb", "hye": "arm", "eus": "baq",
    "mya": "bur", "kat": "geo", "isl": "ice", "mkd": "mac", "mri": "mao", "msa": "may",
    "bod": "tib", "cym": "wel",
}

UNDETERMINED = {"und", "mul", "zxx", "mis", ""}


class Languages:
    def __init__(self, to_alpha2: dict[str, str], names: dict[str, str]):
        self._to_alpha2 = to_alpha2
        self._names = names

    @classmethod
    def from_mkvmerge_list(cls, text: str) -> Languages:
        """Parse `mkvmerge --list-languages` (name | 639-3 | 639-2 | 639-1)."""
        to_alpha2: dict[str, str] = {}
        names: dict[str, str] = {}
        for line in text.splitlines():
            cols = [c.strip() for c in line.split("|")]
            if len(cols) != 4 or not re.fullmatch(r"[a-z]{3}", cols[1]):
                continue
            name, iso3, iso2b, iso1 = cols
            key = iso1 or iso2b or iso3
            for code in (iso3, iso2b, iso1):
                if code:
                    to_alpha2.setdefault(code, key)
            names.setdefault(key, name)
        for t, b in _TERMINOLOGIC.items():
            if b in to_alpha2:
                to_alpha2.setdefault(t, to_alpha2[b])
        return cls(to_alpha2, names)

    def key(self, code: str | None) -> str:
        """Normalise a 639-1/639-2/639-3 code or a BCP 47 tag to one comparable key."""
        if not code:
            return "und"
        primary = code.strip().replace("_", "-").split("-")[0].lower()
        if primary in UNDETERMINED:
            return "und"
        return self._to_alpha2.get(primary, primary)

    def known(self, code: str) -> bool:
        return code.strip().split("-")[0].lower() in self._to_alpha2

    def name(self, key: str) -> str:
        return self._names.get(key, key)

    def __len__(self) -> int:
        return len(self._to_alpha2)
