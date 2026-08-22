"""
Blocking keys — the reason this finishes at all.

THE COMPLEXITY BUDGET
---------------------
The naive way to match source A against source B is to compare every record with
every other: O(n·m). At 50k x 50k that is 2.5 billion comparisons. It does not
finish, and no amount of tuning the comparison makes it finish.

Blocking fixes it by only ever comparing records that could plausibly match. We
bucket every record by a key built from properties a true match must share —
currency, direction, value date, and a coarse amount band — then compare only
within a bucket. Bucket sizes are bounded by how many transactions a merchant
does in a day within a ₹100 band, which is a small constant. The pass becomes
effectively O(n log n).

The trade-off is explicit and worth stating: **blocking can only lose matches, it
cannot create false ones.** If a true pair disagrees on its blocking key we will
never compare them and we will miss the match. That is an acceptable failure —
the pair becomes an honest exception. The opposite trade (compare everything,
match aggressively) risks false matches, which are far worse. So the keys are
chosen to be things a genuine pair really must agree on, and where they might
legitimately differ — a settlement landing T+2 after its ledger entry — we widen
the search deliberately by probing neighbouring buckets rather than by dropping
the key.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Iterator
from datetime import date, timedelta

from ledger.domain.models import CanonicalTransaction, Currency, Direction

# Width of an amount bucket, in minor units. ₹100 = 10_000 paise.
#
# Narrower buckets mean fewer comparisons but more probing when an amount sits
# near a boundary; wider buckets mean the reverse. ₹100 keeps same-day buckets
# small while needing only one neighbouring probe for the tolerances we allow.
AMOUNT_BAND_MINOR = 10_000

# The key: records that cannot share this cannot be the same money movement.
BlockKey = tuple[str, str, date, int]


def amount_band(amount_minor: int) -> int:
    return amount_minor // AMOUNT_BAND_MINOR


def block_key(
    currency: Currency, direction: Direction, value_date: date, amount_minor: int
) -> BlockKey:
    return (
        currency.value,
        direction.value,
        value_date,
        amount_band(amount_minor),
    )


class BlockingIndex:
    """Buckets records so only plausible candidates are ever compared.

    Build once, probe many times. Probing is a dict lookup per neighbouring
    bucket, so the cost per record is a small constant rather than a function of
    batch size.
    """

    def __init__(self, records: Iterable[CanonicalTransaction]) -> None:
        self._buckets: dict[BlockKey, list[CanonicalTransaction]] = defaultdict(list)
        for record in records:
            self._buckets[
                block_key(
                    record.currency,
                    record.direction,
                    record.value_date,
                    record.amount_minor,
                )
            ].append(record)

    def __len__(self) -> int:
        return sum(len(bucket) for bucket in self._buckets.values())

    @property
    def bucket_count(self) -> int:
        return len(self._buckets)

    @property
    def largest_bucket(self) -> int:
        """The worst-case comparison count for one probe. Watch this number.

        If it grows into the thousands the blocking key has stopped discriminating
        and the pass is drifting back toward quadratic.
        """
        return max((len(b) for b in self._buckets.values()), default=0)

    def candidates(
        self,
        *,
        currency: Currency,
        direction: Direction,
        value_date: date,
        amount_minor: int,
        date_offsets: Iterable[int] = (0,),
        band_offsets: Iterable[int] = (0,),
    ) -> Iterator[CanonicalTransaction]:
        """Yield every record in the neighbouring buckets worth comparing.

        `date_offsets` is how settlement lag is handled: a payout landing T+2 is
        in a bucket two days along, so we probe (0, 1, 2) rather than abandoning
        the date component of the key. `band_offsets` covers an amount sitting
        near a band boundary, or a fee-adjusted amount landing just below.
        """
        base = amount_band(amount_minor)
        for day_shift in date_offsets:
            shifted = value_date + timedelta(days=day_shift)
            for band_shift in band_offsets:
                key = (currency.value, direction.value, shifted, base + band_shift)
                yield from self._buckets.get(key, ())
