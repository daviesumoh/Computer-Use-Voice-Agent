from worker.browser import snapshot

SAMPLE = """- generic [active] [ref=e1]:
  - heading "Shop" [level=1] [ref=e2]
  - link "Deals \\"50% off\\"" [ref=e3] [cursor=pointer]:
    - /url: /x
  - button "Pay" [disabled] [ref=e5]
  - iframe [ref=e8]:
    - button "In frame" [ref=f1e2]
  - paragraph: no ref here
"""


def test_parse_reads_roles_names_flags_and_nested_frame_refs() -> None:
    nodes = snapshot.parse(SAMPLE)
    assert nodes["e3"] == snapshot.Node("e3", "link", 'Deals "50% off"')
    assert nodes["e5"].disabled
    assert nodes["f1e2"].name == "In frame"
    assert "e1" in nodes and nodes["e8"].role == "iframe"
    assert len(nodes) == 6  # lines without a ref are ignored


def test_find_same_matches_on_role_and_name_only() -> None:
    nodes = snapshot.parse(
        '- link "Details" [ref=e1]\n- link "Details" [ref=e2]\n- button "Details" [ref=e3]'
    )
    found = snapshot.find_same(nodes, snapshot.Node("e9", "link", "Details"))
    assert [n.ref for n in found] == ["e1", "e2"]


def test_long_snapshots_are_cut_with_a_marker() -> None:
    cut = snapshot.truncate("x" * (snapshot.MAX_SNAPSHOT_CHARS + 50))
    assert cut.endswith("extract to see more]")
    assert len(cut) < snapshot.MAX_SNAPSHOT_CHARS + 100
    assert snapshot.truncate("short") == "short"


CARD = """\
- link "Cirrus 65 TV $799.50 was $999 Only 3 left" [ref=e1] [cursor=pointer]:
  - /url: /product/2?rq=tv&utm=track
  - generic [ref=e2]:
    - generic [ref=e3]: Cirrus 65 TV
    - generic [ref=e4]: $799.50
    - text: was $999
    - paragraph [ref=e5]: 4.5 out of 5 stars
  - button "Add to cart" [ref=e6]
"""


def test_compacting_drops_what_a_names_ancestor_already_says_and_keeps_the_rest() -> None:
    compact = snapshot.compact(CARD)

    assert "Cirrus 65 TV $799.50 was $999 Only 3 left" in compact  # the link's own name stays
    assert compact.count("$799.50") == 1 and compact.count("was $999") == 1
    assert "4.5 out of 5 stars" in compact  # new information stays
    assert "/url: /product/2\n" in compact + "\n"  # tracking query removed
    assert "cursor=pointer" not in compact
    assert "- generic [ref=e2]:" not in compact  # an empty wrapper


def test_compacting_never_loses_an_element_the_model_could_act_on() -> None:
    before = {n.ref for n in snapshot.parse(CARD).values() if n.role in snapshot.INTERACTIVE_ROLES}
    after = {
        n.ref for n in snapshot.parse(snapshot.compact(CARD)).values()
        if n.role in snapshot.INTERACTIVE_ROLES
    }  # fmt: skip

    assert before == after == {"e1", "e6"}


def test_compacting_shortens_boilerplate_but_not_product_names() -> None:
    boilerplate = "- paragraph [ref=e9]: " + "terms and conditions " * 40
    long_name = "x" * 250
    product = f'- link "{long_name} $19.99" [ref=e10]'

    compact = snapshot.compact(boilerplate + "\n" + product)

    first, second = compact.splitlines()
    assert len(first) < 250 and first.endswith("…")
    assert second == product  # 250 + price is under the name limit: untouched
