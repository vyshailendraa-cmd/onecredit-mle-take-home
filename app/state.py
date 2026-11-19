"""Fold a company's events into its current state.

The fold is a pure function of the *set* of events: it sorts by the source timestamp first, so the result
doesn't depend on delivery order or on how many times a message was redelivered. That one property is what
makes the consumer safe under at-least-once, out-of-order delivery.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

from app.events import STRUCK_OFF, Event

ACTIVE = "active"
STRUCK = "struck_off"


@dataclass
class CompanyState:
    registration_number: str
    company_name: str | None = None
    nature_of_business: str | None = None
    industry: str | None = None
    state: str | None = None
    status: str = ACTIVE
    updated_at: datetime | None = None
    sources: int = field(default=0)

    def embedding_text(self) -> str:
        """What we embed. Nature of business is the signal; fall back so no company is left without a vector."""
        if self.nature_of_business:
            return self.nature_of_business
        fallback = " ".join(p for p in (self.industry, self.company_name) if p)
        return fallback or self.registration_number


def fold(registration_number: str, events: Iterable[Event]) -> CompanyState:
    company = CompanyState(registration_number)
    # Ties on updated_at never conflict in this feed; sorting by the field values makes ties deterministic anyway.
    ordered = sorted(events, key=lambda e: (e.updated_at, e.type, sorted(e.fields.items())))
    for event in ordered:
        if event.type == STRUCK_OFF:
            company.status = STRUCK
        else:
            # A newer upsert means the registry still lists the company.
            company.status = ACTIVE
            for column, value in event.fields.items():
                setattr(company, column, value)
        company.updated_at = event.updated_at
        company.sources += 1
    return company
