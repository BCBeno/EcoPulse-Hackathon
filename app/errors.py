"""One error shape for every failure: {"error": {"code", "message", "fields"?}} with Romanian messages."""

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger("ecopulse.errors")


class FieldError(BaseModel):
    field: str
    message: str


class ErrorBody(BaseModel):
    code: str
    message: str
    fields: list[FieldError] | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody


class AppError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        fields: list[FieldError] | None = None,
        headers: dict[str, str] | None = None,
    ):
        super().__init__(code)
        self.status, self.code, self.message = status, code, message
        self.fields, self.headers = fields, headers


def not_found(message: str = "Nu am găsit ce ai cerut.") -> AppError:
    return AppError(404, "not_found", message)


def forbidden(message: str) -> AppError:
    return AppError(403, "forbidden", message)


def conflict(message: str) -> AppError:
    return AppError(409, "conflict", message)


def invalid(field: str, message: str) -> AppError:
    return AppError(422, "validation_error", message, fields=[FieldError(field=field, message=message)])


def outside_city() -> AppError:
    return AppError(422, "outside_city", "Locația e în afara zonei acoperite de EcoPulse (Iași).")


VALIDATION_MESSAGES = {
    "missing": "Câmpul este obligatoriu.",
    "string_too_short": "Textul este prea scurt.",
    "string_too_long": "Textul este prea lung.",
    "too_short": "Prea puține elemente.",
    "too_long": "Prea multe elemente.",
    "greater_than": "Valoarea este prea mică.",
    "greater_than_equal": "Valoarea este prea mică.",
    "less_than": "Valoarea este prea mare.",
    "less_than_equal": "Valoarea este prea mare.",
    "literal_error": "Valoare nepermisă.",
    "enum": "Valoare nepermisă.",
    "float_parsing": "Trebuie să fie un număr.",
    "float_type": "Trebuie să fie un număr.",
    "int_parsing": "Trebuie să fie un număr întreg.",
    "int_type": "Trebuie să fie un număr întreg.",
    "finite_number": "Trebuie să fie un număr finit.",
    "uuid_parsing": "Identificator invalid.",
    "datetime_parsing": "Data și ora nu sunt valide.",
    "datetime_from_date_parsing": "Data și ora nu sunt valide.",
    "timezone_aware": "Data și ora trebuie să includă fusul orar.",
    "json_invalid": "Corpul cererii nu este JSON valid.",
    "model_attributes_type": "Format invalid.",
    "dict_type": "Format invalid.",
    "extra_forbidden": "Câmp necunoscut.",
}


def _error(status: int, code: str, message: str, fields=None, headers=None) -> JSONResponse:
    body = ErrorResponse(error=ErrorBody(code=code, message=message, fields=fields))
    return JSONResponse(body.model_dump(exclude_none=True), status_code=status, headers=headers)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def app_error(request: Request, exc: AppError):
        return _error(exc.status, exc.code, exc.message, exc.fields, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        fields = []
        for item in exc.errors():
            location = [str(part) for part in item.get("loc", ()) if part not in ("body", "query", "path")]
            message = VALIDATION_MESSAGES.get(item.get("type", ""), "Valoare invalidă.")
            if item.get("type") == "value_error":
                # Our own validators raise ValueError with a Romanian message.
                message = str(item.get("ctx", {}).get("error", message))
            fields.append(FieldError(field=".".join(location) or "body", message=message))
        return _error(422, "validation_error", "Datele trimise nu sunt valide.", fields)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        codes = {
            401: ("unauthorized", "Autentificare necesară."),
            403: ("forbidden", "Acțiune nepermisă."),
            404: ("not_found", "Nu am găsit ce ai cerut."),
            405: ("method_not_allowed", "Metodă nepermisă."),
        }
        code, message = codes.get(exc.status_code, ("http_error", "Cererea nu a putut fi procesată."))
        return _error(exc.status_code, code, message, headers=getattr(exc, "headers", None))

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception):
        log.exception("Unhandled error on %s %s", request.method, request.url.path)
        return _error(500, "internal_error", "A apărut o eroare neașteptată. Încearcă din nou.")
