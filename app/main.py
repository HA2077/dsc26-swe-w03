"""Rawaj Pay transfer API. See README.md for the endpoints, errors and rules."""
import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select, update
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.db import make_engine, make_session_factory
from app.models import Account, Transfer
from app.settings import Settings

PROBLEM_JSON = "application/problem+json"

class ProblemError(Exception):
    def __init__(self, status: int, title: str, headers: dict[str, str] | None = None) -> None:
        self.status = status
        self.title = title
        self.headers = headers or {}


def problem_response(status: int, title: str, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse(
        {"type": "about:blank", "title": title, "status": status},
        status_code=status,
        media_type=PROBLEM_JSON,
        headers=headers,
    )


class TransferRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    from_account_id: str
    to_account_id: str
    amount_piastres: int = Field(gt=0)
    memo: str | None = Field(default=None, max_length=140)


def transfer_json(transfer: Transfer) -> dict[str, Any]:
    return {
        "id": transfer.id,
        "from_account_id": transfer.from_account_id,
        "to_account_id": transfer.to_account_id,
        "amount_piastres": transfer.amount_piastres,
        "memo": transfer.memo,
        "status": transfer.status,
    }


def authenticated_customer(request: Request) -> str:
    header = request.headers.get("Authorization")
    if header:
        scheme, _, token = header.partition(" ")
        token = token.strip()
        if scheme.lower() == "bearer" and token:
            customer = request.app.state.settings.api_tokens.get(token)
            if customer is not None:
                return customer
    raise ProblemError(401, "Unauthorized", {"WWW-Authenticate": "Bearer"})


async def validated_body(request: Request) -> TransferRequest:
    try:
        raw = await request.json()
        return TransferRequest.model_validate(raw)
    except (ValueError, ValidationError):
        raise ProblemError(422, "Unprocessable Entity")


def create_app(settings: Settings) -> FastAPI:
    """Return a new app wired to the given settings."""
    app = FastAPI()
    engine = make_engine(settings.database_url)
    app.state.engine = engine
    app.state.session_factory = make_session_factory(engine)
    app.state.settings = settings

    @app.exception_handler(ProblemError)
    async def handle_problem_error(request: Request, exc: ProblemError) -> JSONResponse:
        return problem_response(exc.status, exc.title, exc.headers)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return problem_response(422, "Unprocessable Entity")

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        titles = {404: "Not Found", 405: "Method Not Allowed"}
        return problem_response(exc.status_code, titles.get(exc.status_code, "Error"))

    @app.post("/v1/transfers", status_code=201)
    async def create_transfer(request: Request) -> JSONResponse:
        customer = authenticated_customer(request)
        if not request.headers.get("Idempotency-Key"):
            raise ProblemError(400, "Bad Request")
        payload = await validated_body(request)

        if payload.amount_piastres > settings.max_transfer_piastres:
            raise ProblemError(422, "Unprocessable Entity")
        if payload.from_account_id == payload.to_account_id:
            raise ProblemError(422, "Unprocessable Entity")

        with app.state.session_factory() as session:
            source = session.get(Account, payload.from_account_id)
            if source is None or source.owner_id != customer:
                raise ProblemError(404, "Not Found")
            if session.get(Account, payload.to_account_id) is None:
                raise ProblemError(422, "Unprocessable Entity")

            debit = session.execute(
                update(Account)
                .where(
                    Account.id == payload.from_account_id,
                    Account.owner_id == customer,
                    Account.balance_piastres >= payload.amount_piastres,
                )
                .values(balance_piastres=Account.balance_piastres - payload.amount_piastres)
            )
            if debit.rowcount != 1:
                session.rollback()
                raise ProblemError(409, "Conflict")
            session.execute(
                update(Account)
                .where(Account.id == payload.to_account_id)
                .values(balance_piastres=Account.balance_piastres + payload.amount_piastres)
            )
            transfer = Transfer(
                id="trf_" + uuid.uuid4().hex,
                from_account_id=payload.from_account_id,
                to_account_id=payload.to_account_id,
                amount_piastres=payload.amount_piastres,
                memo=payload.memo,
                status="completed",
            )
            session.add(transfer)
            session.commit()
            body = transfer_json(transfer)

        return JSONResponse(body, status_code=201)

    @app.get("/v1/accounts/{account_id}")
    def get_account(account_id: str, request: Request) -> JSONResponse:
        customer = authenticated_customer(request)
        with app.state.session_factory() as session:
            account = session.get(Account, account_id)
            if account is None or account.owner_id != customer:
                raise ProblemError(404, "Not Found")
            body = {
                "id": account.id,
                "balance_piastres": account.balance_piastres,
                "currency": account.currency,
            }
        return JSONResponse(body)

    @app.get("/v1/transfers/{transfer_id}")
    def get_transfer(transfer_id: str, request: Request) -> JSONResponse:
        customer = authenticated_customer(request)
        with app.state.session_factory() as session:
            transfer = session.get(Transfer, transfer_id)
            if transfer is None:
                raise ProblemError(404, "Not Found")
            owners = {
                session.scalar(select(Account.owner_id).where(Account.id == transfer.from_account_id)),
                session.scalar(select(Account.owner_id).where(Account.id == transfer.to_account_id)),
            }
            if customer not in owners:
                raise ProblemError(404, "Not Found")
            body = transfer_json(transfer)
        return JSONResponse(body)

    return app