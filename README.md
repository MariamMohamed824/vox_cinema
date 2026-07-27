# Cinema Showtime Notifier

Polls cinema sites every 10 minutes via GitHub Actions and sends you a Telegram message the moment showtimes go live for a movie on a date you care about. One notification per watch per target date — no spam.

Two sites are supported:

- **`scene`** — [Scene Cinemas District 5](https://district5.scenecinemas.com/)
- **`vox`** — [VOX Cinemas Egypt](https://egy.voxcinemas.com/)

## What's being watched

Watches are declared in [`watches.json`](watches.json). The current set:

| id | site | movie | cinema | target date | notifies when |
| --- | --- | --- | --- | --- | --- |
| `odyssey-d5-friday` | scene | `the-odyssey` | District 5 | next Friday (rolling) | showtimes are published |
| `spiderman-almaza-aug6` | vox | `spider-man-brand-new-day` | City Centre Almaza | 2026-08-06 (fixed) | a showtime is **bookable** |

Each run checks every watch independently — one failing site can't stop the others, and each gets its own Telegram message and its own dedupe entry.

### Published vs bookable

Both sites list showtimes for a date *before* opening sales, so `notify_on` picks the trigger:

- **`published`** (default) — fire as soon as any showtime appears, bookable or not.
- **`bookable`** — hold off until at least one showtime can actually be booked. Nothing is
  recorded while waiting, so the watch keeps checking every run and fires the moment sales open.

## How it works

1. A scheduled workflow runs `check_showtimes.py` every 10 minutes.
2. For each watch it resolves the target date: either a fixed calendar date, or the next occurrence of a weekday in the watch's timezone.
3. It fetches that date's showtimes. Both sites publish only a rolling window of upcoming dates, so a date that isn't published yet comes back with no showtimes — that's how "not available yet" is detected:
   - **scene** returns an **empty body** from its AJAX fragment
     (`…/movie-details/<movie>.html?business_day=DD-MM-YYYY&ajax=1`).
   - **vox** returns a full page carrying *"No showtimes could be found…"*
     (`/showtimes?c=<cinema>&m=<movie>&d=YYYYMMDD`).
4. Once showtimes appear, they're grouped by experience (IMAX / Premiere / Standard & Deluxe on Scene; GOLD / 4DX / Standard on VOX), each with a booking link or a struck-through marker. Scene distinguishes genuine sell-outs (`sold out`); VOX has a single "unavailable" state covering both sold out and not-yet-on-sale, so it's labelled `not bookable`.
5. If the watch's `notify_on` trigger is met and `state.json` shows it hasn't already notified for this date, it posts to Telegram and records the date. The workflow commits the updated `state.json` back to the repo.

## Adding or changing a watch

Edit `watches.json` and commit — no code change needed.

```json
{
  "id": "unique-slug",
  "site": "vox",
  "movie_slug": "spider-man-brand-new-day",
  "cinema_slug": "city-centre-almaza",
  "target": { "date": "2026-08-06" },
  "notify_on": "bookable"
}
```

| field | required | notes |
| --- | --- | --- |
| `id` | yes | Unique; used as the `state.json` dedupe key. Renaming it re-arms the watch. |
| `site` | yes | `scene` or `vox`. |
| `movie_slug` | yes | From the movie URL, e.g. `.../movie-details/the-odyssey.html` → `the-odyssey`. |
| `cinema_slug` | vox only | From the VOX showtimes URL's `c=` parameter. |
| `target` | yes | Exactly one of `{"weekday": "friday"}` or `{"date": "2026-08-06"}`. |
| `notify_on` | no | `published` (default) or `bookable`. See above. |
| `timezone` | no | Defaults to the `TIMEZONE` repo variable, else `Africa/Cairo`. |
| `base_url` | no | scene only; overrides the movie-details URL. `{movie}` is substituted. |

Config errors (bad site, both/neither target kind, duplicate ids, malformed date, unknown `notify_on`) fail loudly at startup rather than silently not notifying. A fixed-date watch stops checking once its date has passed.

## One-time setup

### 1. Create a Telegram bot

- Open Telegram and message [`@BotFather`](https://t.me/BotFather).
- Send `/newbot`, follow the prompts, and copy the **bot token** it gives you.

### 2. Find your chat ID

- Send any message to your new bot (open its chat first via the link BotFather provided).
- Run `python get_chat_id.py`, or visit `https://api.telegram.org/bot<TOKEN>/getUpdates` and look for `"chat":{"id":<number>,…}`.

### 3. Configure the repo

Push this repo to GitHub, then go to **Settings → Secrets and variables → Actions**:

**Secrets:**

| Name | Value |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | The token from BotFather |
| `TELEGRAM_CHAT_ID`   | Your chat ID |

**Variables:**

| Name | Example | Notes |
| --- | --- | --- |
| `TIMEZONE` | `Africa/Cairo` | Default for watches that don't set their own |

### 4. Enable Actions

**Settings → Actions → General →** allow all actions, and make sure **Workflow permissions** is set to **Read and write permissions** so the workflow can commit `state.json` back.

### 5. Test it

**Actions tab → "Check Showtimes" → Run workflow.** The first successful run with showtimes available will Telegram you; subsequent runs for the same date log `Already notified for …` and exit.

To check delivery and formatting without waiting for real showtimes, run the **"Test Telegram"** workflow (or `python send_test_message.py` locally). It sends one sample alert per configured watch, using that watch's real site wording and trigger headline, each prefixed with a clear TEST banner. Pass a watch id to send just one.

## State file

`state.json` maps each watch id to the date it last notified for:

```json
{ "notified_for": { "odyssey-d5-friday": "20260731" } }
```

To force a re-notification, delete that watch's entry and commit. Deleting the whole file re-arms everything.

## Local dev

```
pip install -r requirements.txt pytest
pytest -q
```

To dry-run locally, set `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` and run `python check_showtimes.py` — note this really does send Telegram messages and write `state.json`. Point `WATCHES_FILE` at a scratch config to try a watch without touching the committed one. The script logs everything it does at INFO level, prefixed with the watch id.

## Notes

- GitHub free-tier scheduled workflows can drift 5–15 minutes during peak load. That's expected.
- Showtimes are fetched from each site's normal endpoints — no headless browser. Both sites' WAFs reject plain `requests`, so fetches go through `curl_cffi` with Chrome TLS impersonation.
- If a site changes its markup, the parser returns nothing and the run exits cleanly as "not yet" rather than crashing. For VOX that would be silent, so the script warns when a page has neither showtimes nor the expected "No showtimes could be found" notice.
