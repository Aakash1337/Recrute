import pytest

from recrute.location import admits_us
from recrute.pipeline.filter import is_us_location
from recrute.sources.util import us_eligible


@pytest.mark.parametrize("locs,expected", [
    (["South America"], False),
    (["Latin America"], False),
    (["Remote - Central America"], False),
    (["Remote, South America"], False),
    (["US or Latin America"], True),
    (["Latin America", "New York, NY"], True),
    (["Americas"], True),
    (["North America"], True),
    (["London, UK", "San Francisco"], None),
    (["London, UK", "Berlin"], False),
    (["Remote"], None),
])
def test_location_classifier_is_shared(locs, expected):
    assert admits_us(locs) is expected
    assert us_eligible(locs) is expected
    assert is_us_location(locs, None) is expected
