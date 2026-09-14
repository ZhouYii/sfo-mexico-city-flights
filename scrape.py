"""Nonstop round trips SFO -> Mexico City, out Fri Jan 22, back Sun Jan 31 2027,
at the cheapest fare sold by the airline. Basic fares are allowed per airline
(`BASIC_OK`); both carriers on this route currently are.

Google's flight lists price everything at the lowest fare, which on United and
Aeromexico is Basic. The fare ladder (Basic / Economy / Classic / ...) only
appears on the booking page of a chosen outbound + return pair, so every pair
Google offers is opened and the cheapest tier that isn't a basic fare is kept.

On a round-trip search Google lists outbound flights first; choosing one shows
only the returns that can be ticketed with it (United out -> United back), so
the pairs are exactly what the airlines sell as one round-trip ticket.

    python scrape.py            # writes docs/data.json
    python scrape.py --headed   # watch the browser

Only fares sold by the airline count. Travel agencies on the booking page quote
a bare price with no fare name, and that price is usually the Basic one.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from fast_flights import FlightQuery, Passengers, create_query
from playwright.sync_api import sync_playwright

ORIGIN, DEST = "SFO", "MEX"
OUT_DATE, RETURN_DATE = date(2027, 1, 22), date(2027, 1, 31)
REFRESH_HOURS = 2
OUT = Path(__file__).parent / "docs" / "data.json"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")

# Fare names that mean "basic economy" under some airline's branding:
# United/Delta "Basic Economy", Aeromexico/Volaris "Basic", Alaska "Saver",
# Viva Aerobus "Light"/"Zero".
BASIC = re.compile(r"\b(basic|saver|light|zero)\b", re.I)
# Airlines whose basic fare is acceptable - the traveller's call. They first
# excluded Basic, then allowed Aeromexico's, then United's too. Remove a name to
# exclude that airline's basic fare again (United Basic has no carry-on).
BASIC_OK = {"Aeromexico", "United"}
TIME = re.compile(r"(\d{1,2}:\d{2} [AP]M)(\+\d)?")
PRICE = re.compile(r"\$([\d,]+)")
BLOCKED = ("unusual traffic", "not a robot", "captcha")
NO_FLIGHTS = ("no nonstop flights found", "no results returned")
OUTBOUND_LIST, RETURN_LIST = "departing flights", "returning flights"


def norm(text: str) -> str:
    # Google separates "1:35" and "PM" with a narrow no-break space.
    return text.replace(" ", " ").replace("\xa0", " ")


def lines_of(text: str) -> list[str]:
    return [l.strip() for l in norm(text).splitlines() if l.strip()]


def search_url() -> str:
    return create_query(
        flights=[
            FlightQuery(date=OUT_DATE.isoformat(), from_airport=ORIGIN,
                        to_airport=DEST, max_stops=0),
            FlightQuery(date=RETURN_DATE.isoformat(), from_airport=DEST,
                        to_airport=ORIGIN, max_stops=0),
        ],
        seat="economy", trip="round-trip", passengers=Passengers(adults=1),
        currency="USD", language="en-US",
    ).url()


def parse_row(text: str) -> dict | None:
    """One list <li>, or None if it isn't a flight summary row.

    Rows read: "1:35 PM", "–", "8:00 PM", "AeromexicoDelta", "4 hr 25 min", ...
    Each flight also has a hidden detail <li> that starts "1:35 PM1:35 PM on"
    and fails the full-match on line one, which is what dedupes them.
    """
    ls = lines_of(text)
    if len(ls) < 5 or not TIME.fullmatch(ls[0]) or "Nonstop" not in ls:
        return None
    arr_i = next((i for i in range(1, 4) if TIME.fullmatch(ls[i])), None)
    if arr_i is None:
        return None
    arr = TIME.fullmatch(ls[arr_i])
    # The price Google prints on the row - always the lowest fare, usually Basic.
    prices = [int(p.group(1).replace(",", "")) for l in ls if (p := PRICE.fullmatch(l))]
    return {
        "list_price": prices[-1] if prices else None,
        "id": f"{ls[0]}|{ls[arr_i + 1]}",
        "depart": ls[0],
        "arrive": arr.group(1),
        "arrive_day_offset": int(arr.group(2) or 0),
        "listed_airlines": ls[arr_i + 1],
        "duration": ls[arr_i + 2],
    }


def parse_fares(text: str) -> tuple[list[dict], str | None]:
    """Named fare tiers sold by airlines, plus the first airline offering any.

    The booking page reads, per seller:
        Book with UnitedAirline / Hide options / Basic Economy / $568 / ... /
        Continue / Economy / $668 / ...
        Book with FlightHub / $561 / Continue      <- agency, no fare name
    """
    ls = lines_of(text)
    try:
        start = ls.index("Booking options")
    except ValueError:
        return [], None
    fares, seller, is_airline, first_airline = [], None, False, None
    for i in range(start + 1, len(ls)):
        line = ls[i]
        if line.startswith("Prices include required"):
            break
        m = re.fullmatch(r"Book with (.+?)(Airline)?", line)
        if m:
            seller, is_airline = m.group(1).strip(), bool(m.group(2))
            if is_airline and not first_airline:
                first_airline = seller
            continue
        p = PRICE.fullmatch(line)
        if p and is_airline:
            name = ls[i - 1]
            if name.startswith("Book with") or PRICE.fullmatch(name):
                continue  # a collapsed seller's headline price, not a tier
            fares.append({"seller": seller, "name": name,
                          "price": int(p.group(1).replace(",", ""))})
    return fares, first_airline


def minutes(t: str) -> int:
    h, rest = t.split(":")
    m, ampm = rest.split(" ")
    return (int(h) % 12 + (12 if ampm == "PM" else 0)) * 60 + int(m)


def rows_on(page) -> dict[str, tuple[int, dict]]:
    """Flight id -> (index among the page's <li>s, parsed row)."""
    found = {}
    texts = page.eval_on_selector_all("li", "els => els.map(e => e.innerText)")
    for i, text in enumerate(texts):
        row = parse_row(text)
        if row and row["id"] not in found:
            found[row["id"]] = (i, row)
    return found


def wait_for_rows(page, marker: str, timeout_s: int = 45) -> dict[str, tuple[int, dict]]:
    """Wait for a flight list headed by `marker`, then return its rows."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        low = norm(page.inner_text("body")).lower()
        if any(s in low for s in BLOCKED):
            raise RuntimeError("Google served a bot challenge")
        if marker in low:
            if rows_on(page):
                page.wait_for_timeout(1500)  # the list streams in; let it settle
                if page.get_by_text(re.compile(r"View more flights", re.I)).count():
                    page.get_by_text(re.compile(r"View more flights", re.I)).first.click()
                    page.wait_for_timeout(2500)
                return rows_on(page)
            if any(s in low for s in NO_FLIGHTS):
                return {}
        page.wait_for_timeout(1000)
    raise RuntimeError(f"timed out waiting for {marker}")


def open_returns(page, url: str, out_id: str) -> dict[str, tuple[int, dict]]:
    """Load the search, choose outbound `out_id`, return the return-flight rows."""
    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    outs = wait_for_rows(page, OUTBOUND_LIST)
    if out_id not in outs:
        raise RuntimeError(f"outbound {out_id} no longer listed")
    page.locator("li").nth(outs[out_id][0]).click()
    return wait_for_rows(page, RETURN_LIST)


def open_fares(page, idx: int) -> tuple[list[dict], str | None, str]:
    page.locator("li").nth(idx).click()
    page.wait_for_selector("text=Booking options", timeout=30_000)
    fares, airline, expanded = [], None, False
    deadline = time.time() + 20
    while time.time() < deadline:
        fares, airline = parse_fares(page.inner_text("body"))
        if fares:
            break
        # The airline's own tiers are sometimes collapsed behind "View options".
        if not expanded and time.time() > deadline - 12:
            expanded = True
            for btn in page.get_by_role("button", name=re.compile("View options", re.I)).all()[:3]:
                try:
                    btn.click(timeout=3000)
                except Exception:  # noqa: BLE001
                    pass
        page.wait_for_timeout(1000)
    return fares, airline, page.url


def tidy_airline(listed: str) -> str:
    # Codeshares arrive glued together ("AeromexicoDelta").
    return re.sub(r"([a-z])([A-Z][a-z])", r"\1 · \2", listed)


def scrape(page) -> dict:
    url = search_url()
    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    outs = wait_for_rows(page, OUTBOUND_LIST)
    if not outs:
        return {"status": "none", "search_url": url, "outbound": [], "return": [], "combos": []}

    outbound = {oid: row for oid, (_, row) in outs.items()}
    returns, combos = {}, []
    for out_id in outbound:
        try:
            rets = open_returns(page, url, out_id)
        except Exception as exc:  # noqa: BLE001 - skip this outbound, keep the rest
            print(f"  out {out_id}: {exc}", file=sys.stderr)
            continue
        for ret_id in list(rets):
            if ret_id not in rets:  # the list was lost on the way back; rebuild it
                rets = open_returns(page, url, out_id)
                if ret_id not in rets:
                    continue
            idx, row = rets[ret_id]
            returns.setdefault(ret_id, row)
            fares, airline, booking_url = [], None, url
            try:
                fares, airline, booking_url = open_fares(page, idx)
            except Exception as exc:  # noqa: BLE001 - keep the pair, just unpriced
                print(f"  {out_id} + {ret_id}: fare page failed: {exc}", file=sys.stderr)
            acceptable = sorted((f for f in fares
                                 if not BASIC.search(f["name"]) or f["seller"] in BASIC_OK),
                                key=lambda f: f["price"])
            basic = [f["price"] for f in fares if BASIC.search(f["name"])]
            combos.append({
                "out": out_id, "ret": ret_id,
                "airline": airline or tidy_airline(outbound[out_id]["listed_airlines"]),
                "fare": acceptable[0] if acceptable else None,
                "fares": acceptable,
                # Kept so the page can reconcile with the price Google lists,
                # which is the Basic fare whenever one exists.
                "basic_price": min(basic) if basic else None,
                "google_list_price": row["list_price"],
                # Distinguishes "only Basic is on sale" from "couldn't read fares".
                "fares_read": bool(fares),
                "booking_url": booking_url,
            })
            print(f"  {out_id:<24} + {ret_id:<24} "
                  f"{acceptable[0]['name'] + ' $' + str(acceptable[0]['price']) if acceptable else '-'}",
                  flush=True)
            page.wait_for_timeout(random.randint(500, 1200))
            try:
                page.go_back(wait_until="domcontentloaded", timeout=30_000)
                rets = wait_for_rows(page, RETURN_LIST, timeout_s=20)
            except Exception:  # noqa: BLE001 - next iteration rebuilds the list
                rets = {}

    if not combos or not any(c["fares_read"] for c in combos):
        raise RuntimeError("found flights but could not read any fares")

    # Name each flight after the airline that actually sells it.
    sold_by = {}
    for c in combos:
        if c["fares_read"]:
            sold_by.setdefault(c["out"], c["airline"])
            sold_by.setdefault(c["ret"], c["airline"])

    def flights(rows: dict) -> list[dict]:
        out = []
        for fid, row in rows.items():
            out.append({k: row[k] for k in ("id", "depart", "arrive", "arrive_day_offset", "duration")}
                       | {"airline": sold_by.get(fid) or tidy_airline(row["listed_airlines"])})
        return sorted(out, key=lambda f: minutes(f["depart"]))

    combos.sort(key=lambda c: (c["fare"] is None, c["fare"]["price"] if c["fare"] else 0))
    return {"status": "ok", "search_url": url, "outbound": flights(outbound),
            "return": flights(returns), "combos": combos}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()

    now = datetime.now(timezone.utc)
    stamp = now.isoformat(timespec="seconds")
    if (now - timedelta(hours=8)).date() > OUT_DATE:
        print("trip has started; nothing to search")
        return 0

    previous = json.loads(OUT.read_text("utf-8")) if OUT.exists() else {}
    if "combos" not in previous:  # an older file format; don't carry it forward
        previous = {}

    base = {"origin": ORIGIN, "destination": DEST,
            "out_date": OUT_DATE.isoformat(), "return_date": RETURN_DATE.isoformat(),
            "refresh_hours": REFRESH_HOURS, "generated_at": stamp}
    code = 0
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=not args.headed)
            try:
                page = browser.new_page(user_agent=UA, locale="en-US",
                                        viewport={"width": 1400, "height": 1000})
                page.set_default_timeout(30_000)
                result = scrape(page)
            finally:
                browser.close()
        data = base | result | {"last_success_at": stamp}
    except Exception as exc:  # noqa: BLE001 - publish the failure, keep the last good times
        print(f"FAILED {type(exc).__name__}: {exc}", file=sys.stderr)
        data = {"outbound": [], "return": [], "combos": [], "search_url": search_url()} \
            | previous | base | {"status": "error", "error": str(exc)[:200]}
        code = 1

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, indent=1), encoding="utf-8")
    print(f"wrote {OUT}: {data['status']}, {len(data['outbound'])} out, "
          f"{len(data['return'])} back, {len(data['combos'])} pairs")
    return code


if __name__ == "__main__":
    sys.exit(main())
