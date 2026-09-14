# SFO ⇄ Mexico City round trip, Jan 22 → Jan 31 2027

**Live page:** https://zhouyii.github.io/sfo-mexico-city-flights/

Nonstop both ways, 1 adult, out Fri Jan 22 SFO → MEX, back Sun Jan 31 MEX → SFO.

Every 2 hours a GitHub Action (`.github/workflows/refresh.yml`) runs `scrape.py`,
which does the round-trip search on Google Flights, picks each outbound flight,
opens the booking page for every return that pairs with it, and records the
cheapest round-trip fare sold by the airline. Basic fares are allowed per airline
via `BASIC_OK` — currently United and Aeromexico, i.e. Basic included. (It started
as "no Basic Economy"; the filtering is kept so it can be switched back.)
It writes `docs/data.json`
and commits it; GitHub Pages serves `docs/`.

Why every pairing is opened: Google's lists price flights at the Basic fare
(United round trip: Basic $568, Economy $668). The fare tiers only appear on the
booking page of a chosen outbound + return. Only fares sold by the airline count;
travel-agency prices there carry no fare name and are usually the Basic fare.

Fare names treated as basic: *Basic*, *Basic Economy*, *Saver*, *Light*, *Zero*
(`BASIC` in `scrape.py`). Airlines whose basic fare is allowed anyway: `BASIC_OK`.

If a search fails, the page keeps the last good results and says so in a banner.

Run locally: `pip install -r requirements.txt && python -m playwright install chromium`,
then `python scrape.py` (add `--headed` to watch).

Run now instead of waiting: Actions tab → Refresh flights → Run workflow, or
`gh workflow run refresh.yml`. To stop it: `gh workflow disable refresh.yml`.
