"""
City of Oceanside - Boards, Commissions & Committees
https://www.ci.oceanside.ca.us/government/about-us/city-calendar

The city calendar is a monthly grid that mixes public meetings with community
events. We fetch a window of months around the current one, keep only the
meeting types listed in MEETING_PATTERNS, and follow each meeting to its detail
page for the time range and location.

The city site sits behind Akamai bot protection, so its pages are fetched with
curl_cffi (browser TLS impersonation). Detail
pages also serve Akamai's interstitial challenge, which we solve once per
session (see _solve_challenge).

City Council agendas, minutes and videos come from Legistar
(https://oceanside.legistar.com/Calendar.aspx). Legistar is scraped first with
the core LegistarSpider flow; the city calendar is fetched once it finishes,
so each meeting can be matched to its Legistar links when it's built.
"""

import re
import time
from collections import defaultdict
from datetime import datetime
from urllib.parse import urljoin, urlparse

import scrapy
from city_scrapers_core.constants import CITY_COUNCIL, COMMISSION, COMMITTEE
from city_scrapers_core.items import Meeting
from city_scrapers_core.spiders import LegistarSpider
from curl_cffi import requests as curl_requests
from dateutil.relativedelta import relativedelta
from parsel import Selector
from scrapy import signals
from scrapy.exceptions import DontCloseSpider


class SandieOceansideSpider(LegistarSpider):
    name = "sandie_oceanside"
    agency = "City of Oceanside- Boards, Commissions & Committees"
    timezone = "America/Los_Angeles"
    base_url = "https://www.ci.oceanside.ca.us"
    calendar_url = (
        base_url + "/government/about-us/city-calendar/-curm-{month}/-cury-{year}"
    )
    start_urls = ["https://oceanside.legistar.com/Calendar.aspx"]
    link_types = ["Español"]

    months_back = 36
    months_ahead = 6
    # Seconds to wait between curl_cffi requests to the city site
    request_delay = 0.5

    custom_settings = {
        "DOWNLOAD_DELAY": 1,
        "ROBOTSTXT_OBEY": False,
        "FEED_EXPORT_ENCODING": "utf-8",
    }

    # Matched against the lowercased calendar title with "&" replaced by "and".
    # Calendar titles vary ("Police & Fire Commission Meeting", "Police and Fire
    # Commission Meeting", "Joint City Council/CDC/Harbor/OPFA Regular Meeting").
    MEETING_PATTERNS = [
        r"police and fire commission",
        r"utilities commission",
        r"water[\s/-]*sewer committee",
        r"economic development commission",
        r"planning commission",
        r"city council",
        r"^council\b",
        r"community relations commission",
        r"housing commission",
        r"citizen investment oversight committee",
    ]

    default_location = {
        "name": "TBD",
        "address": "",
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.since_year = self._first_month().year
        self._legistar_links = defaultdict(list)
        self._calendar_started = False

    @classmethod
    def from_crawler(cls, crawler, *args, **kwargs):
        spider = super().from_crawler(crawler, *args, **kwargs)
        crawler.signals.connect(spider._start_calendar, signal=signals.spider_idle)
        return spider

    def start_requests(self):
        # Legistar first (handled by LegistarSpider.parse); the city calendar
        # is fetched in _start_calendar once Legistar is done.
        yield scrapy.Request(self.start_urls[0], callback=self.parse)

    def _start_calendar(self, spider):
        if self._calendar_started:
            return
        self._calendar_started = True
        # City pages are fetched with curl_cffi in parse_calendar_pages.
        self.crawler.engine.crawl(
            scrapy.Request(
                "data:,", callback=self.parse_calendar_pages, dont_filter=True
            )
        )
        raise DontCloseSpider

    def _calendar_urls(self):
        first_month = self._first_month()
        for offset in range(self.months_back + self.months_ahead + 1):
            month = first_month + relativedelta(months=offset)
            yield self.calendar_url.format(month=month.month, year=month.year)

    def _first_month(self):
        return datetime.today().replace(day=1) - relativedelta(months=self.months_back)

    # LEGISTAR

    def parse_legistar(self, events):
        """Store City Council Legistar links by meeting date for matching in
        _parse_detail. Before 2025 Legistar also lists other bodies (Planning
        Commission, Parks and Recreation, ...), which are skipped."""
        for event in events:
            if "city council" not in (event.get("Name") or "").lower():
                continue
            start = self.legistar_start(event)
            links = self.legistar_links(event)
            if start and links:
                self._legistar_links[start.date()].append((start, links))
        return []

    def _parse_legistar_events(self, response):
        """
        Copy of LegistarSpider._parse_legistar_events with two fixes for this
        Legistar page:
        - Use the gridCalendar table. The first rgMasterTable on the page is
          an empty "upcoming meetings" grid.
        - Detect the iCal link by URL only. The core also requires the column
          header to be "" or "ics", but here the header reads " " (from a
          hidden sort button), so every row was skipped.
        """
        tables = response.css("table[id$='gridCalendar_ctl00']")
        if not tables:
            return []
        events_table = tables[0]

        headers = []
        for header in events_table.css("th[class^='rgHeader']"):
            header_text = (
                " ".join(header.css("*::text").extract()).replace("&nbsp;", " ").strip()
            )
            header_inputs = header.css("input")
            if header_text:
                headers.append(header_text)
            elif len(header_inputs) > 0:
                headers.append(header_inputs[0].attrib["value"])
            else:
                headers.append(header.css("img")[0].attrib["alt"])

        events = []
        for row in events_table.css("tr.rgRow, tr.rgAltRow"):
            try:
                data = defaultdict(lambda: None)
                for header, field in zip(headers, row.css("td")):
                    field_text = (
                        " ".join(field.css("*::text").extract())
                        .replace("&nbsp;", " ")
                        .strip()
                    )
                    url = None
                    if len(field.css("a")) > 0:
                        link_el = field.css("a")[0]
                        if "onclick" in link_el.attrib and link_el.attrib[
                            "onclick"
                        ].startswith(("radopen('", "window.open", "OpenTelerikWindow")):
                            url = response.urljoin(
                                link_el.attrib["onclick"].split("'")[1]
                            )
                        elif "href" in link_el.attrib:
                            url = response.urljoin(link_el.attrib["href"])
                    if url:
                        if "View.ashx?M=IC" in url:
                            header = "iCalendar"
                            value = {"url": url}
                        else:
                            value = {"label": field_text, "url": url}
                    else:
                        value = field_text

                    data[header] = value

                ical_url = data.get("iCalendar", {}).get("url")
                if ical_url is None or ical_url in self._scraped_urls:
                    continue
                else:
                    self._scraped_urls.add(ical_url)
                events.append(dict(data))
            except Exception:
                pass

        return events

    def _get_legistar_links(self, start, classification):
        """Find Legistar links for a City Council meeting: same start time, or
        the only Legistar meeting that day."""
        if classification != CITY_COUNCIL or not start:
            return []
        same_day = self._legistar_links.get(start.date(), [])
        for legistar_start, links in same_day:
            if legistar_start == start:
                return links
        if len(same_day) == 1:
            return same_day[0][1]
        return []

    # CITY CALENDAR REQUESTS (curl_cffi)

    def parse_calendar_pages(self, response):
        """Fetch each calendar month and the detail page of every matching
        meeting, and yield Meetings."""
        self.session = curl_requests.Session(impersonate="chrome110")
        seen = set()
        for url in self._calendar_urls():
            html = self._fetch_html(url, referer=self.base_url + "/")
            if not html:
                continue
            for detail in self._parse_calendar(Selector(text=html)):
                # The same meeting can be linked from more than one month page
                if detail["source"] in seen:
                    continue
                seen.add(detail["source"])
                meeting = self._fetch_meeting(detail, referer=url)
                if meeting:
                    yield meeting

    def _fetch_meeting(self, detail, referer, attempts=2):
        """Fetch and parse a detail page, retrying once: the site occasionally
        returns a page without the event details."""
        for _ in range(attempts):
            html = self._fetch_html(detail["source"], referer=referer)
            if html:
                meeting = self._parse_detail(Selector(text=html), **detail)
                if meeting:
                    return meeting
        return None

    def _fetch_html(self, url, referer):
        """Fetch a page with curl_cffi, solving the Akamai challenge if served.
        Returns the HTML, or None if the page couldn't be fetched."""
        time.sleep(self.request_delay)
        response = self._get_response(url, referer)
        if response is not None and self._solve_challenge(url, response.text):
            response = self._get_response(url, referer)
        if response is None or response.status_code != 200:
            self.logger.warning(f"Failed to fetch {url}")
            return None
        if "/_sec/verify" in response.text:
            self.logger.warning(f"Akamai challenge not solved for {url}")
            return None
        return response.text

    def _get_response(self, url, referer):
        try:
            return self.session.get(
                url,
                headers={
                    "Referer": referer,
                    "Upgrade-Insecure-Requests": "1",
                },
                timeout=30,
            )
        except curl_requests.RequestsError as e:
            self.logger.warning(f"Error fetching {url}: {e}")
            return None

    def _solve_challenge(self, url, html):
        """
        Akamai's interstitial challenge page computes a "pow" value from two
        inline numbers and POSTs it with a "bm-verify" token. We do the same so
        the session gets a verified cookie, which then lets later pages load
        directly. Returns True if a solution was submitted.
        """
        challenge = self._parse_challenge(html)
        if not challenge:
            return False
        try:
            self.session.post(
                urljoin(url, "/_sec/verify?provider=interstitial"),
                json=challenge,
                timeout=30,
            )
        except curl_requests.RequestsError as e:
            self.logger.warning(f"Error solving Akamai challenge for {url}: {e}")
            return False
        return True

    def _parse_challenge(self, html):
        """Extract the challenge payload, e.g. from
        'var i = 1792696350; var j = i + Number("8912" + "10911");'"""
        if "/_sec/verify" not in html:
            return None
        i_match = re.search(r"var i = (\d+);", html)
        j_match = re.search(r'var j = i \+ Number\("(\d+)" \+ "(\d+)"\);', html)
        token_match = re.search(r'"bm-verify":\s*"([^"]+)"', html)
        if not (i_match and j_match and token_match):
            return None
        return {
            "bm-verify": token_match.group(1),
            "pow": int(i_match.group(1)) + int("".join(j_match.groups())),
        }

    # CITY CALENDAR PARSING

    def _parse_calendar(self, selector):
        """Find matching meetings on a calendar month page. Returns the title,
        start and detail page URL for each."""
        details = []
        for day in selector.css("td.calendar_day_with_items"):
            date = self._parse_day_date(day)
            if not date:
                continue
            for item in day.css(".calendar_item"):
                link = item.css("a.calendar_eventlink")
                title = self._clean_text(
                    link.attrib.get("title") or " ".join(link.css("::text").getall())
                )
                href = link.attrib.get("href")
                if not href or not self._is_relevant(title):
                    continue
                details.append(
                    {
                        "title": title,
                        "start": self._combine_date_time(
                            date, item.css(".calendar_eventtime::text").get()
                        ),
                        "source": self._clean_detail_url(href),
                    }
                )
        return details

    def _parse_detail(self, selector, title, start, source):
        """Build a Meeting from a calendar event detail page."""
        if not selector.css("ul.detail-list"):
            self.logger.warning(f"No event details found at {source}")
            return None
        title = (
            self._clean_text(selector.css(".detail-title span::text").get()) or title
        )
        detail_start, detail_end = self._parse_detail_times(selector)
        start = detail_start or start
        classification = self._parse_classification(title)
        meeting = Meeting(
            title=title,
            description="",
            classification=classification,
            start=start,
            end=detail_end,
            all_day=False,
            time_notes="",
            location=self._parse_location(selector),
            links=self._get_legistar_links(start, classification),
            source=source,
        )
        meeting["status"] = self._get_status(meeting)
        meeting["id"] = self._get_id(meeting)
        return meeting

    def _is_relevant(self, title):
        normalized = title.lower().replace("&", "and")
        return any(re.search(pattern, normalized) for pattern in self.MEETING_PATTERNS)

    def _parse_classification(self, title):
        title_lower = title.lower()
        if "council" in title_lower:
            return CITY_COUNCIL
        if "commission" in title_lower:
            return COMMISSION
        return COMMITTEE

    def _parse_day_date(self, day):
        """Parse the date from a day cell's aria-label, e.g.
        "Scheduled events, Tuesday, October 13, 2026"."""
        label = day.attrib.get("aria-label", "")
        match = re.search(r"([A-Za-z]+ \d{1,2}, \d{4})", label)
        if not match:
            return None
        return datetime.strptime(match.group(1), "%B %d, %Y").date()

    def _combine_date_time(self, date, time_str):
        time_str = self._clean_text(time_str)
        if time_str:
            try:
                time = datetime.strptime(time_str, "%I:%M %p").time()
                return datetime.combine(date, time)
            except ValueError:
                pass
        return datetime.combine(date, datetime.min.time())

    def _parse_detail_times(self, selector):
        """Parse the start and end from the detail page's date field, e.g.
        "10/13/2026 5:00 PM - 7:00 PM"."""
        value = self._detail_value(selector, "Date")
        text = self._clean_text(" ".join(value.xpath("./text()").getall()))
        match = re.search(
            r"(\d{1,2}/\d{1,2}/\d{4})\s+(\d{1,2}:\d{2}\s*[AP]M)"
            r"(?:\s*-\s*(?:(\d{1,2}/\d{1,2}/\d{4})\s+)?(\d{1,2}:\d{2}\s*[AP]M))?",
            text,
            flags=re.IGNORECASE,
        )
        if not match:
            return None, None
        start_date, start_time, end_date, end_time = match.groups()
        start = self._parse_datetime(start_date, start_time)
        end = self._parse_datetime(end_date or start_date, end_time)
        return start, end

    def _parse_datetime(self, date_str, time_str):
        if not date_str or not time_str:
            return None
        try:
            return datetime.strptime(
                f"{date_str} {time_str.upper().replace(' ', '')}", "%m/%d/%Y %I:%M%p"
            )
        except ValueError:
            return None

    def _parse_location(self, selector):
        value = self._detail_value(selector, "Location")
        if not value:
            return self.default_location
        name = self._clean_text(value.css("[itemprop='name']::text").get())
        street = self._clean_text(value.css("[itemprop='street-address']::text").get())
        locality = self._clean_text(value.css("[itemprop='locality']::text").get())
        region = self._clean_text(value.css("[itemprop='region']::text").get())
        address_text = self._clean_text(
            " ".join(value.css("[itemprop='address'] ::text").getall())
        )
        zip_match = re.search(r"\b\d{5}\b", address_text)

        address = ", ".join(part for part in [street, locality, region] if part)
        if address and zip_match:
            address = f"{address} {zip_match.group(0)}"
        if not address:
            address = address_text
        if not name and not address:
            # Location without itemprop markup (e.g. plain text)
            text = self._clean_text(" ".join(value.css("::text").getall()))
            return {"name": text, "address": ""} if text else self.default_location
        return {"name": name, "address": address}

    def _detail_value(self, selector, label):
        return selector.xpath(
            "//ul[contains(@class, 'detail-list')]/li"
            "[span[contains(@class, 'detail-list-label') and "
            f"contains(normalize-space(.), '{label}')]]"
            "/span[contains(@class, 'detail-list-value')]"
        )

    def _clean_detail_url(self, href):
        """Drop the curm/cury query string so the source URL is stable no
        matter which month page linked to it."""
        url = urljoin(self.base_url, href)
        return urlparse(url)._replace(query="", fragment="").geturl()

    def _clean_text(self, text):
        return re.sub(r"\s+", " ", text or "").strip()
