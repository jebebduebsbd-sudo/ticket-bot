"""Unit tests for the pure logic that decides pricing, sourcing and review tags.

These run without Discord or a database — they cover the bits most likely to
regress and quietly charge the wrong price or mis-handle a review.
"""


# ---- resale price -----------------------------------------------------------
def test_resale_price_rounds_up_to_whole_euro(bot):
    src, resale = bot._resale_price({"price": 10})
    assert src == 10.0
    assert resale == 25.0  # 10 * 2.5 exactly


def test_resale_price_ceils_fraction(bot):
    src, resale = bot._resale_price({"price": 4.1})
    assert src == 4.1
    assert resale == 11.0  # 4.1 * 2.5 = 10.25 → ceil to 11


def test_resale_price_zero(bot):
    assert bot._resale_price({}) == (0.0, 0.0)


# ---- sourcing markup (2.5x normal, 3x for high budgets) ---------------------
def test_sourcing_markup_normal_budget(bot):
    assert bot._sourcing_markup(10) == 2.5
    assert bot._sourcing_markup(29.99) == 2.5


def test_sourcing_markup_high_budget(bot):
    assert bot._sourcing_markup(30) == 3.0   # threshold inclusive
    assert bot._sourcing_markup(100) == 3.0


def test_sourcing_markup_none(bot):
    assert bot._sourcing_markup(None) == 2.5
    assert bot._sourcing_markup(0) == 2.5


# ---- "most stacked" ranking metric -----------------------------------------
def test_spent_metric_fortnite_weights_skins(bot):
    many_skins = {"fortnite_skin_count": 200, "fortnite_shop_skins_cost": 0}
    few_skins = {"fortnite_skin_count": 50, "fortnite_shop_skins_cost": 5000}
    assert bot._spent_metric("fortnite", many_skins) > bot._spent_metric("fortnite", few_skins)


def test_spent_metric_valorant_uses_inventory_value(bot):
    a = {"riot_valorant_inventory_value": 5000}
    b = {"riot_valorant_inventory_value": 1000}
    assert bot._spent_metric("valorant", a) > bot._spent_metric("valorant", b)


# ---- +rep / -rep detection --------------------------------------------------
def test_plus_rep_detection(bot):
    for text in ["+rep great seller", "+ rep thanks", "amazing +rep!!"]:
        assert bot._PLUS_REP_RE.search(text), text


def test_minus_rep_detection(bot):
    for text in ["-rep slow", "- rep bad", "meh -rep"]:
        assert bot._MINUS_REP_RE.search(text), text


def test_no_false_positive_rep(bot):
    # Ordinary words containing "rep" must not count as a review tag.
    for text in ["prepare a report", "representative", "reputation", "repeat that"]:
        assert not bot._PLUS_REP_RE.search(text), text
        assert not bot._MINUS_REP_RE.search(text), text
