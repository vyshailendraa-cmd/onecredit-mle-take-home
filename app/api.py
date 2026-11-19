"""HTTP API for the "Similar companies" panel. Reads Postgres only; never loads the embedding model."""

import logging
from contextlib import asynccontextmanager
from typing import Annotated

import psycopg
from fastapi import FastAPI, Path, Query, Request
from fastapi.responses import JSONResponse
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from pydantic import BaseModel

from app import config
from app.events import InvalidEvent, normalise_registration
from app.state import ACTIVE

log = logging.getLogger("api")


class CompanyCard(BaseModel):
    """Everything one card in the mock-up needs. Clicking a card uses registration_number."""

    registration_number: str
    company_name: str | None
    industry: str | None
    state: str | None


class Company(CompanyCard):
    nature_of_business: str | None
    status: str


class SimilarCompany(CompanyCard):
    similarity: float


class SimilarResponse(BaseModel):
    company: Company
    similar: list[SimilarCompany]


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = config.load()
    pool = ConnectionPool(
        settings.database_url,
        min_size=1,
        max_size=10,
        open=False,
        configure=register_vector,
        kwargs={"row_factory": dict_row},
    )
    pool.open(wait=False)
    app.state.pool = pool
    yield
    pool.close()


app = FastAPI(title="OneCredit similar companies", lifespan=lifespan)


@app.exception_handler(ApiError)
async def api_error(_: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse({"error": {"code": exc.code, "message": exc.message}}, status_code=exc.status)


@app.exception_handler(psycopg.OperationalError)
@app.exception_handler(TimeoutError)
async def database_down(_: Request, exc: Exception) -> JSONResponse:
    log.error("database unavailable: %s", exc)
    return JSONResponse(
        {"error": {"code": "unavailable", "message": "Recommendations are temporarily unavailable."}},
        status_code=503,
    )


def _registration(value: str) -> str:
    try:
        return normalise_registration(value)
    except InvalidEvent:
        raise ApiError(
            400, "invalid_registration_number", "Expected a registration number like SYN-001550."
        ) from None


def _company(conn: psycopg.Connection, reg: str) -> dict:
    row = conn.execute(
        "SELECT registration_number, company_name, industry, state, nature_of_business, status, embedding"
        " FROM companies WHERE registration_number = %s",
        (reg,),
    ).fetchone()
    if row is None:
        raise ApiError(404, "company_not_found", f"No company with registration number {reg}.")
    return row


@app.get("/health")
def health(request: Request) -> dict:
    with request.app.state.pool.connection(timeout=2) as conn:
        conn.execute("SELECT 1")
    return {"status": "ok"}


@app.get("/companies/{registration_number}", response_model=Company)
def get_company(request: Request, registration_number: Annotated[str, Path()]) -> dict:
    with request.app.state.pool.connection(timeout=2) as conn:
        return _company(conn, _registration(registration_number))


@app.get("/companies/{registration_number}/similar", response_model=SimilarResponse)
def similar_companies(
    request: Request,
    registration_number: Annotated[str, Path()],
    limit: Annotated[int, Query(ge=1, le=20)] = 4,
) -> dict:
    """Most similar active companies by nature of business, most similar first."""
    reg = _registration(registration_number)
    with request.app.state.pool.connection(timeout=2) as conn:
        company = _company(conn, reg)
        # Index scan for a candidate pool (the vector is a parameter, not a subquery, so the HNSW index is used),
        # then a deterministic re-sort. Only ~170 distinct descriptions cover 2,000 companies, so exact ties
        # are common: break them by same industry, then registration number, so the panel doesn't reshuffle.
        rows = conn.execute(
            """
            SELECT registration_number, company_name, industry, state, 1 - distance AS similarity
            FROM (
                SELECT registration_number, company_name, industry, state, embedding <=> %(vector)s AS distance
                FROM companies
                WHERE registration_number <> %(reg)s AND status = %(active)s
                ORDER BY embedding <=> %(vector)s
                LIMIT %(pool)s
            ) AS candidates
            ORDER BY round(distance::numeric, 6), (industry IS NOT DISTINCT FROM %(industry)s) DESC,
                     registration_number
            LIMIT %(limit)s
            """,
            {
                "vector": company.pop("embedding"),
                "reg": reg,
                "active": ACTIVE,
                "industry": company["industry"],
                "pool": max(50, limit * 5),
                "limit": limit,
            },
        ).fetchall()
    return {"company": company, "similar": rows}
