from datetime import datetime
from os.path import dirname, join

import pytest
from city_scrapers_core.constants import CITY_COUNCIL, COMMISSION, COMMITTEE, PASSED
from city_scrapers_core.utils import file_response
from freezegun import freeze_time

from city_scrapers.spiders.sandie_oceanside import SandieOceansideSpider

test_response = file_response(
    join(dirname(__file__), "files", "sandie_oceanside.html"),
    url="https://www.ci.oceanside.ca.us/government/about-us/city-calendar/-curm-10/-cury-2026",  # noqa
)
test_detail_response = file_response(
    join(dirname(__file__), "files", "sandie_oceanside_detail.html"),
    url="https://www.ci.oceanside.ca.us/Home/Components/Calendar/Event/3642/16",
)
test_council_detail_response = file_response(
    join(dirname(__file__), "files", "sandie_oceanside_council_detail.html"),
    url="https://www.ci.oceanside.ca.us/Home/Components/Calendar/Event/2473/16",
)
test_legistar_response = file_response(
    join(dirname(__file__), "files", "sandie_oceanside_legistar.html"),
    url="https://oceanside.legistar.com/Calendar.aspx",
)

freezer = freeze_time("2026-10-22")
freezer.start()

spider = SandieOceansideSpider()

legistar_events = spider._parse_legistar_events(test_legistar_response)
list(spider.parse_legistar(legistar_events))

parsed_details = spider._parse_calendar(test_response)
detail = next(d for d in parsed_details if d["source"] == test_detail_response.url)
parsed_items = [spider._parse_detail(test_detail_response, **detail)]
council_detail = next(
    d for d in parsed_details if d["source"] == test_council_detail_response.url
)
council_items = [spider._parse_detail(test_council_detail_response, **council_detail)]

freezer.stop()

CHALLENGE_HTML = """
<html><head><script> var i = 1792696350; var j = i + Number("8912" + "10911");
</script></head><body><script>
xhr.open("POST", "/_sec/verify?provider=interstitial", false);
xhr.send(JSON.stringify({"bm-verify": "AAQAAAAO/////abc+def==", "pow": j}));
</script></body></html>
"""

COUNCIL_LINKS = [
    {
        "href": "https://oceanside.legistar.com/View.ashx?M=A&ID=1355285&GUID=7F18C6DC-8088-47E1-B8A2-18FD726F0C82",  # noqa
        "title": "Agenda",
    },
    {
        "href": "https://oceanside.legistar.com/View.ashx?M=M&ID=1355285&GUID=7F18C6DC-8088-47E1-B8A2-18FD726F0C82",  # noqa
        "title": "Minutes",
    },
    {
        "href": "https://oceanside.legistar.com/Video.aspx?Mode=Granicus&ID1=100&Mode2=Video",  # noqa
        "title": "Video",
    },
    {
        "href": "https://oceanside.legistar.com/View.ashx?M=E1&ID=1355285&GUID=7F18C6DC-8088-47E1-B8A2-18FD726F0C82",  # noqa
        "title": "Español",
    },
]


def test_start_requests():
    # Only Legistar is requested up front; the calendar follows on spider_idle
    assert [req.url for req in spider.start_requests()] == [
        "https://oceanside.legistar.com/Calendar.aspx"
    ]


def test_since_year():
    # Legistar years start at the beginning of the 3-year calendar window
    with freeze_time("2026-01-15"):
        assert SandieOceansideSpider().since_year == 2023
    with freeze_time("2026-10-22"):
        assert SandieOceansideSpider().since_year == 2023


def test_calendar_urls():
    # 3 years back through 6 months ahead
    with freeze_time("2026-10-22"):
        urls = list(spider._calendar_urls())
    assert len(urls) == 43
    assert urls[0] == (
        "https://www.ci.oceanside.ca.us/government/about-us/city-calendar/-curm-10/-cury-2023"  # noqa
    )
    assert urls[-1] == (
        "https://www.ci.oceanside.ca.us/government/about-us/city-calendar/-curm-4/-cury-2027"  # noqa
    )


def test_calendar_count():
    # Community events, Arts/Youth/Historic Preservation commissions, Library
    # Board, etc. are filtered out
    assert len(parsed_details) == 8


def test_calendar_titles():
    assert [d["title"] for d in parsed_details] == [
        "Joint City Council/CDC/Harbor/OPFA Regular Meeting",
        "Planning Commission",
        "Cancelled - Economic Development Commission Meeting",
        "Police and Fire Commission Meeting",
        "Joint City Council/CDC/Harbor/OPFA Regular Meeting",
        "Community Relations Commission Standing Commission",
        "Planning Commission",
        "Housing Commission",
    ]


def test_calendar_source():
    # Query string (?curm=10&cury=2026) is dropped
    assert (
        parsed_details[0]["source"]
        == "https://www.ci.oceanside.ca.us/Home/Components/Calendar/Event/2473/16"
    )


def test_calendar_start():
    assert parsed_details[0]["start"] == datetime(2026, 10, 7, 15, 30)
    assert parsed_details[-1]["start"] == datetime(2026, 10, 27, 18, 15)


def test_parse_detail_without_details():
    # Page without event details (e.g. unsolved Akamai challenge): no meeting
    blocked = file_response(
        join(dirname(__file__), "files", "sandie_oceanside.html"),
        url=test_detail_response.url,
    )
    assert spider._parse_detail(blocked, **detail) is None


def test_parse_challenge():
    assert spider._parse_challenge(CHALLENGE_HTML) == {
        "bm-verify": "AAQAAAAO/////abc+def==",
        "pow": 1792696350 + 891210911,
    }
    assert spider._parse_challenge("<html>No challenge</html>") is None


class FakeResponse:
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code


class FakeSession:
    """Serves the Akamai challenge until the solution is POSTed."""

    def __init__(self):
        self.verified = False
        self.posts = []

    def get(self, url, **kwargs):
        if self.verified:
            return FakeResponse("<ul class='detail-list'></ul>")
        return FakeResponse(CHALLENGE_HTML)

    def post(self, url, json=None, **kwargs):
        self.posts.append((url, json))
        self.verified = True
        return FakeResponse('{"reload": true}')


def test_fetch_solves_challenge():
    fetch_spider = SandieOceansideSpider()
    fetch_spider.request_delay = 0
    fetch_spider.session = FakeSession()
    html = fetch_spider._fetch_html(test_detail_response.url, referer="")
    assert "detail-list" in html
    assert fetch_spider.session.posts == [
        (
            "https://www.ci.oceanside.ca.us/_sec/verify?provider=interstitial",
            {"bm-verify": "AAQAAAAO/////abc+def==", "pow": 1792696350 + 891210911},
        )
    ]


def test_fetch_unsolved_challenge():
    fetch_spider = SandieOceansideSpider()
    fetch_spider.request_delay = 0
    fetch_spider.session = FakeSession()
    # Solution is POSTed but the challenge keeps coming back
    fetch_spider.session.post = lambda url, **kwargs: FakeResponse("")
    assert fetch_spider._fetch_html(test_detail_response.url, referer="") is None


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Police & Fire Commission Meeting", True),
        ("Police and Fire Commission Meeting", True),
        ("Utilities Commission Water-Sewer Committee Meeting", True),
        ("Water/Sewer Committee", True),
        ("Economic Development Commission", True),
        ("Planning Commission", True),
        ("Joint City Council/CDC/Harbor/OPFA Regular Meeting", True),
        ("Council Budget Workshop", True),
        ("Community Relations Commission", True),
        ("Housing Commission", True),
        ("Utilities Commission", True),
        ("Citizen Investment Oversight Committee Meeting", True),
        ("Arts Commission Meeting", False),
        ("Youth Commission", False),
        ("Downtown Advisory Committee", False),
        ("Halloween Trunk or Treat", False),
    ],
)
def test_is_relevant(title, expected):
    assert spider._is_relevant(title) is expected


def test_count():
    assert len(parsed_items) == 1


def test_title():
    assert parsed_items[0]["title"] == "Police and Fire Commission Meeting"


def test_description():
    assert parsed_items[0]["description"] == ""


def test_start():
    assert parsed_items[0]["start"] == datetime(2026, 10, 13, 17, 0)


def test_end():
    assert parsed_items[0]["end"] == datetime(2026, 10, 13, 19, 0)


def test_time_notes():
    assert parsed_items[0]["time_notes"] == ""


def test_id():
    assert (
        parsed_items[0]["id"]
        == "sandie_oceanside/202610131700/x/police_and_fire_commission_meeting"
    )


def test_status():
    assert parsed_items[0]["status"] == PASSED


def test_location():
    assert parsed_items[0]["location"] == {
        "name": "TBD",
        "address": "",
    }


def test_source():
    assert (
        parsed_items[0]["source"]
        == "https://www.ci.oceanside.ca.us/Home/Components/Calendar/Event/3642/16"
    )


def test_links():
    # Legistar only has City Council meetings
    assert parsed_items[0]["links"] == []


def test_council_links():
    assert council_items[0]["title"] == (
        "Joint City Council/CDC/Harbor/OPFA Regular Meeting"
    )
    assert council_items[0]["classification"] == CITY_COUNCIL
    assert council_items[0]["links"] == COUNCIL_LINKS


def test_legistar_events():
    assert len(legistar_events) == 23
    assert legistar_events[1]["Name"] == "City Council"
    assert legistar_events[1]["Meeting Date"] == "10/7/2026"
    assert legistar_events[1]["Meeting Time"] == "3:30 PM"


def test_legistar_links_not_available():
    # Future meeting: no minutes yet ("Not available" has no link)
    oct_21 = spider._get_legistar_links(datetime(2026, 10, 21, 15, 30), CITY_COUNCIL)
    assert [link["title"] for link in oct_21] == ["Agenda", "Video", "Español"]


def test_legistar_links_matching():
    # Same day, different time: falls back to the only Legistar meeting that day
    assert (
        spider._get_legistar_links(datetime(2026, 10, 7, 17, 0), CITY_COUNCIL)
        == COUNCIL_LINKS
    )
    # No Legistar meeting that day
    assert spider._get_legistar_links(datetime(2026, 10, 8, 15, 30), CITY_COUNCIL) == []
    # Only City Council meetings get Legistar links
    assert spider._get_legistar_links(datetime(2026, 10, 7, 15, 30), COMMISSION) == []


def test_legistar_skips_other_bodies():
    # Before 2025 Legistar lists other bodies too, e.g. on 11/1/2023
    legistar_spider = SandieOceansideSpider()
    legistar_spider.parse_legistar(
        [
            {
                "Name": "Parks and Recreation Commission",
                "Meeting Date": "11/1/2023",
                "Meeting Time": "6:00 PM",
                "Agenda": {"label": "Agenda", "url": "https://example.com/parks"},
            },
            {
                "Name": "City Council",
                "Meeting Date": "11/1/2023",
                "Meeting Time": "6:00 PM",
                "Agenda": {"label": "Agenda", "url": "https://example.com/council"},
            },
        ]
    )
    # Calendar says 3:30 PM: falls back to the only City Council meeting that day
    assert legistar_spider._get_legistar_links(
        datetime(2023, 11, 1, 15, 30), CITY_COUNCIL
    ) == [{"href": "https://example.com/council", "title": "Agenda"}]


def test_fetch_meeting_retries():
    retry_spider = SandieOceansideSpider()
    pages = [
        "<html>No details</html>",
        open(join(dirname(__file__), "files", "sandie_oceanside_detail.html")).read(),
    ]
    retry_spider._fetch_html = lambda url, referer: pages.pop(0)
    meeting = retry_spider._fetch_meeting(detail, referer="")
    assert meeting["title"] == "Police and Fire Commission Meeting"
    assert pages == []


def test_classification():
    assert parsed_items[0]["classification"] == COMMISSION


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Joint City Council/CDC/Harbor/OPFA Regular Meeting", CITY_COUNCIL),
        ("Housing Commission", COMMISSION),
        ("Citizen Investment Oversight Committee Meeting", COMMITTEE),
    ],
)
def test_parse_classification(title, expected):
    assert spider._parse_classification(title) == expected


def test_all_day():
    assert parsed_items[0]["all_day"] is False
