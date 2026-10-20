"""Thirty pages with known answers, for measuring how well the agent reads what it sees.

They cover the layouts shops and dashboards use: tables, spec lists, product cards, content
drawn by JavaScript after the page loads, shadow DOM, iframes, charts and settings screens,
plus pages that do not contain the answer, where the right reply is "it isn't there"."""

# ruff: noqa: E501
from dataclasses import dataclass


@dataclass(frozen=True)
class Case:
    name: str
    html: str
    question: str
    expected: tuple[str, ...] = ()  # every one must appear in the answer; empty: abstain
    decoys: tuple[str, ...] = ()  # figures on the page that would be wrong answers


def table(rows: list[tuple[str, ...]], head: tuple[str, ...]) -> str:
    cells = "".join(f"<th>{h}</th>" for h in head)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f"<table><thead><tr>{cells}</tr></thead><tbody>{body}</tbody></table>"


def late(content: str, seconds: float = 1.2) -> str:
    """Content drawn by script after the page has loaded, as single-page shops do."""
    return (
        "<div id=app>Loading…</div><script>setTimeout(() => { document.getElementById('app')"
        f".innerHTML = `{content}`; }}, {int(seconds * 1000)});</script>"
    )


TVS = table(
    [("Cirrus 55", "$549.00", "In stock"), ("Cirrus 65", "$799.50", "In stock"),
     ("Stratus 75", "$1,199.99", "Out of stock")],
    ("Model", "Price", "Stock"),
)  # fmt: skip

CASES: tuple[Case, ...] = (
    # tables
    Case("table-price", TVS, "What does the Cirrus 65 cost?", ("799.50",), ("549.00",)),
    Case("table-stock", TVS, "Which model is out of stock?", ("Stratus 75",)),
    Case("table-cheapest", TVS, "Which model is cheapest and at what price?", ("Cirrus 55", "549")),
    Case(
        "table-total",
        table(
            [("Pens", "3", "$2.00"), ("Paper", "1", "$8.50"), ("Total", "", "$14.50")],
            ("Item", "Qty", "Price"),
        ),
        "What is the order total?",
        ("14.50",),
        ("8.50",),
    ),
    Case(
        "table-merged-headers",
        "<table><tr><th rowspan=2>Plan</th><th colspan=2>Price per month</th></tr>"
        "<tr><th>Monthly</th><th>Yearly</th></tr>"
        "<tr><td>Basic</td><td>$12</td><td>$10</td></tr><tr><td>Pro</td><td>$30</td><td>$25</td></tr>"
        "</table>",
        "What does the Pro plan cost per month when billed yearly?",
        ("25",),
        ("30",),
    ),
    Case(
        "table-two-columns-same-name",
        table(
            [("Aurora", "4.5", "312"), ("Borealis", "3.9", "88")], ("Product", "Rating", "Reviews")
        ),
        "How many reviews does Borealis have?",
        ("88",),
        ("3.9",),
    ),
    # specification lists
    Case(
        "spec-list",
        "<dl><dt>Screen</dt><dd>65 inch</dd><dt>Resolution</dt><dd>3840 x 2160</dd>"
        "<dt>HDMI ports</dt><dd>4</dd><dt>Weight</dt><dd>21.4 kg</dd></dl>",
        "How many HDMI ports does it have?",
        ("4",),
        ("65", "21.4"),
    ),
    Case(
        "spec-weight",
        "<dl><dt>Screen</dt><dd>65 inch</dd><dt>Weight</dt><dd>21.4 kg</dd></dl>",
        "How much does it weigh?",
        ("21.4",),
        ("65",),
    ),
    Case(
        "spec-bullets",
        "<ul><li>Battery life: 14 hours</li><li>Charging: USB-C</li><li>Warranty: 2 years</li></ul>",
        "How long is the warranty?",
        ("2 years",),
        ("14",),
    ),
    Case(
        "spec-sentence",
        "<p>The Delta kettle holds 1.7 litres and boils in under 3 minutes. It is sold in "
        "white, black and red.</p>",
        "What colours is it sold in?",
        ("white", "black", "red"),
    ),
    # drawn by JavaScript after load
    Case(
        "js-price",
        late("<h1>Cirrus 65</h1><p class=price>$799.50</p>"),
        "What is the price?",
        ("799.50",),
    ),
    Case(
        "js-list",
        late("<ul><li>Aurora $19.99</li><li>Borealis $24.99</li><li>Corona $9.99</li></ul>"),
        "What is the cheapest item and its price?",
        ("Corona", "9.99"),
    ),
    Case(
        "js-slow",
        late("<h2>Delivery</h2><p>Arrives Thursday, November 19</p>", 2.5),
        "When does it arrive?",
        ("November 19",),
    ),
    Case(
        "js-stock",
        late("<p>Only <b>3</b> left at Eglinton store</p>"),
        "How many are left and where?",
        ("3", "Eglinton"),
    ),
    # shadow DOM
    Case(
        "shadow-price",
        "<div id=host></div><script>document.getElementById('host').attachShadow({mode:'open'})"
        ".innerHTML = '<h1>Aurora lamp</h1><p>Price: $39.95</p>';</script>",
        "What is the price of the lamp?",
        ("39.95",),
    ),
    Case(
        "shadow-rating",
        "<div id=h></div><script>document.getElementById('h').attachShadow({mode:'open'})"
        ".innerHTML = '<p>Rated 4.7 out of 5 by 1,204 shoppers</p>';</script>",
        "What is the rating?",
        ("4.7",),
    ),
    Case(
        "shadow-nested",
        "<div id=a></div><script>const r = document.getElementById('a').attachShadow({mode:'open'});"
        "r.innerHTML = '<div id=b></div>';"
        "r.getElementById('b').attachShadow({mode:'open'}).innerHTML = '<p>Serial number: ZX-4471</p>';"
        "</script>",
        "What is the serial number?",
        ("ZX-4471",),
    ),
    # iframes
    Case(
        "iframe-price",
        "<h1>Store</h1><iframe srcdoc='<p>Member price: $64.00</p>'></iframe>",
        "What is the member price?",
        ("64.00",),
    ),
    Case(
        "iframe-two",
        "<iframe srcdoc='<p>Order #1182</p>'></iframe><iframe srcdoc='<p>Status: shipped</p>'></iframe>",
        "What is the status of the order?",
        ("shipped",),
    ),
    # charts and dashboards
    Case(
        "chart-svg",
        "<svg width=300 height=120 role=img aria-label='Sales by month'>"
        "<title>Sales by month</title><text x=10 y=20>Oct: 120</text><text x=10 y=40>Nov: 185</text>"
        "<text x=10 y=60>Dec: 240</text></svg>",
        "What were December's sales?",
        ("240",),
        ("185",),
    ),
    Case(
        "chart-with-table",
        "<canvas width=300 height=100 aria-label='Visitors chart'></canvas>"
        + table([("Mon", "1,200"), ("Tue", "1,850")], ("Day", "Visitors")),
        "How many visitors on Tuesday?",
        ("1,850",),
        ("1,200",),
    ),
    Case(
        "dashboard-tiles",
        "<div><h3>Revenue</h3><p>$48,200</p></div><div><h3>Orders</h3><p>312</p></div>"
        "<div><h3>Refunds</h3><p>$1,150</p></div>",
        "How many orders were there?",
        ("312",),
        ("48,200",),
    ),
    # product cards
    Case(
        "cards-sale",
        "<article><h2>Mastercraft drill</h2><p>Was $159.99</p><p>Now $119.99</p></article>",
        "What is the current price?",
        ("119.99",),
        ("159.99",),
    ),
    Case(
        "cards-three",
        "<article><h2>Drill A</h2><p>$89.99</p></article><article><h2>Drill B</h2><p>$119.99</p>"
        "</article><article><h2>Drill C</h2><p>$74.99</p></article>",
        "Which drill is cheapest?",
        ("Drill C",),
        ("89.99",),
    ),
    # settings screens
    Case(
        "settings",
        "<form><label>Notifications <select><option>All</option><option selected>Mentions only"
        "</option></select></label><label>Language <input value='English'></label></form>",
        "What is the notification setting?",
        ("Mentions only",),
    ),
    Case(
        "settings-toggle",
        "<label><input type=checkbox checked> Two-step verification</label>"
        "<label><input type=checkbox> Marketing emails</label>",
        "Are marketing emails on?",
        ("not", "off"),
    ),
    # the answer is not on the page
    Case(
        "absent-price", "<h1>Cirrus 65</h1><p>Call the store for pricing.</p>", "What is the price?"
    ),
    Case(
        "absent-login",
        "<h1>Your orders</h1><p>Sign in to see your order history.</p>",
        "What was my last order?",
    ),
    Case(
        "absent-out-of-stock",
        "<h1>Stratus 75</h1><p>Currently unavailable.</p><p>Related: Cirrus 55 $549.00</p>",
        "What is the price of the Stratus 75?",
        (),
        ("549.00",),
    ),
    Case(
        "absent-spec",
        "<dl><dt>Screen</dt><dd>65 inch</dd><dt>HDMI ports</dt><dd>4</dd></dl>",
        "What is the refresh rate?",
        (),
        ("65", "4"),
    ),
)
