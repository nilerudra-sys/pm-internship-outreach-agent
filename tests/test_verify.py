"""Sentence-level fact verification, using text modelled on a real fintech homepage."""

import pytest

from services.research.verify import check_multi, check_support, locate_quote, name_tokens, tokens

SOURCE = """Online platform to invest in stocks, IPOs, derivatives, mutual funds, ETFs, bonds, and more.
Trust with confidence
That's why 1.8+ crore customers trust Acmetrade with ~ ₹9 lakh crores
of equity investments, making us India's largest broker; contributing to 15% of daily retail exchange volumes in India.
No gimmicks, spam, "gamification", or annoying push notifications. High quality apps that you use at your pace.
Coin
Buy direct mutual funds online, commission-free, delivered directly to your Demat account.
Kite
Our ultra-fast flagship trading platform with streaming market data, advanced charts, an elegant UI, and more.
If you are a registered intermediary (broker, DP, Mutual Fund etc.), you need not undertake KYC again.
Free and open market education
Our office is at J.P Nagar 4th Phase, Bengaluru - 560078, Karnataka, India."""

IGNORE = name_tokens("Acmetrade", "acmetrade.com")


@pytest.mark.parametrize("fact", [
    "Acmetrade serves over 1.8 crore clients",
    "Acmetrade does not send annoying push notifications",
    "Coin lets users buy direct mutual funds online without commission",
    "Kite is a flagship trading platform with streaming market data and advanced charts",
    "Acmetrade contributes 15% of daily retail exchange volumes in India",
    "The office is in Bengaluru, Karnataka",
])
def test_true_facts_pass(fact):
    s = check_support(fact, SOURCE, IGNORE)
    assert s.ok, s.reason
    assert s.quote


@pytest.mark.parametrize("fact, reason", [
    ("Acmetrade serves over 18 crore clients", "number"),
    ("Acmetrade serves 9 crore clients", "number"),                         # 9 belongs to "lakh crores"
    ("Acmetrade has 15 lakh customers", "number"),
    ("Acmetrade does not offer mutual funds", "negat"),                       # "need not" is about KYC
    ("Acmetrade charges commission on direct mutual funds", ""),             # source says commission-free
    ("Acmetrade sends gamified push notifications", ""),                     # source says "No ... push notifications"
    ("Acmetrade is hiring product interns in Bengaluru", "claim"),
    ("Acmetrade is a fully remote company across India", "claim"),
    ("Acmetrade recently launched a new trading app", "claim"),
    ("Acmetrade raised a Series B round from Sequoia", "claim"),
    ("Acmetrade opened an office in London", ""),
])
def test_fabricated_or_distorted_facts_fail(fact, reason):
    s = check_support(fact, SOURCE, IGNORE)
    assert not s.ok
    assert reason in s.reason


def test_company_name_alone_proves_nothing():
    assert not check_support("Acmetrade Acmetrade.com", SOURCE, IGNORE).ok


def test_whole_word_matching():
    assert "intern" not in set(tokens("international internet"))
    assert not check_support("Acmetrade hires interns internationally", "We have international customers.", IGNORE).ok


def test_verbatim_quote_is_located_with_neighbours():
    windows = locate_quote("Buy direct mutual funds online, commission-free", SOURCE)
    assert windows and "Coin" in windows[1]
    assert locate_quote("a sentence that is not in the source at all", SOURCE) == []


def test_multi_source():
    ok, quotes, _ = check_multi("Customers trust the platform and pay zero brokerage on delivery",
                                [("P1", "1.8 crore customers trust our platform."),
                                 ("P2", "Pay zero brokerage on equity delivery trades.")], IGNORE)
    assert ok and [q["source_id"] for q in quotes] == ["P1", "P2"]
    ok, _, reason = check_multi("Customers trust the platform and pay zero brokerage on delivery",
                                [("P1", "1.8 crore customers trust our platform."),
                                 ("P2", "Our office is in Bengaluru.")], IGNORE)
    assert not ok and "P2" in reason
