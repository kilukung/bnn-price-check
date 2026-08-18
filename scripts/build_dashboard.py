#!/usr/bin/env python3
"""Build a simple HTML dashboard summarising check_prices runs."""
import html
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
LOG_FILE = BASE_DIR / "check_prices.log"
STATE_FILE = BASE_DIR / "prices.json"
OUT_FILE = BASE_DIR / "dashboard.html"

RUN_START_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] (.*)$")

# Status kinds, worst-first for health reporting.
PARSER_ERROR = "parser_error"
NETWORK_ERROR = "network_error"
CHANGED = "changed"
OK = "ok"


def parse_runs():
    """Parse the append-only log into one record per run, newest last."""
    if not LOG_FILE.exists():
        return []

    runs = []
    current = None
    for raw in LOG_FILE.read_text(encoding="utf-8").splitlines():
        m = RUN_START_RE.match(raw)
        if m:
            timestamp, first_line = m.groups()
            # The parser-failure alert is a second log call within the same run.
            if current and current["timestamp"] == timestamp and "⚠️" in first_line:
                current["status"] = PARSER_ERROR
                continue
            current = {
                "timestamp": timestamp,
                "status": classify(first_line),
                "headline": first_line,
                "details": [],
            }
            runs.append(current)
        elif current is not None:
            # Old-format "Errors:" lines were logged separately, not as details.
            if raw.startswith("Errors: "):
                continue
            # Blank lines are kept: they separate one changed product from the next.
            current["details"].append(raw)
    return runs


def classify(line):
    if "0 products parsed" in line or "⚠️" in line:
        return PARSER_ERROR
    if "fetch had errors" in line or "fetch failed" in line:
        return NETWORK_ERROR
    if "เปลี่ยนแปลง" in line:
        return CHANGED
    return OK


def group_details(details):
    """Split a change block into (name, change, url) triples, one per product."""
    items = []
    buffer = []
    for line in details + [""]:
        if not line.strip():
            if buffer:
                url = next((b for b in buffer if b.startswith("http")), "")
                text = [b for b in buffer if not b.startswith("http")]
                # Stock-only changes are one line; price changes put the
                # "old -> new" range on a second line.
                name = text[0] if text else ""
                change = " ".join(text[1:])
                items.append((name, change, url))
                buffer = []
        else:
            buffer.append(line.strip())
    return items


STATUS_LABEL = {
    OK: ("ไม่มีการเปลี่ยนแปลง", "neutral"),
    CHANGED: ("พบราคาเปลี่ยน", "accent"),
    NETWORK_ERROR: ("เน็ตไม่ติด ข้ามรอบนี้", "warn"),
    PARSER_ERROR: ("อ่านราคาไม่ได้ ต้องแก้สคริปต์", "bad"),
}

STOCK_IN = "มีสินค้าแล้ว"
STOCK_OUT = "สินค้าหมด"
NUM_RE = re.compile(r"฿\s*([\d,]+)")
DASH_SUFFIX_RE = re.compile(r"\s+[—-]\s+(?:%s|%s).*$" % (STOCK_IN, STOCK_OUT))


def _first_price(text):
    m = NUM_RE.search(text or "")
    return int(m.group(1).replace(",", "")) if m else None


def _price_span(text):
    """Normalise a '฿A - ฿B' fragment, dropping trailing annotations."""
    prices = NUM_RE.findall(text or "")
    if not prices:
        return ""
    if len(prices) == 1:
        return f"฿{prices[0]}"
    return f"฿{prices[0]}–{prices[-1]}"


def make_event(timestamp, name, change, url):
    """Turn one logged change block into a structured price event."""
    raw = f"{name} {change}"
    restock = STOCK_IN in raw
    out_of_stock = STOCK_OUT in raw

    before = after = ""
    delta = None
    if "->" in change:
        left, right = change.split("->", 1)
        before, after = _price_span(left), _price_span(right)
        old, new = _first_price(left), _first_price(right)
        if old and new and old != new:
            delta = (new - old) / old * 100
    elif out_of_stock:
        before = _price_span(raw)
        after = "—"
    elif restock:
        after = _price_span(raw)
        before = "—"

    if out_of_stock:
        kind, label = "oos", STOCK_OUT
    elif restock:
        kind, label = "restock", STOCK_IN
    elif delta is not None and delta < 0:
        kind, label = "drop", "ลดราคา"
    elif delta is not None and delta > 0:
        kind, label = "rise", "ขึ้นราคา"
    else:
        kind, label = "neutral", "เปลี่ยนแปลง"

    return {
        "timestamp": timestamp,
        "date": timestamp[:10],
        "time": timestamp[11:16],
        "name": DASH_SUFFIX_RE.sub("", name).strip(),
        "before": before or "—",
        "after": after or "—",
        "delta": delta,
        "kind": kind,
        "label": label,
        "url": url,
    }


def collect_events(runs):
    """All price events, newest first."""
    events = []
    for run in runs:
        if run["status"] != CHANGED:
            continue
        for name, change, url in group_details(run["details"]):
            if not name:
                continue
            events.append(make_event(run["timestamp"], name, change, url))
    events.reverse()
    return events


def delta_class(delta):
    """Colour the % by direction, independent of the event kind."""
    if delta is None:
        return "neutral empty"
    return "rise" if delta > 0 else "drop"


def fmt_delta(delta):
    if delta is None:
        return "—"
    return f"{'+' if delta > 0 else '−'}{abs(delta):.1f}%"


def build_health(runs):
    if not runs:
        return "bad", "ยังไม่เคยรัน", "ไม่มีข้อมูลใน log"

    last = runs[-1]
    last_dt = datetime.strptime(last["timestamp"], "%Y-%m-%d %H:%M:%S")
    age_days = (datetime.now() - last_dt).days

    if last["status"] == PARSER_ERROR:
        return "bad", "ระบบมีปัญหา", "อ่านราคาจากเว็บไม่ได้ — เว็บอาจเปลี่ยนโครงสร้างหน้า ต้องแก้สคริปต์"
    if age_days >= 2:
        return "bad", "ระบบอาจหยุดทำงาน", f"ไม่ได้รันมา {age_days} วันแล้ว (คอมอาจปิดอยู่)"
    if last["status"] == NETWORK_ERROR:
        recent = [r["status"] for r in runs[-3:]]
        if recent.count(NETWORK_ERROR) == len(recent):
            return "warn", "ต่อเน็ตไม่ได้ติดกันหลายรอบ", "ลองเช็กว่าเครื่องต่อเน็ตตอน 11:30 หรือไม่"
        return "warn", "รอบล่าสุดเน็ตไม่ติด", "ไม่ใช่ปัญหาถาวร รอบถัดไปมักกลับมาปกติเอง"
    return "ok", "ระบบทำงานปกติ", "ตรวจราคาสำเร็จในรอบล่าสุด"


THAI_MONTHS = ["ม.ค.", "ก.พ.", "มี.ค.", "เม.ย.", "พ.ค.", "มิ.ย.",
               "ก.ค.", "ส.ค.", "ก.ย.", "ต.ค.", "พ.ย.", "ธ.ค."]


def thai_date(iso):
    d = datetime.strptime(iso, "%Y-%m-%d")
    return f"{d.day:02d} {THAI_MONTHS[d.month - 1]}"


def day_label(iso, today, yesterday):
    if iso == today:
        return "วันนี้ · TODAY"
    if iso == yesterday:
        return "เมื่อวาน · YESTERDAY"
    return thai_date(iso) + f" {iso[:4]}"


CSS = """
:root {
  --bg:#f7f7f8; --card:#fff; --line:#e4e4e7; --line-soft:#f1f1f3;
  --text:#17181c; --muted:#71717a; --faint:#a1a1aa; --dim:#3f3f46;
  --accent:#f2b60d; --accent-ink:#8a6300; --accent-soft:#fdf3d6; --accent-pale:#f6e8bb;
  --ok:#17754f; --ok-bg:#e5f3ec; --bad:#b3261e; --bad-bg:#fbeceb;
  --info:#1d4ed8; --info-bg:#e8eeff; --neutral:#52525b; --neutral-bg:#f4f4f5;
  --warn:#92600a; --warn-bg:#fdf3e0;
}
* { box-sizing: border-box; }
body {
  margin:0; background:var(--bg); color:var(--text);
  font-family:"IBM Plex Sans Thai","IBM Plex Sans",-apple-system,system-ui,sans-serif;
}
a { color:inherit; text-decoration:none; }
.mono { font-family:"IBM Plex Mono",ui-monospace,monospace; font-variant-numeric:tabular-nums; }

/* layout */
.shell { display:flex; min-height:100vh; }
.side {
  width:232px; flex:none; background:var(--card); border-right:1px solid var(--line);
  padding:22px 14px; display:flex; flex-direction:column; gap:26px;
  position:sticky; top:0; height:100vh;
}
.brand { display:flex; align-items:center; gap:10px; padding:0 8px; }
.brand .mark { width:26px; height:26px; border-radius:8px; background:var(--accent); flex:none; }
.brand .name { font:600 14px "IBM Plex Sans",sans-serif; }
.nav { display:flex; flex-direction:column; gap:2px; }
.nav a {
  padding:9px 12px; border-radius:9px; color:var(--dim);
  font:500 13.5px "IBM Plex Sans Thai",sans-serif;
  display:flex; align-items:center; justify-content:space-between; gap:8px;
}
.nav a:hover { background:var(--neutral-bg); }
.nav a .en { font:400 11px "IBM Plex Sans",sans-serif; color:var(--faint); }
.nav a.active { background:var(--accent-soft); color:var(--accent-ink); font-weight:600; }
.nav a.active .en { color:#b58c2b; }
.nav .badge {
  padding:1px 7px; border-radius:10px; background:var(--bad-bg); color:var(--bad);
  font:600 10.5px "IBM Plex Mono",monospace;
}
.side-health {
  margin-top:auto; padding:12px; border-radius:11px; background:var(--bg);
  border:1px solid var(--line); display:flex; flex-direction:column; gap:6px;
}
.side-health .title { display:flex; align-items:center; gap:7px; font:600 12px "IBM Plex Sans Thai",sans-serif; }
.side-health .dot { width:7px; height:7px; border-radius:4px; flex:none; }
.side-health .note { font:400 11.5px/1.5 "IBM Plex Sans Thai",sans-serif; color:var(--muted); }
.h-ok { color:var(--ok); } .h-ok .dot { background:var(--ok); }
.h-warn { color:var(--warn); } .h-warn .dot { background:var(--warn); }
.h-bad { color:var(--bad); } .h-bad .dot { background:var(--bad); }

.main { flex:1; min-width:0; display:flex; flex-direction:column; }
.topbar {
  padding:20px 28px; border-bottom:1px solid var(--line); background:var(--card);
  display:flex; align-items:center; justify-content:space-between; gap:20px; flex-wrap:wrap;
}
.topbar h1 { margin:0; font:600 19px "IBM Plex Sans Thai",sans-serif; }
.topbar .sub { font:400 12.5px "IBM Plex Sans",sans-serif; color:var(--muted); margin-top:3px; }
.content { padding:24px 28px 56px; display:flex; flex-direction:column; gap:20px; }
.btn {
  padding:9px 14px; border-radius:9px; border:1px solid var(--line); background:var(--card);
  font:500 13px "IBM Plex Sans Thai",sans-serif; color:var(--dim);
}
.btn.primary { background:var(--accent); border-color:var(--accent); color:var(--text); }

/* cards */
.card { background:var(--card); border:1px solid var(--line); border-radius:13px; }
.card-head {
  padding:15px 18px; border-bottom:1px solid var(--line);
  display:flex; align-items:center; justify-content:space-between; gap:12px;
  font:600 14.5px "IBM Plex Sans Thai",sans-serif;
}
.pad { padding:16px 18px; }
.stats { display:grid; grid-template-columns:repeat(4,1fr); gap:14px; }
.stat { background:var(--card); border:1px solid var(--line); border-radius:13px; padding:16px; display:flex; flex-direction:column; gap:7px; }
.stat .label { font:600 11px "IBM Plex Sans",sans-serif; letter-spacing:.07em; color:var(--faint); }
.stat .value { font:500 26px "IBM Plex Mono",monospace; letter-spacing:-.02em; }
.stat .foot { font:400 12px "IBM Plex Sans Thai",sans-serif; color:var(--muted); }
.cols { display:grid; grid-template-columns:1.55fr 1fr; gap:18px; align-items:start; }
.stack { display:flex; flex-direction:column; gap:18px; }

/* pills */
.pill { display:inline-block; padding:3px 9px; border-radius:20px; font:600 11px "IBM Plex Sans Thai",sans-serif; white-space:nowrap; }
.pill.drop, .pill.ok { background:var(--ok-bg); color:var(--ok); }
.pill.rise, .pill.bad { background:var(--bad-bg); color:var(--bad); }
.pill.restock, .pill.accent { background:var(--info-bg); color:var(--info); }
.pill.oos, .pill.neutral { background:var(--neutral-bg); color:var(--neutral); }
.pill.warn { background:var(--warn-bg); color:var(--warn); }
.t-drop { color:var(--ok); } .t-rise { color:var(--bad); }
.t-oos, .t-neutral { color:var(--faint); }
.t-neutral.empty { color:var(--faint); } .t-restock { color:var(--info); }

/* tables */
table { width:100%; border-collapse:collapse; }
th {
  text-align:left; padding:12px 18px; font:600 11px "IBM Plex Sans",sans-serif;
  letter-spacing:.06em; color:var(--faint); border-bottom:1px solid var(--line); white-space:nowrap;
}
td { padding:12px 18px; border-top:1px solid var(--line-soft); font:400 13.5px "IBM Plex Sans",sans-serif; vertical-align:middle; }
tbody tr:first-child td { border-top:none; }
th.r, td.r { text-align:right; }
td .model { font:500 13.5px "IBM Plex Sans",sans-serif; display:block; }
td .meta { font:400 11.5px "IBM Plex Sans Thai",sans-serif; color:var(--faint); }
td.num { font:500 13px "IBM Plex Mono",monospace; white-space:nowrap; }
td.num.was { font-weight:400; color:var(--faint); }
.bar-cell { width:26px; padding-right:0; }
.bar { width:6px; height:26px; border-radius:3px; display:block; }
.bar.drop { background:var(--ok); } .bar.rise { background:var(--bad); }
.bar.restock { background:var(--info); } .bar.oos, .bar.neutral { background:var(--faint); }
.scroll { overflow-x:auto; }
.scroll table { min-width:520px; }
.scroll.wide table { min-width:680px; }
td .was { color:var(--faint); font-weight:400; }

/* run group heading */
.run-head { display:flex; align-items:center; gap:10px; margin-bottom:10px; }
.run-head .when { font:600 12px "IBM Plex Sans",sans-serif; letter-spacing:.07em; color:var(--faint); }
.run-head .rule { flex:1; height:1px; background:var(--line); }
.run-head .id { font:400 11.5px "IBM Plex Mono",monospace; color:var(--faint); }

/* activity chart */
.chart { display:flex; align-items:flex-end; gap:5px; height:78px; }
.chart div { flex:1; background:var(--accent-pale); border-radius:3px; min-height:4px; }
.chart div.hit { background:var(--accent); }
.axis { display:flex; justify-content:space-between; font:400 11px "IBM Plex Mono",monospace; color:var(--faint); }

/* history list */
.runs { list-style:none; margin:0; padding:0; }
.runs > li { padding:14px 18px; border-top:1px solid var(--line-soft); }
.runs > li:first-child { border-top:none; }
.runs .line { display:flex; align-items:center; gap:12px; flex-wrap:wrap; }
.runs .when { font:400 13px "IBM Plex Mono",monospace; color:var(--muted); }
.runs ul { margin:8px 0 0; padding-left:18px; }
.runs ul li { margin-bottom:6px; font:400 13px "IBM Plex Sans",sans-serif; }
.muted { color:var(--muted); margin:0; font:400 12.5px "IBM Plex Sans Thai",sans-serif; }
.section-label { font:600 11px "IBM Plex Sans",sans-serif; letter-spacing:.08em; color:var(--faint); }
footer { padding:0 28px 40px; color:var(--faint); font:400 11.5px "IBM Plex Mono",monospace; }

/* view switching */
.view { display:none; flex-direction:column; gap:20px; }
.view.active { display:flex; }
.tabbar { display:none; }

/* mobile */
@media (max-width: 900px) {
  .cols, .stats { grid-template-columns:1fr 1fr; }
}
@media (max-width: 760px) {
  .side { display:none; }
  .shell { display:block; }
  .topbar { padding:16px 20px; }
  .topbar h1 { font-size:22px; }
  .content { padding:16px 20px 104px; gap:16px; }
  .stats { grid-template-columns:1fr 1fr; }
  .cols { grid-template-columns:1fr; }
  /* tables collapse into the stacked alert cards from the phone mockup */
  .tbl { border:none; background:transparent; border-radius:0; }
  .tbl .card-head { border:none; padding:0 0 10px; }
  .tbl .scroll, .tbl.scroll { overflow:visible; }
  .tbl table, .tbl.scroll table { min-width:0; }
  .tbl thead, .tbl td.bar-cell { display:none; }
  .tbl table, .tbl tbody, .tbl tr, .tbl td { display:block; width:100%; }
  .tbl tr {
    background:var(--card); border:1px solid var(--line); border-radius:16px;
    padding:16px; margin-bottom:12px; box-shadow:0 1px 2px rgba(0,0,0,.04);
  }
  .tbl td { border:none; padding:0; text-align:left !important; }
  .tbl tr { display:flex; flex-wrap:wrap; align-items:baseline; gap:8px 10px; }
  .tbl td { width:auto; }
  .tbl td.name { flex-basis:100%; }
  .tbl td.st { margin-left:auto; }
  .tbl td.was { text-decoration:line-through; }
  .tbl td.empty { display:none; }
  .tbl td.num { font-size:16px; }
  .tbl td .meta { margin-top:2px; }
  .tabbar {
    position:fixed; left:0; right:0; bottom:0; height:76px; display:flex;
    background:rgba(255,255,255,.9); backdrop-filter:blur(18px);
    border-top:1px solid var(--line); padding:10px 12px 0; z-index:20;
  }
  .tabbar a {
    flex:1; display:flex; flex-direction:column; align-items:center; gap:5px;
    color:var(--faint); font:500 10.5px "IBM Plex Sans Thai",sans-serif;
  }
  .tabbar a .ico { width:22px; height:22px; border-radius:6px; border:2px solid var(--faint); }
  .tabbar a.active { color:var(--accent-ink); }
  .tabbar a.active .ico { background:var(--accent); border-color:var(--accent); }
}
"""

SCRIPT = """
(function () {
  var links = document.querySelectorAll('[data-view]');
  function show(name) {
    document.querySelectorAll('.view').forEach(function (v) {
      v.classList.toggle('active', v.id === 'view-' + name);
    });
    links.forEach(function (l) {
      l.classList.toggle('active', l.dataset.view === name);
    });
    if (location.hash.slice(1) !== name) history.replaceState(null, '', '#' + name);
    window.scrollTo(0, 0);
  }
  links.forEach(function (l) {
    l.addEventListener('click', function (e) { e.preventDefault(); show(l.dataset.view); });
  });
  var initial = location.hash.slice(1);
  show(document.getElementById('view-' + initial) ? initial : 'overview');
})();
"""

NAV = [
    ("overview", "ภาพรวม", "Overview"),
    ("changes", "การเปลี่ยนแปลง", "Changes"),
    ("tracked", "สินค้าที่ติดตาม", "Tracked"),
    ("history", "ประวัติการตรวจ", "Runs"),
]


def esc(value):
    return html.escape(str(value))


def event_row(event, with_bar=True):
    link = (f"<a href='{esc(event['url'])}' target='_blank' class='model'>{esc(event['name'])}</a>"
            if event["url"] else f"<span class='model'>{esc(event['name'])}</span>")
    bar = f"<td class='bar-cell'><span class='bar {event['kind']}'></span></td>" if with_bar else ""
    return (
        f"<tr>{bar}"
        f"<td class='name'>{link}<span class='meta'>{esc(event['time'])} · {esc(event['label'])}</span></td>"
        f"<td class='num was r'>{esc(event['before'])}</td>"
        f"<td class='num r'>{esc(event['after'])}</td>"
        f"<td class='num r t-{delta_class(event['delta'])}'>{esc(fmt_delta(event['delta']))}</td>"
        f"<td class='r st'><span class='pill {event['kind']}'>{esc(event['label'])}</span></td>"
        f"</tr>"
    )


def event_row_compact(event):
    """Overview variant: one combined 'from -> to' column, as in the mockup."""
    link = (f"<a href='{esc(event['url'])}' target='_blank' class='model'>{esc(event['name'])}</a>"
            if event["url"] else f"<span class='model'>{esc(event['name'])}</span>")
    return (
        f"<tr>"
        f"<td class='name'>{link}<span class='meta'>{esc(event['time'])} · {esc(event['label'])}</span></td>"
        f"<td class='num r'><span class='was'>{esc(event['before'])}</span> → {esc(event['after'])}</td>"
        f"<td class='num r t-{delta_class(event['delta'])}'>{esc(fmt_delta(event['delta']))}</td>"
        f"</tr>"
    )


def render_overview(runs, events, products, health):
    today = runs[-1]["date"] if runs else ""
    today_events = [e for e in events if e["date"] == today]
    kinds = [e["kind"] for e in today_events]
    breakdown = (f"ลดราคา {kinds.count('drop')} · ขึ้นราคา {kinds.count('rise')} · "
                 f"สต็อก {kinds.count('oos') + kinds.count('restock')}")

    drops = [e for e in events[:60] if e["delta"] is not None and e["delta"] < 0]
    biggest = min(drops, key=lambda e: e["delta"]) if drops else None

    by_cat = {}
    for product in products.values():
        by_cat.setdefault(product["category"], []).append(product)
    cat_summary = " · ".join(f"{cat} {len(items)}" for cat, items in by_cat.items()) or "—"
    oos_count = sum(1 for p in products.values() if p["out_of_stock"])

    recent = "".join(event_row_compact(e) for e in events[:6]) or (
        "<tr><td colspan='3'><p class='muted'>ยังไม่มีการเปลี่ยนแปลงที่บันทึกไว้</p></td></tr>")

    # 14-day activity: how many events landed on each of the last 14 run days.
    days = sorted({r["date"] for r in runs})[-14:]
    per_day = {d: sum(1 for e in events if e["date"] == d) for d in days}
    peak = max(per_day.values()) if per_day and max(per_day.values()) else 1
    bars = "".join(
        f"<div class='{'hit' if per_day[d] else ''}' style='height:{max(14, round(per_day[d] / peak * 100))}%' "
        f"title='{esc(d)} · {per_day[d]} รายการ'></div>"
        for d in days
    ) or "<div style='height:14%'></div>"
    axis = (f"<span>{thai_date(days[0])}</span><span>{thai_date(days[-1])}</span>"
            if days else "<span>—</span>")

    return f"""
<div class="view" id="view-overview">
  <div class="stats">
    <div class="stat">
      <div class="label">CHANGES TODAY</div>
      <div class="value">{len(today_events)}</div>
      <div class="foot">{esc(breakdown)}</div>
    </div>
    <div class="stat">
      <div class="label">BIGGEST DROP</div>
      <div class="value t-drop">{esc(fmt_delta(biggest['delta'])) if biggest else '—'}</div>
      <div class="foot">{esc(biggest['name']) if biggest else 'ยังไม่พบราคาลด'}</div>
    </div>
    <div class="stat">
      <div class="label">TRACKED</div>
      <div class="value">{len(products)}</div>
      <div class="foot">{esc(cat_summary)}</div>
    </div>
    <div class="stat">
      <div class="label">OUT OF STOCK</div>
      <div class="value">{oos_count}</div>
      <div class="foot">จาก {len(products)} รายการที่ติดตาม</div>
    </div>
  </div>

  <div class="cols">
    <div class="card tbl">
      <div class="card-head">การเปลี่ยนแปลงล่าสุด<a href="#changes" data-view="changes"
        style="font:500 12.5px 'IBM Plex Sans Thai',sans-serif;color:var(--accent-ink)">ดูทั้งหมด</a></div>
      <div class="scroll"><table>
        <thead><tr><th>MODEL / รุ่น</th><th class="r">FROM → TO</th><th class="r">CHANGE</th></tr></thead>
        <tbody>{recent}</tbody>
      </table></div>
    </div>

    <div class="stack">
      <div class="card pad" style="display:flex;flex-direction:column;gap:14px">
        <div style="font:600 14.5px 'IBM Plex Sans Thai',sans-serif">การเปลี่ยนแปลงต่อรอบ 14 วันล่าสุด</div>
        <div class="chart">{bars}</div>
        <div class="axis">{axis}</div>
      </div>
      <div class="card pad" style="display:flex;flex-direction:column;gap:10px">
        <div style="font:600 14.5px 'IBM Plex Sans Thai',sans-serif">สถานะระบบ</div>
        <div class="side-health h-{health[0]}" style="margin:0;background:var(--bg)">
          <div class="title"><span class="dot"></span>{esc(health[1])}</div>
          <div class="note">{esc(health[2])}</div>
        </div>
        <div class="muted">รันทั้งหมด {len(runs)} รอบ · ตรวจอัตโนมัติทุกวัน 11:30 น.</div>
      </div>
    </div>
  </div>
</div>"""


def render_changes(events, today, yesterday):
    groups = []
    seen = []
    for event in events:
        if not seen or seen[-1][0] != event["timestamp"]:
            seen.append((event["timestamp"], []))
        seen[-1][1].append(event)

    for timestamp, items in seen[:20]:
        rows = "".join(event_row(e) for e in items)
        groups.append(f"""
      <div style="display:flex;flex-direction:column;gap:10px">
        <div class="run-head">
          <span class="when">{esc(day_label(timestamp[:10], today, yesterday))} · {esc(timestamp[11:16])}</span>
          <span class="rule"></span>
          <span class="id">{len(items)} รายการ</span>
        </div>
        <div class="card tbl scroll wide"><table>
          <thead><tr><th class="bar-cell"></th><th>MODEL / รุ่น</th><th class="r">ก่อน</th>
            <th class="r">หลัง</th><th class="r">Δ</th><th class="r">สถานะ</th></tr></thead>
          <tbody>{rows}</tbody>
        </table></div>
      </div>""")

    body = "".join(groups) or "<div class='card pad'><p class='muted'>ยังไม่มีการเปลี่ยนแปลงที่บันทึกไว้</p></div>"
    return f"""
<div class="view" id="view-changes">
  <div style="display:flex;flex-direction:column;gap:22px">{body}</div>
</div>"""


def render_tracked(products):
    by_cat = {}
    for product in products.values():
        by_cat.setdefault(product["category"], []).append(product)

    sections = []
    for cat, items in by_cat.items():
        rows = "".join(
            f"<tr><td class='name'><a href='{esc(p['url'])}' target='_blank' class='model'>{esc(p['name'])}</a></td>"
            f"<td class='num r'>{esc(p['price'])}</td>"
            f"<td class='r st'><span class='pill {'oos' if p['out_of_stock'] else 'ok'}'>"
            f"{STOCK_OUT if p['out_of_stock'] else 'มีสินค้า'}</span></td></tr>"
            for p in sorted(items, key=lambda p: p["name"])
        )
        sections.append(f"""
    <div class="card tbl">
      <div class="card-head">{esc(cat)}<span class="section-label">{len(items)} รุ่น</span></div>
      <div class="scroll"><table>
        <thead><tr><th>MODEL / รุ่น</th><th class="r">ราคา</th><th class="r">สถานะ</th></tr></thead>
        <tbody>{rows}</tbody>
      </table></div>
    </div>""")

    return f"""
<div class="view" id="view-tracked">{''.join(sections) or "<div class='card pad'><p class='muted'>ยังไม่มีข้อมูลสินค้า</p></div>"}</div>"""


def render_history(runs):
    items = []
    for run in reversed(runs[-30:]):
        label, cls = STATUS_LABEL[run["status"]]
        events = [make_event(run["timestamp"], n, c, u)
                  for n, c, u in group_details(run["details"]) if n]
        if events:
            detail = "<ul>" + "".join(
                f"<li>" + (f"<a href='{esc(e['url'])}' target='_blank'>{esc(e['name'])}</a>"
                           if e["url"] else esc(e["name"]))
                + f" <span class='mono t-{e['kind']}'>{esc(e['before'])} → {esc(e['after'])}"
                + (f" ({esc(fmt_delta(e['delta']))})" if e["delta"] is not None else "")
                + "</span></li>"
                for e in events
            ) + "</ul>"
        elif run["status"] == NETWORK_ERROR:
            detail = "<p class='muted'>ต่ออินเทอร์เน็ตไม่ได้ตอนที่รัน จึงข้ามรอบนี้ไป</p>"
        elif run["status"] == PARSER_ERROR:
            detail = "<p class='muted'>เข้าเว็บได้ แต่อ่านราคาไม่เจอ — หน้าเว็บน่าจะเปลี่ยนโครงสร้าง</p>"
        else:
            detail = "<p class='muted'>ราคาทุกรุ่นเท่าเดิม</p>"

        items.append(f"<li><div class='line'><span class='when'>{esc(run['timestamp'])}</span>"
                     f"<span class='pill {cls}'>{label}</span></div>{detail}</li>")

    return f"""
<div class="view" id="view-history">
  <div class="card"><ul class="runs">{''.join(items) or "<li><p class='muted'>ไม่มีข้อมูลใน log</p></li>"}</ul></div>
</div>"""


def render(runs, products):
    for run in runs:
        run["date"] = run["timestamp"][:10]

    health = build_health(runs)
    events = collect_events(runs)

    today = runs[-1]["date"] if runs else ""
    yesterday = ""
    if today:
        yesterday = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    today_count = sum(1 for e in events if e["date"] == today)

    nav = "".join(
        f"<a href='#{key}' data-view='{key}'>"
        f"<span>{th}</span>"
        + (f"<span class='badge'>{today_count}</span>" if key == "changes" and today_count
           else f"<span class='en'>{en}</span>")
        + "</a>"
        for key, th, en in NAV
    )
    tabs = "".join(
        f"<a href='#{key}' data-view='{key}'><span class='ico'></span><span>{th}</span></a>"
        for key, th, _ in NAV
    )

    last_run = runs[-1]["timestamp"] if runs else "—"
    generated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    return f"""<!doctype html>
<html lang="th">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Price Watch · สรุปการตรวจราคา BaNANA</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans+Thai:wght@400;500;600;700&family=IBM+Plex+Sans:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>{CSS}</style>
</head>
<body>
<div class="shell">
  <aside class="side">
    <div class="brand"><span class="mark"></span><span class="name">Price Watch</span></div>
    <nav class="nav">{nav}</nav>
    <div class="side-health h-{health[0]}">
      <div class="title"><span class="dot"></span>{esc(health[1])}</div>
      <div class="note">{esc(health[2])}</div>
      <div class="note">{len(runs)} รอบ · ตรวจล่าสุด {esc(last_run[11:16] or '—')}</div>
    </div>
  </aside>

  <main class="main">
    <header class="topbar">
      <div>
        <h1>สรุปการตรวจราคา BaNANA</h1>
        <div class="sub">bnn.in.th · iPhone &amp; iPad · ตรวจล่าสุด {esc(last_run)}</div>
      </div>
      <div style="display:flex;gap:10px">
        <span class="btn">{len(products)} รายการที่ติดตาม</span>
        <span class="btn primary">วันนี้ {today_count} การเปลี่ยนแปลง</span>
      </div>
    </header>

    <div class="content">
      {render_overview(runs, events, products, health)}
      {render_changes(events, today, yesterday)}
      {render_tracked(products)}
      {render_history(runs)}
    </div>

    <footer>generated {generated}</footer>
  </main>
</div>
<nav class="tabbar">{tabs}</nav>
<script>{SCRIPT}</script>
</body>
</html>
"""


def main():
    runs = parse_runs()
    products = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}
    OUT_FILE.write_text(render(runs, products), encoding="utf-8")
    print(f"Dashboard written to {OUT_FILE}")


if __name__ == "__main__":
    main()
