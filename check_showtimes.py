"""Poll cinema sites for showtimes on a target date and notify via Telegram.

Watches are declared in `watches.json`; each one names a site adapter, a movie, and a
target date (either a fixed calendar date or "the next occurrence of a weekday"). Every
run checks all of them independently and sends one Telegram message per watch that has
just gone live.

Two sites are supported, and both share the same core signal: **no showtimes parsed means
"not published yet"**, because each site only publishes a rolling window of upcoming dates.

  scene — Scene Cinemas District 5. Showtimes come from an AJAX fragment:
          <movie-details-url>?business_day=DD-MM-YYYY&ajax=1
          A date beyond the published window returns an empty body.

  vox   — VOX Cinemas Egypt. Showtimes come from the full page:
          /showtimes?c=<cinema>&m=<movie>&d=YYYYMMDD
          A date beyond the published window still returns a full page, but with a
          "No showtimes could be found" notice and no showtime elements.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Optional
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from curl_cffi import requests as cffi_requests

SCENE_MOVIE_BASE = "https://district5.scenecinemas.com/movie-details/{movie}.html"
SCENE_CINEMA_NAME = "Scene Cinemas — District 5"
SCENE_SHOWTIMES_URL = "{base}?business_day={date}&ajax=1"
VOX_SHOWTIMES_URL = "https://egy.voxcinemas.com/showtimes?c={cinema}&m={movie}&d={date}"
TELEGRAM_URL = "https://api.telegram.org/bot{token}/sendMessage"

_COMMON_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}
# Scene's fragment is fetched as an XHR; VOX's page is fetched as a top-level navigation.
SCENE_HEADERS = {
    **_COMMON_HEADERS,
    "X-Requested-With": "XMLHttpRequest",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}
VOX_HEADERS = {
    **_COMMON_HEADERS,
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
}

DEFAULT_STATE_PATH = Path(__file__).parent / "state.json"
DEFAULT_WATCHES_PATH = Path(__file__).parent / "watches.json"
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

log = logging.getLogger("showtime_notifier")


@dataclass(frozen=True)
class Config:
    """Telegram credentials and run-wide defaults, from environment variables."""
    telegram_token: str
    telegram_chat_id: str
    default_timezone: str
    watches_path: Path


NOTIFY_MODES = ("published", "bookable")


@dataclass(frozen=True)
class Watch:
    """One thing we're watching: a movie at a cinema on a target date.

    `notify_on` picks the trigger: "published" fires as soon as any showtime appears for
    the target date, "bookable" holds off until at least one of them can actually be
    booked. They differ because both sites list showtimes before opening sales.
    """
    id: str
    site: str
    movie_slug: str
    timezone: str
    cinema_slug: str = ""
    base_url: str = ""
    target_weekday: str = ""
    target_date: Optional[date] = None
    notify_on: str = "published"


@dataclass(frozen=True)
class Showtime:
    """A single showtime entry parsed from the page."""
    time: str
    href: str
    soldout: bool = False


@dataclass(frozen=True)
class Site:
    """Per-site adapter: how to build the URL, fetch it, and read showtimes out of it."""
    build_url: Callable[[Watch, date], str]
    parse: Callable[[str], "dict[str, list[Showtime]]"]
    display_names: Callable[[str, Watch], "tuple[str, str]"]
    headers: dict
    # Consulted only when nothing parsed: is an empty result the site's normal
    # "nothing scheduled" response, or a sign the markup changed under us?
    empty_is_expected: Callable[[str], bool]
    # What a non-bookable time means on this site — Scene flags genuine sell-outs,
    # VOX uses one "unavailable" state for both sold out and not-yet-on-sale.
    soldout_label: str
    # Cinema name to use when the watch has no cinema_slug (single-venue sites).
    default_cinema: str = ""


def load_config() -> Config:
    """Read and validate required env vars; raise on missing required ones."""
    required = {
        "TELEGRAM_BOT_TOKEN": os.environ.get("TELEGRAM_BOT_TOKEN"),
        "TELEGRAM_CHAT_ID": os.environ.get("TELEGRAM_CHAT_ID"),
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        raise RuntimeError(f"Missing required env vars: {', '.join(missing)}")

    watches_path = os.environ.get("WATCHES_FILE")
    return Config(
        telegram_token=required["TELEGRAM_BOT_TOKEN"],
        telegram_chat_id=required["TELEGRAM_CHAT_ID"],
        default_timezone=(os.environ.get("TIMEZONE") or "Africa/Cairo").strip(),
        watches_path=Path(watches_path) if watches_path else DEFAULT_WATCHES_PATH,
    )


def load_watches(path: Path, default_timezone: str = "Africa/Cairo") -> "list[Watch]":
    """Parse and validate watches.json into Watch objects.

    Each entry needs an `id`, a `site` we have an adapter for, a `movie_slug`, and a
    `target` of either {"weekday": "friday"} or {"date": "YYYY-MM-DD"} — not both.
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    entries = raw.get("watches") if isinstance(raw, dict) else raw
    if not isinstance(entries, list) or not entries:
        raise RuntimeError(f"{path}: expected a non-empty 'watches' list")

    watches: list[Watch] = []
    seen: set[str] = set()
    for i, e in enumerate(entries):
        where = f"{path} watch #{i}"
        wid = str(e.get("id") or "").strip()
        if not wid:
            raise RuntimeError(f"{where}: missing 'id'")
        if wid in seen:
            raise RuntimeError(f"{where}: duplicate id {wid!r}")
        seen.add(wid)

        site = str(e.get("site") or "").strip().lower()
        if site not in SITES:
            raise RuntimeError(f"{where}: site must be one of {sorted(SITES)}, got {site!r}")

        movie_slug = str(e.get("movie_slug") or "").strip()
        if not movie_slug:
            raise RuntimeError(f"{where}: missing 'movie_slug'")

        cinema_slug = str(e.get("cinema_slug") or "").strip()
        if site == "vox" and not cinema_slug:
            raise RuntimeError(f"{where}: site 'vox' requires 'cinema_slug'")

        base_url = str(e.get("base_url") or "").strip()
        if site == "scene":
            base_url = base_url or SCENE_MOVIE_BASE
            if "{movie}" in base_url:
                base_url = base_url.format(movie=movie_slug)

        target = e.get("target") or {}
        weekday = str(target.get("weekday") or "").strip().lower()
        fixed_raw = str(target.get("date") or "").strip()
        if bool(weekday) == bool(fixed_raw):
            raise RuntimeError(f"{where}: target needs exactly one of 'weekday' or 'date'")
        if weekday and weekday not in WEEKDAYS:
            raise RuntimeError(f"{where}: weekday must be one of {WEEKDAYS}, got {weekday!r}")
        fixed = None
        if fixed_raw:
            try:
                fixed = date.fromisoformat(fixed_raw)
            except ValueError as exc:
                raise RuntimeError(f"{where}: date must be YYYY-MM-DD, got {fixed_raw!r}") from exc

        notify_on = str(e.get("notify_on") or "published").strip().lower()
        if notify_on not in NOTIFY_MODES:
            raise RuntimeError(
                f"{where}: notify_on must be one of {list(NOTIFY_MODES)}, got {notify_on!r}")

        tz = str(e.get("timezone") or "").strip() or default_timezone
        try:
            ZoneInfo(tz)
        except Exception as exc:  # noqa: BLE001 — zoneinfo raises its own error types
            raise RuntimeError(f"{where}: unknown timezone {tz!r}") from exc

        watches.append(Watch(
            id=wid,
            site=site,
            movie_slug=movie_slug,
            timezone=tz,
            cinema_slug=cinema_slug,
            base_url=base_url,
            target_weekday=weekday,
            target_date=fixed,
            notify_on=notify_on,
        ))
    return watches


def next_target_date(weekday: str, tz: str, now: Optional[datetime] = None) -> date:
    """Return the next occurrence of `weekday` in `tz`; today if today already matches."""
    if now is None:
        now = datetime.now(ZoneInfo(tz))
    target_idx = WEEKDAYS.index(weekday.lower())
    today_idx = now.weekday()
    delta = (target_idx - today_idx) % 7
    return (now.date() + timedelta(days=delta))


def resolve_target_date(watch: Watch, now: Optional[datetime] = None) -> date:
    """The calendar date this watch is currently asking about."""
    if watch.target_date is not None:
        return watch.target_date
    return next_target_date(watch.target_weekday, watch.timezone, now=now)


def today_in(tz: str, now: Optional[datetime] = None) -> date:
    return (now or datetime.now(ZoneInfo(tz))).date()


def fetch_page(url: str, headers: Optional[dict] = None,
               attempts: int = 3, timeout: int = 20) -> str:
    """GET `url` impersonating Chrome's TLS fingerprint; retry on network errors and 5xx.

    Both sites' WAFs reject plain `requests`/`urllib3` TLS handshakes, so we use curl_cffi.
    """
    last_err: Optional[Exception] = None
    for i in range(attempts):
        try:
            r = cffi_requests.get(
                url, headers=headers or SCENE_HEADERS, timeout=timeout, impersonate="chrome124"
            )
            if 500 <= r.status_code < 600:
                raise RuntimeError(f"server error {r.status_code}")
            if r.status_code >= 400:
                raise RuntimeError(f"HTTP {r.status_code}")
            return r.text
        except Exception as e:  # noqa: BLE001 — curl_cffi raises its own errors
            last_err = e
            backoff = 2 ** i
            log.warning("fetch attempt %d/%d failed: %s (sleeping %ds)", i + 1, attempts, e, backoff)
            if i < attempts - 1:
                time.sleep(backoff)
    raise RuntimeError(f"failed to fetch {url} after {attempts} attempts: {last_err}")


# --------------------------------------------------------------------------- scene


def scene_url(watch: Watch, target: date) -> str:
    return SCENE_SHOWTIMES_URL.format(base=watch.base_url, date=target.strftime("%d-%m-%Y"))


def parse_showtimes_scene(html: str) -> "dict[str, list[Showtime]]":
    """Parse the Scene Cinemas AJAX fragment, returning {screen_type: [Showtime, ...]}.

    Showtimes are grouped by an experience label span (e.g. `IMAX`, `Premiere`,
    `Standard & Deluxe`) whose class starts with `ex_`. Sold-out entries carry the
    `showtime_soldout` class and a `javascript:void(0)` href. Returns an empty dict when
    the fragment is empty (no showtimes scheduled yet for that date).
    """
    text = html.strip()
    if not text:
        return {}

    soup = BeautifulSoup(html, "html.parser")
    groups: dict[str, list[Showtime]] = {}

    # Each experience label is a <span class="ex_imax|ex_vip|ex_stand|...">. The content
    # wrapper divs are `ex_*_content`, so exclude those to keep only label spans.
    label_spans = soup.find_all(
        "span", class_=lambda c: bool(c) and any(
            cls.startswith("ex_") and not cls.endswith("_content") for cls in c.split()
        )
    )
    for span in label_spans:
        label = span.get_text(strip=True)
        if not label:
            continue
        container = span.find_parent("div")
        if container is None:
            continue
        for a in container.select("ul li a"):
            time_text = a.get_text(strip=True)
            if not time_text:
                continue
            classes = a.get("class") or []
            href = (a.get("href") or "").strip()
            soldout = "showtime_soldout" in classes or href.lower().startswith("javascript:")
            groups.setdefault(label, []).append(
                Showtime(time=time_text, href=href, soldout=soldout)
            )
    return groups


def scene_display_names(html: str, watch: Watch) -> "tuple[str, str]":
    """Return (movie_title, cinema_name); cinema comes from the fragment's branch label."""
    movie = title_from_slug(watch.movie_slug)

    cinema = SCENE_CINEMA_NAME
    soup = BeautifulSoup(html, "html.parser")
    branch = soup.find(class_="branch")
    if branch and branch.get_text(strip=True):
        cinema = re.sub(r"\s+", " ", branch.get_text(strip=True)).strip()

    return movie, cinema


def scene_empty_is_expected(html: str) -> bool:
    """An empty fragment IS Scene's 'nothing scheduled' response, so nothing looks wrong."""
    return True


# ----------------------------------------------------------------------------- vox


def vox_url(watch: Watch, target: date) -> str:
    return VOX_SHOWTIMES_URL.format(
        cinema=watch.cinema_slug, movie=watch.movie_slug, date=target.strftime("%Y%m%d")
    )


def parse_showtimes_vox(html: str) -> "dict[str, list[Showtime]]":
    """Parse the VOX showtimes page, returning {screen_type: [Showtime, ...]}.

    Bookable times are `<a class="action showtime" href="/booking/...">`; times that exist
    but can't be booked (sold out or not yet on sale) are `<span class="action showtime
    unavailable">` with no href. Both count as "showtimes are published" — only the
    booking link differs — so we keep both and flag the spans as sold out.

    Screen type comes from the `<strong>` label of the enclosing `<li>` in `ol.showtimes`.
    """
    soup = BeautifulSoup(html, "html.parser")
    nodes = soup.select("a.action.showtime, span.action.showtime")
    if not nodes:
        return {}

    groups: dict[str, list[Showtime]] = {}
    for node in nodes:
        text = node.get_text(strip=True)
        if not text:
            continue
        classes = node.get("class") or []
        href = (node.get("href") or "").strip()
        soldout = node.name != "a" or "unavailable" in classes or not href
        groups.setdefault(_vox_group_label(node), []).append(
            Showtime(time=text, href=href, soldout=soldout)
        )
    return groups


def _vox_group_label(node) -> str:
    """Walk up from a showtime node to the nearest <li> carrying a <strong> screen label."""
    for parent in node.parents:
        if parent.name == "li":
            strong = parent.find("strong", recursive=False)
            if strong and strong.get_text(strip=True):
                return strong.get_text(strip=True)
    return "Showtimes"


def vox_display_names(html: str, watch: Watch) -> "tuple[str, str]":
    """Movie from the showtimes article's <h2>, cinema from the `.highlight` heading.

    The lookup is scoped to `article.movie-compare` on purpose: the page footer also has
    <h2> elements ("Stay in touch"), which a bare `find("h2")` would happily return.
    """
    soup = BeautifulSoup(html, "html.parser")

    movie = title_from_slug(watch.movie_slug)
    heading = soup.select_one("article.movie-compare h2") or soup.select_one("section.showtimes h2")
    if heading and heading.get_text(strip=True):
        movie = re.sub(r"\s+", " ", heading.get_text(strip=True)).strip()

    cinema = title_from_slug(watch.cinema_slug)
    branch = soup.select_one(".dates h3.highlight") or soup.select_one("h3.highlight")
    if branch and branch.get_text(strip=True):
        cinema = re.sub(r"\s+", " ", branch.get_text(strip=True)).strip()

    return movie, cinema


def vox_empty_is_expected(html: str) -> bool:
    """VOX states 'no showtimes' explicitly; its absence means the page structure moved."""
    return "no showtimes could be found" in html.lower()


SITES: "dict[str, Site]" = {
    "scene": Site(
        build_url=scene_url,
        parse=parse_showtimes_scene,
        display_names=scene_display_names,
        headers=SCENE_HEADERS,
        empty_is_expected=scene_empty_is_expected,
        soldout_label="sold out",
        default_cinema=SCENE_CINEMA_NAME,
    ),
    "vox": Site(
        build_url=vox_url,
        parse=parse_showtimes_vox,
        display_names=vox_display_names,
        headers=VOX_HEADERS,
        empty_is_expected=vox_empty_is_expected,
        # VOX shows one "unavailable" state for both sold out and not-yet-on-sale, and
        # times routinely flip from unavailable to bookable — so don't claim "sold out".
        soldout_label="not bookable",
    ),
}


# --------------------------------------------------------------------------- output


def title_from_slug(slug: str) -> str:
    return " ".join(w.capitalize() for w in slug.replace("_", "-").split("-"))


def should_notify(notify_on: str, total: int, bookable: int) -> bool:
    """Has this watch's trigger fired? Split out from `check_watch` so it's directly testable."""
    if total == 0:
        return False
    if notify_on == "bookable":
        return bookable > 0
    return True


def format_message(movie: str, cinema: str, target_date: date,
                   groups: "dict[str, list[Showtime]]",
                   soldout_label: str = "sold out",
                   headline: str = "showtimes are live!") -> str:
    """Format the HTML-mode Telegram message body."""
    date_str = f"{target_date:%A, %B} {target_date.day}, {target_date:%Y}"
    lines = [
        f"🎬 <b>{movie}</b> {headline}",
        f"📍 {cinema}",
        f"📅 {date_str}",
    ]
    for screen_type, times in groups.items():
        lines.append("")
        lines.append(f"<b>{screen_type}</b>")
        for st in times:
            if st.soldout:
                lines.append(f"• <s>{st.time}</s> ({soldout_label})")
            elif st.href:
                lines.append(f"• <a href=\"{st.href}\">{st.time}</a>")
            else:
                lines.append(f"• {st.time}")
    return "\n".join(lines)


def send_telegram(token: str, chat_id: str, text: str) -> None:
    """POST a message to the Telegram Bot API; raise on failure."""
    r = requests.post(
        TELEGRAM_URL.format(token=token),
        json={
            "chat_id": chat_id,
            "parse_mode": "HTML",
            "text": text,
            "disable_web_page_preview": True,
        },
        timeout=20,
    )
    if not r.ok:
        raise RuntimeError(f"Telegram API error {r.status_code}: {r.text}")
    body = r.json()
    if not body.get("ok"):
        raise RuntimeError(f"Telegram API returned not-ok: {body}")


# ---------------------------------------------------------------------------- state


def load_state(path: Path, watches: "Optional[list[Watch]]" = None) -> dict:
    """Read state.json as {"notified_for": {watch_id: "YYYYMMDD"}}.

    Older runs stored a single bare string for the one watch that existed then; that value
    is migrated onto the first watch so an already-sent notification isn't repeated.
    """
    if not path.exists():
        return {"notified_for": {}}

    state = json.loads(path.read_text(encoding="utf-8"))
    notified = state.get("notified_for")
    if isinstance(notified, dict):
        return {"notified_for": notified}
    if isinstance(notified, str) and watches:
        return {"notified_for": {watches[0].id: notified}}
    return {"notified_for": {}}


def save_state(path: Path, state: dict) -> None:
    """Persist state.json with a trailing newline."""
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


# ------------------------------------------------------------------------------ run


def check_watch(watch: Watch, cfg: Config, state: dict) -> bool:
    """Check one watch; send a notification if it just went live. Returns True if state changed."""
    site = SITES[watch.site]
    target = resolve_target_date(watch)
    dedupe_key = target.strftime("%Y%m%d")

    if watch.target_date is not None and target < today_in(watch.timezone):
        log.info("[%s] target date %s has passed — skipping", watch.id, target.isoformat())
        return False

    url = site.build_url(watch, target)
    log.info("[%s] checking %s on %s", watch.id, watch.movie_slug, target.isoformat())
    log.info("[%s] URL: %s", watch.id, url)

    html = fetch_page(url, headers=site.headers)
    groups = site.parse(html)
    total = sum(len(v) for v in groups.values())

    if total == 0:
        if not site.empty_is_expected(html):
            log.warning("[%s] no showtimes AND no expected empty-state marker — "
                        "the page structure may have changed", watch.id)
        log.info("[%s] No showtimes yet for %s", watch.id, target.isoformat())
        return False

    bookable = sum(1 for v in groups.values() for s in v if not s.soldout)
    log.info("[%s] Found %d showtimes (%d bookable) across %d screen types: %s",
             watch.id, total, bookable, len(groups), ", ".join(groups.keys()))

    if not should_notify(watch.notify_on, total, bookable):
        # notify_on="bookable": the date is published but sales haven't opened. Stay
        # silent and keep checking — state is deliberately not written here.
        log.info("[%s] %d showtimes listed but none bookable yet — waiting", watch.id, total)
        return False

    if state["notified_for"].get(watch.id) == dedupe_key:
        log.info("[%s] Already notified for %s", watch.id, dedupe_key)
        return False

    movie, cinema = site.display_names(html, watch)
    headline = "is now bookable!" if watch.notify_on == "bookable" else "showtimes are live!"
    text = format_message(movie, cinema, target, groups,
                          soldout_label=site.soldout_label, headline=headline)
    send_telegram(cfg.telegram_token, cfg.telegram_chat_id, text)
    log.info("[%s] Telegram notification sent for %s", watch.id, dedupe_key)

    state["notified_for"][watch.id] = dedupe_key
    return True


def main() -> int:
    """Check every configured watch, isolating failures so one bad site can't mask the rest."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    cfg = load_config()
    watches = load_watches(cfg.watches_path, cfg.default_timezone)
    state = load_state(DEFAULT_STATE_PATH, watches)
    log.info("loaded %d watch(es): %s", len(watches), ", ".join(w.id for w in watches))

    changed = False
    failures: list[str] = []
    for watch in watches:
        try:
            changed |= check_watch(watch, cfg, state)
        except Exception as e:  # noqa: BLE001 — one watch must not sink the others
            log.error("[%s] failed: %s", watch.id, e)
            failures.append(watch.id)

    if changed:
        save_state(DEFAULT_STATE_PATH, state)

    if failures:
        log.error("%d of %d watches failed: %s", len(failures), len(watches), ", ".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001
        log.error("fatal: %s", e)
        sys.exit(1)
