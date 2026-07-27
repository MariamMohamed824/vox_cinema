"""One-shot helper: send a sample Telegram message per configured watch.

Samples are derived from `watches.json`, so each message uses that watch's real site
wording ("sold out" vs "not bookable") and its real `notify_on` headline — the point is to
see what an actual alert will look like, not just to prove the bot token works.

Every message is prefixed with a TEST banner, because these land in a shared group.

Usage (PowerShell):
    $env:TELEGRAM_BOT_TOKEN = "..."
    $env:TELEGRAM_CHAT_ID   = "..."
    python send_test_message.py                  # one message per watch
    python send_test_message.py odyssey-d5-friday  # only the named watch(es)

Exits 0 on success, 1 on any failure.
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import date, timedelta

from check_showtimes import (
    SITES,
    DEFAULT_WATCHES_PATH,
    Showtime,
    Watch,
    title_from_slug,
    format_message,
    load_watches,
    resolve_target_date,
    send_telegram,
)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

BANNER = "🧪 <b>TEST</b> — verifying the notifier. Not a real alert, please ignore."

log = logging.getLogger("send_test_message")


def sample_groups(watch: Watch) -> "dict[str, list[Showtime]]":
    """Fake showtimes shaped like the real thing: some bookable, some not."""
    if watch.site == "vox":
        booking = "https://egy.voxcinemas.com/booking/0047-sample"
        return {
            "Standard": [
                Showtime(time="1:45pm", href=f"{booking}1"),
                Showtime(time="5:00pm", href=f"{booking}2"),
            ],
            "GOLD": [
                Showtime(time="8:00pm", href="", soldout=True),
            ],
        }
    booking = "https://district5.scenecinemas.com/showtime-sample"
    return {
        "IMAX": [
            Showtime(time="04:00 PM", href=f"{booking}1"),
            Showtime(time="08:00 PM", href="", soldout=True),
        ],
        "Premiere": [
            Showtime(time="09:30 PM", href=f"{booking}2"),
        ],
    }


def sample_date(watch: Watch) -> date:
    """The watch's real target date, or a near-future stand-in if it's already passed."""
    target = resolve_target_date(watch)
    return target if target >= date.today() else date.today() + timedelta(days=3)


def main(argv: "list[str]") -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID env vars first.", file=sys.stderr)
        return 1

    watches = load_watches(DEFAULT_WATCHES_PATH)
    wanted = set(argv)
    if wanted:
        unknown = wanted - {w.id for w in watches}
        if unknown:
            print(f"Unknown watch id(s): {', '.join(sorted(unknown))}", file=sys.stderr)
            return 1
        watches = [w for w in watches if w.id in wanted]

    for watch in watches:
        site = SITES[watch.site]
        headline = "is now bookable!" if watch.notify_on == "bookable" else "showtimes are live!"
        text = format_message(
            movie=title_from_slug(watch.movie_slug),
            cinema=(title_from_slug(watch.cinema_slug) if watch.cinema_slug
                    else site.default_cinema or watch.site.title()),
            target_date=sample_date(watch),
            groups=sample_groups(watch),
            soldout_label=site.soldout_label,
            headline=headline,
        )
        log.info("[%s] sending sample (notify_on=%s) to chat %s", watch.id, watch.notify_on, chat_id)
        send_telegram(token, chat_id, BANNER + "\n\n" + text)

    log.info("%d test message(s) sent — check Telegram", len(watches))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception as e:  # noqa: BLE001
        logging.error("failed: %s", e)
        sys.exit(1)
