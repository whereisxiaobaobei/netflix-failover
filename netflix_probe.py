"""Conservative, account-free Netflix catalog evidence classification.

This module performs no I/O. ``unlocked`` describes public title-page evidence,
not an authenticated playback or DRM-license check.
"""

from __future__ import annotations

import re
import unicodedata
from html.parser import HTMLParser
from typing import Iterable, Literal
from urllib.parse import urlsplit

State = Literal["unlocked", "restricted", "unknown"]
STATES = frozenset(("unlocked", "restricted", "unknown"))
CONTROL_TITLE = ("80057281", "Stranger Things")
LICENSED_TITLES = (
    ("81280792", "LEGO Ninjago"),
    ("70143836", "Breaking Bad"),
)


def _normalize(value: str) -> str:
    value = unicodedata.normalize("NFKC", value)
    value = value.translate(str.maketrans({"’": "'", "‘": "'", "\u00a0": " "}))
    return " ".join(value.casefold().split())


def _https_url(value: str):
    try:
        parts = urlsplit(value)
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.port not in (None, 443)
        ):
            return None
        return parts
    except (ValueError, TypeError):
        return None


def _title_url_matches(value: str, title_id: str) -> bool:
    parts = _https_url(value)
    if parts is None or parts.hostname not in ("netflix.com", "www.netflix.com"):
        return False
    return re.fullmatch(
        r"/(?:[a-zA-Z]{2,3}(?:-[a-zA-Z]{2,4})?/)?title/" + re.escape(title_id) + r"/?",
        parts.path,
    ) is not None


def _media_url_matches(value: str) -> bool:
    parts = _https_url(value)
    if parts is None:
        return False
    host = parts.hostname
    domains = ("nflxso.net", "nflxvideo.net", "netflix.com")
    return bool(
        any(host == domain or host.endswith("." + domain) for domain in domains)
        and re.search(r"\.(?:mp4|m4v|webm|m3u8|mpd)$", parts.path, re.IGNORECASE)
    )


def _title_label_matches(label: str, expected_title: str) -> bool:
    return re.fullmatch(
        r"(?:watch )?"
        + re.escape(_normalize(expected_title))
        + r"\s*\|\s*netflix(?: official site)?",
        _normalize(label),
    ) is not None


class _TitlePage(HTMLParser):
    """Read actual HTML elements; JavaScript strings are never meta evidence."""

    _HIDDEN = frozenset(("script", "style", "template", "noscript"))

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, list[str]] = {}
        self.titles: list[str] = []
        self.text: list[str] = []
        self._title_parts: list[str] | None = None
        self._hidden: list[str] = []
        self._in_head = False
        self.malformed = False
        self.body_seen = False
        self.body_closed = False
        self.html_closed = False

    def handle_starttag(self, tag, attrs):
        if self._hidden:
            if tag in self._HIDDEN:
                self._hidden.append(tag)
            return
        if tag in self._HIDDEN:
            self._hidden.append(tag)
            return
        if tag == "head":
            self._in_head = True
        if tag == "body":
            self.body_seen = True
        if tag == "title" and self._in_head:
            if self._title_parts is not None:
                self.malformed = True
            self._title_parts = []
        if tag == "meta" and self._in_head:
            values = dict(attrs)
            if len(values) != len(attrs):
                self.malformed = True
                return
            prop = (values.get("property") or "").casefold()
            if prop in ("og:title", "og:url", "og:video"):
                self.meta.setdefault(prop, []).append((values.get("content") or "").strip())

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if self._hidden:
            if tag == self._hidden[-1]:
                self._hidden.pop()
            return
        if tag == "title" and self._title_parts is not None:
            self.titles.append("".join(self._title_parts))
            self._title_parts = None
        if tag == "head":
            self._in_head = False
        if tag == "body":
            self.body_closed = True
        if tag == "html":
            self.html_closed = True

    def handle_data(self, data):
        if self._hidden:
            return
        if self._title_parts is not None:
            self._title_parts.append(data)
        elif self.body_seen and not self.body_closed:
            self.text.append(data)


_UNAVAILABLE = re.compile(
    r"(?:this title (?:currently )?(?:isn't|is not) available(?: to watch)? "
    r"in your (?:current )?(?:country|region)"
    r"|(?:this title (?:is |is currently )?)?unavailable in your (?:country|region))"
)
_CHALLENGE = re.compile(
    r"verify (?:that )?you are human|checking your browser|"
    r"unusual traffic|access denied|complete the captcha"
)


def classify_page(
    body: str, status: int, url: str, title_id: str, expected_title: str
) -> State:
    """Classify one complete English public Netflix title-page response.

    The caller must also treat transport errors as unknown, even if a partial
    response happened to carry HTTP 200. Both final and Open Graph URLs must bind
    the response to the requested title. Missing or conflicting evidence fails
    conservatively to unknown.
    """
    if (
        status != 200
        or not isinstance(body, str)
        or not body.strip()
        or not isinstance(title_id, str)
        or not title_id.isascii()
        or not title_id.isdigit()
        or not expected_title.strip()
        or not _title_url_matches(url, title_id)
    ):
        return "unknown"
    page = _TitlePage()
    try:
        page.feed(body)
        page.close()
    except (ValueError, AssertionError):
        return "unknown"
    if (
        page.malformed
        or page._hidden
        or page._in_head
        or page._title_parts is not None
        or not (page.body_seen and page.body_closed and page.html_closed)
        or len(page.titles) != 1
        or not _title_label_matches(page.titles[0], expected_title)
    ):
        return "unknown"
    for name in ("og:title", "og:url", "og:video"):
        if len(set(page.meta.get(name, []))) > 1:
            return "unknown"
    og_title = page.meta.get("og:title", [""])[0]
    og_url = page.meta.get("og:url", [""])[0]
    if not (
        _title_label_matches(og_title, expected_title)
        and _title_url_matches(og_url, title_id)
    ):
        return "unknown"
    visible = _normalize(" ".join(page.text))
    if _CHALLENGE.search(visible):
        return "unknown"
    has_oh_no = re.search(r"\boh no\s*!", visible) is not None
    has_unavailable = _UNAVAILABLE.search(visible) is not None
    video_values = page.meta.get("og:video", [])
    has_video = bool(video_values and _media_url_matches(video_values[0]))
    if video_values and not has_video:
        return "unknown"
    if has_video and (has_oh_no or has_unavailable):
        return "unknown"
    if has_video:
        return "unlocked"
    if has_oh_no and has_unavailable:
        return "restricted"
    return "unknown"


def aggregate_title_results(control_state: State, licensed_states: Iterable[State]) -> State:
    """Require a healthy global control and evidence from two licensed titles."""
    licensed = tuple(licensed_states)
    if control_state != "unlocked" or len(licensed) != 2 or any(s not in STATES for s in licensed):
        return "unknown"
    if "unlocked" in licensed:
        return "unlocked"
    if licensed == ("restricted", "restricted"):
        return "restricted"
    return "unknown"


def group_state(member_states: Iterable[State]) -> State:
    """A prefix-selected group needs consistent evidence from all its members."""
    members = tuple(member_states)
    if members and all(s == "unlocked" for s in members):
        return "unlocked"
    if members and all(s == "restricted" for s in members):
        return "restricted"
    return "unknown"


def decision(primary: State, backup: State) -> Literal["switch", "keep"]:
    return "switch" if primary == "restricted" and backup == "unlocked" else "keep"
