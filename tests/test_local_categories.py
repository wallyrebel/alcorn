import pytest

from rss_to_wp.local_categories import additional_local_categories


@pytest.mark.parametrize(
    "source",
    [
        "A meeting in Corinth, Mississippi, is set for Monday.",
        "Corinth MS residents are invited.",
        "The Corinth Police Department announced road closures.",
        "The Corinth Elks Lodge donated to student programs.",
        "Northeast Mississippi Community College students met in Corinth.",
        "<p>Corinth&nbsp;City Park hosts the event.</p>",
    ],
)
def test_explicit_local_rss_signals(source):
    assert additional_local_categories("https://alcornnewsms.com", "Update", source) == [221]


@pytest.mark.parametrize(
    "source",
    [
        "Corintheis Cullins appeared in Lee County court.",
        "Corinth Baptist Church in Tupelo held a picnic.",
        "The Corinth Police Department in Corinth, Texas announced an event.",
        "Corinth, Greece hosted a festival.",
        "Corinth Coca-Cola sponsored an unrelated school activity.",
        "An Alcorn County meeting is set for Monday.",
        "Mississippi schools announced a holiday.",
    ],
)
def test_ambiguous_unrelated_and_other_cities_not_routed(source):
    assert additional_local_categories("https://alcornnewsms.com", "Update", source) == []


def test_other_sites_not_changed():
    assert (
        additional_local_categories("https://anothernews.com", "Corinth, MS", "Community event.")
        == []
    )
