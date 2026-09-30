"""Company registry maintenance (manual adds from the UI/CLI)."""

from sqlmodel import Session, select

from recrute.models import Company
from recrute.sources import ATS_BOARD_SOURCES, parse_ats_url


def add_company_from_url(session: Session, url: str, name: str | None = None) -> Company:
    ref = parse_ats_url(url)
    if ref is None or not ref.token:
        raise ValueError("not a recognizable job-board URL")
    if ref.ats not in ATS_BOARD_SOURCES:
        raise ValueError(f"{ref.ats} boards can't be polled automatically yet "
                         f"(supported: {', '.join(ATS_BOARD_SOURCES)})")
    existing = session.exec(select(Company).where(Company.ats == ref.ats,
                                                  Company.ats_token == ref.token)).first()
    if existing is not None:
        existing.active = True
        if name:
            existing.name = name
        session.add(existing)
        session.commit()
        return existing
    company = Company(name=name or ref.token.replace("-", " ").title(), ats=ref.ats,
                      ats_token=ref.token, origin="manual")
    session.add(company)
    session.commit()
    session.refresh(company)
    return company
