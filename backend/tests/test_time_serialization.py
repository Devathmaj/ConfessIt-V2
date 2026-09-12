"""Every timestamp the API sends must carry an explicit UTC offset.

``isoformat_datetime`` used to return ``value.isoformat()`` unchanged, so a naive datetime
serialised to something like ``"2026-09-28T12:01:36"`` with no zone at all. A browser reads
a bare timestamp as *its own* local time, so a UTC instant would render hours off for the
recipient, and no client-side fix can recover a zone that was never sent.

The database happens to hand back aware values, which is why this never surfaced. That is a
property of the driver rather than a guarantee of this function: code constructs naive
``datetime.now()`` values freely, and one of those reaching this helper is enough. These
tests pin the guarantee instead of the accident, and match the behaviour
``admin_service.serialize_datetime`` already had.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.utils import isoformat_datetime


class TestAlwaysCarriesAnOffset:
    def test_a_naive_datetime_is_read_as_utc(self):
        result = isoformat_datetime(datetime(2026, 9, 28, 12, 1, 36))

        assert result == "2026-09-28T12:01:36+00:00"

    def test_an_aware_datetime_keeps_its_instant(self):
        aware = datetime(2026, 9, 28, 12, 1, 36, tzinfo=timezone.utc)

        assert isoformat_datetime(aware) == "2026-09-28T12:01:36+00:00"

    def test_a_non_utc_datetime_is_converted_not_relabelled(self):
        """5:31 PM in IST is 12:01 UTC -- the same instant, expressed as UTC.

        Relabelling it as 17:31+00:00 would be a different moment entirely, which is the
        bug that put "12:00 PM" in front of a user whose match happened at 5:31 PM.
        """
        ist = timezone(timedelta(hours=5, minutes=30))
        aware = datetime(2026, 9, 28, 17, 31, 0, tzinfo=ist)

        result = isoformat_datetime(aware)

        assert result == "2026-09-28T12:01:00+00:00"
        assert datetime.fromisoformat(result) == aware

    def test_the_output_is_always_parseable_as_an_offset(self):
        samples = [
            datetime(2026, 1, 1, 0, 0, 0),
            datetime(2026, 12, 31, 23, 59, 59, tzinfo=timezone.utc),
            datetime(2026, 6, 1, 5, 0, 0, tzinfo=timezone(timedelta(hours=5, minutes=30))),
            "2026-03-01T12:00:00",
            "2026-03-01T12:00:00Z",
        ]

        for sample in samples:
            result = isoformat_datetime(sample)
            parsed = datetime.fromisoformat(result)
            assert parsed.tzinfo is not None, f"no offset emitted for {sample!r}"


class TestStringAndPassthrough:
    def test_a_bare_string_is_read_as_utc(self):
        assert isoformat_datetime("2026-09-28T12:01:36") == "2026-09-28T12:01:36+00:00"

    def test_a_zulu_string_keeps_its_instant(self):
        assert isoformat_datetime("2026-09-28T12:01:36Z") == "2026-09-28T12:01:36+00:00"

    def test_an_unparseable_string_is_returned_untouched(self):
        """Better a visible odd string than a None that blanks a timestamp in the UI."""
        assert isoformat_datetime("not a date") == "not a date"

    @pytest.mark.parametrize("value", [None, 123, [], {}])
    def test_non_timestamps_are_none(self, value):
        assert isoformat_datetime(value) is None
