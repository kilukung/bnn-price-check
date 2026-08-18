# BaNANA (bnn.in.th) iPhone/iPad Price Checker

Checks prices for the iPhone and iPad categories on bnn.in.th once a day and
sends a LINE message when a price (or stock status) changes.

## How it works

- `scripts/check_prices.py` fetches the two category pages, parses each
  product card (name, price range, out-of-stock badge), and compares against
  the last snapshot saved in `prices.json`.
- On any change it sends a LINE **broadcast** message via the LINE Messaging
  API — every friend of the LINE Official Account receives it, not just you.
  Share the bot's QR code / add-friend link with anyone who wants price
  alerts too.
- If it can reach the site but parses zero products, it sends a separate LINE
  alert — that means bnn.in.th changed its markup and `PRODUCT_RE` needs
  updating. Transient network failures are logged but not alerted on.
- Runs daily at 11:30 via a macOS LaunchAgent
  (`~/Library/LaunchAgents/com.pisud.bnn-price-check.plist`), independent of
  Claude Code.
- A second LaunchAgent (`...-catchup.plist`) runs `--if-needed` every 30
  minutes. It exits immediately unless today's 11:30 check has not yet
  succeeded — so a run missed because the network was down is retried as soon
  as connectivity returns, and never runs twice in a day. Success is tracked
  in `last_run.json`.
- After every run it regenerates `dashboard.html` — a summary page showing
  system health, run history, and the latest recorded prices. Open it in a
  browser (bookmark it); it refreshes itself on each run.

## One-time setup: LINE notifications

LINE Notify was shut down in March 2025. Notifications now go through the
LINE Messaging API, which requires a LINE Official Account:

1. Go to the [LINE Developers Console](https://developers.line.biz/console/)
   and log in with your LINE account.
2. Create a **Provider** (any name), then create a new **Messaging API**
   channel under it (any name/category is fine — this is just for your own
   notifications).
3. In the channel's **Messaging API** tab:
   - Scroll to **Channel access token** and issue a long-lived token.
   - Note the **Basic ID** / QR code for the channel.
4. Add the bot as a friend on your phone (scan the QR code from step 3) so it
   is allowed to message you. Share the same QR code / add-friend link with
   anyone else who wants price alerts — broadcast messages go to everyone
   who has added the bot.
5. Copy `config.json.example` to `config.json` and fill in
   `line_channel_access_token`.

`config.json` is gitignored — never commit it.

## Manual run / test

```
python3 "scripts/check_prices.py"
```

First run just saves a baseline (no notification). Subsequent runs report
changes. To rebuild only the dashboard without re-checking prices:

```
python3 "scripts/build_dashboard.py"
```

## Managing the daily job

```
# reload after editing the plist
launchctl bootout gui/$(id -u)/com.pisud.bnn-price-check
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.pisud.bnn-price-check.plist

# check status
launchctl print gui/$(id -u)/com.pisud.bnn-price-check

# disable
launchctl bootout gui/$(id -u)/com.pisud.bnn-price-check
```

The catch-up job is managed the same way, using the label
`com.pisud.bnn-price-check-catchup`. Removing it just means a missed run waits
for the next day instead of retrying.

Logs: `check_prices.log` (app-level log/messages), `launchd.out.log` /
`launchd.err.log` (stdout/stderr from the LaunchAgent).

## Notes / limitations

- Tracks the iPhone and iPad *category listing pages*, so prices are the
  min–max range shown per model (all colors/storage combined), not a single
  SKU's exact price.
- If bnn.in.th changes its page markup, `PRODUCT_RE` in
  `scripts/check_prices.py` will need updating (the script logs "0 products
  parsed" if this happens).
- Your Mac must be on (or wake on schedule) at 20:00 for the job to fire.
