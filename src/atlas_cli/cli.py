"""Atlas command-line entry point."""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer
from pydantic import ValidationError
from typer._click.exceptions import UsageError
from typer.core import TyperGroup

from atlas_cli import __version__
from atlas_cli.access import AccessManager
from atlas_cli.api_client import AtlasApiClient
from atlas_cli.auth import AuthService
from atlas_cli.booking_runtime import BookingRuntime
from atlas_cli.booking_runtime import build_booking_runtime as compose_booking_runtime
from atlas_cli.business_client import AtlasBusinessClient
from atlas_cli.config import ConfigStore, InternalSettings
from atlas_cli.doctor import DoctorService
from atlas_cli.endpoints import CustomerMode, EndpointResolver
from atlas_cli.logging_config import configure_logging
from atlas_cli.models import CommandResult, success_result, terminal_error_result
from atlas_cli.output import OutputWriter
from atlas_cli.passengers import PassengerSource
from atlas_cli.search import SearchService
from atlas_cli.search_adapters import BookingSearchAdapter, FareSearchAdapter
from atlas_cli.search_models import SearchRequest
from atlas_cli.search_store import SearchStore
from atlas_cli.secure_store import KeyringSecretStore


class AgentFriendlyGroup(TyperGroup):
    def main(
        self,
        args: Sequence[str] | None = None,
        prog_name: str | None = None,
        complete_var: str | None = None,
        standalone_mode: bool = True,
        windows_expand_args: bool = True,
        **extra: Any,
    ) -> Any:
        arguments = list(args) if args is not None else sys.argv[1:]
        json_requested = "--json" in arguments
        try:
            result = super().main(
                args=arguments,
                prog_name=prog_name,
                complete_var=complete_var,
                standalone_mode=False,
                windows_expand_args=windows_expand_args,
                **extra,
            )
        except UsageError as error:
            if not json_requested:
                if standalone_mode:
                    error.show()
                    raise SystemExit(error.exit_code) from error
                raise
            invalid = terminal_error_result(
                "INVALID_ARGUMENT",
                "Invalid command arguments",
                details={"argument": "invalid"},
            )
            exit_code = OutputWriter().write(invalid, json_output=True)
            raise SystemExit(exit_code) from None
        except Exception:
            if not json_requested:
                raise
            failure = terminal_error_result(
                "INTERNAL_ERROR",
                "Atlas Flight Booking CLI could not complete the request",
            )
            exit_code = OutputWriter().write(failure, json_output=True)
            raise SystemExit(exit_code) from None
        if standalone_mode and isinstance(result, int) and result != 0:
            raise SystemExit(result)
        return result


app = typer.Typer(add_completion=False, cls=AgentFriendlyGroup)
auth_app = typer.Typer(add_completion=False)
offer_app = typer.Typer(add_completion=False)
booking_app = typer.Typer(add_completion=False)
baggage_app = typer.Typer(add_completion=False)
seat_app = typer.Typer(add_completion=False)
order_app = typer.Typer(add_completion=False)
environment_app = typer.Typer(add_completion=False)
app.add_typer(auth_app, name="auth")
app.add_typer(offer_app, name="offer")
app.add_typer(booking_app, name="booking")
booking_app.add_typer(baggage_app, name="baggage")
booking_app.add_typer(seat_app, name="seat")
app.add_typer(order_app, name="order")
app.add_typer(environment_app, name="environment", hidden=True)


@app.callback(invoke_without_command=True)
def main(
    version: bool = typer.Option(
        False,
        "--version",
        is_eager=True,
        help="Show the Atlas Flight Booking CLI version.",
    ),
) -> None:
    if version:
        typer.echo(f"atlas-flight {__version__}")
        raise typer.Exit()


def current_customer_mode() -> CustomerMode:
    return CustomerMode(ConfigStore().load_customer_mode())


def build_auth_service() -> AuthService:
    configure_logging()
    settings = InternalSettings()
    api = AtlasApiClient(settings)
    secrets = KeyringSecretStore()
    mode = current_customer_mode()
    return AuthService(
        api=api,
        secrets=secrets,
        settings=settings,
        cli_version=__version__,
        customer_mode=mode,
        credential_synchronizer=AccessManager(
            api=api,
            secrets=secrets,
            resolver=EndpointResolver(settings),
            mode=mode,
        ),
    )


def build_doctor_service() -> DoctorService:
    configure_logging()
    settings = InternalSettings()
    api = AtlasApiClient(settings)
    secrets = KeyringSecretStore()
    mode = current_customer_mode()
    auth = AuthService(
        api=api,
        secrets=secrets,
        settings=settings,
        cli_version=__version__,
        customer_mode=mode,
        credential_synchronizer=AccessManager(
            api=api,
            secrets=secrets,
            resolver=EndpointResolver(settings),
            mode=mode,
        ),
    )
    return DoctorService(
        config=ConfigStore(),
        secrets=secrets,
        api=api,
        auth=auth,
        cli_version=__version__,
    )


def build_search_service() -> SearchService:
    configure_logging()
    settings = InternalSettings()
    control_api = AtlasApiClient(settings)
    secrets = KeyringSecretStore()
    access = AccessManager(
        api=control_api,
        secrets=secrets,
        resolver=EndpointResolver(settings),
        mode=current_customer_mode(),
    )
    business = AtlasBusinessClient(settings)
    return SearchService(
        secrets=secrets,
        access=access,
        fare_adapter=FareSearchAdapter(business),
        booking_adapter=BookingSearchAdapter(business),
        store=SearchStore(secrets=secrets),
    )


def build_booking_runtime() -> BookingRuntime:
    return compose_booking_runtime(mode=current_customer_mode())


def _write_result(result: CommandResult, *, json_output: bool) -> NoReturn:
    exit_code = OutputWriter().write(result, json_output=json_output)
    raise typer.Exit(code=exit_code)


@environment_app.command("use")
def environment_use(
    target: str = typer.Argument(...),
    json_output: bool = typer.Option(False, "--json", help="Emit one JSON result object."),
) -> None:
    selected = {"production": "prod", "prod": "prod", "sandbox": "sandbox"}.get(target.strip().lower())
    if selected is None:
        _write_result(
            terminal_error_result(
                "INVALID_ARGUMENT",
                "Invalid configuration target",
                details={"field": "target"},
            ),
            json_output=json_output,
        )
    ConfigStore().save_customer_mode(selected)
    _write_result(
        success_result(
            "CONFIGURATION_UPDATED",
            "Atlas configuration updated",
            data={},
        ),
        json_output=json_output,
    )


@auth_app.command("login")
def auth_login(
    json_output: bool = typer.Option(False, "--json", help="Emit one JSON result object."),
) -> None:
    _write_result(build_auth_service().login(), json_output=json_output)


@auth_app.command("status")
def auth_status(
    json_output: bool = typer.Option(False, "--json", help="Emit one JSON result object."),
) -> None:
    _write_result(build_auth_service().status(), json_output=json_output)


@auth_app.command("poll")
def auth_poll(
    timeout: str = typer.Option("120", "--timeout", help="Maximum polling time in seconds."),
    json_output: bool = typer.Option(False, "--json", help="Emit one JSON result object."),
) -> None:
    try:
        timeout_seconds = int(timeout)
    except ValueError:
        timeout_seconds = 0
    if not 1 <= timeout_seconds <= 120:
        result = terminal_error_result(
            "INVALID_ARGUMENT",
            "Timeout must be an integer from 1 through 120",
            details={"field": "timeout"},
        )
        _write_result(result, json_output=json_output)
    _write_result(build_auth_service().poll(timeout_seconds), json_output=json_output)


@app.command("doctor")
def doctor(
    json_output: bool = typer.Option(False, "--json", help="Emit one JSON result object."),
) -> None:
    _write_result(build_doctor_service().run(), json_output=json_output)


@app.command("search")
def flight_search(
    origin: str | None = typer.Option(None, "--origin", help="Origin city or airport IATA code."),
    destination: str | None = typer.Option(None, "--destination", help="Destination city or airport IATA code."),
    depart: str | None = typer.Option(None, "--depart", help="Departure date in YYYY-MM-DD format."),
    adults: int | None = typer.Option(None, "--adults", help="Adult passenger count."),
    return_date: str | None = typer.Option(None, "--return-date", help="Return date in YYYY-MM-DD format."),
    children: int = typer.Option(0, "--children", help="Child passenger count."),
    infants: int = typer.Option(0, "--infants", help="Infant passenger count."),
    airline: Annotated[
        list[str] | None,
        typer.Option("--airline", help="Airline IATA filter; repeat as needed."),
    ] = None,
    currency: str | None = typer.Option(None, "--currency", help="Settlement currency."),
    multiple_fare_families: bool = typer.Option(
        False,
        "--multiple-fare-families",
        help="Return multiple fare families.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit one JSON result object."),
) -> None:
    core_values = (origin, destination, depart, adults)
    has_any_input = any(value is not None for value in core_values) or any(
        (
            return_date is not None,
            children != 0,
            infants != 0,
            bool(airline),
            currency is not None,
            multiple_fare_families,
        )
    )
    request: SearchRequest | None = None
    if has_any_input:
        if any(value is None for value in core_values):
            _write_result(
                terminal_error_result(
                    "INVALID_ARGUMENT",
                    "Origin, destination, departure date, and adults are required",
                    details={"field": "search"},
                ),
                json_output=json_output,
            )
        try:
            request = SearchRequest.model_validate(
                {
                    "origin": origin,
                    "destination": destination,
                    "depart": depart,
                    "adults": adults,
                    "return_date": return_date,
                    "children": children,
                    "infants": infants,
                    "airlines": tuple(airline or ()),
                    "currency": currency,
                    "include_multiple_fare_families": multiple_fare_families,
                }
            )
        except ValidationError:
            _write_result(
                terminal_error_result(
                    "INVALID_ARGUMENT",
                    "Invalid flight search arguments",
                    details={"field": "search"},
                ),
                json_output=json_output,
            )
    _write_result(build_search_service().search(request), json_output=json_output)


@offer_app.command("list")
def offer_list(
    search_id: str = typer.Option(..., "--search-id", help="Opaque search identifier."),
    json_output: bool = typer.Option(False, "--json", help="Emit one JSON result object."),
) -> None:
    _write_result(build_search_service().list_offers(search_id), json_output=json_output)


@offer_app.command("verify")
def offer_verify(
    offer_id: str = typer.Option(..., "--offer-id"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    _write_result(build_booking_runtime().verify.verify(offer_id), json_output=json_output)


@booking_app.command("confirm-price")
def booking_confirm_price(
    booking_id: str = typer.Option(..., "--booking-id"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    _write_result(build_booking_runtime().verify.confirm_price(booking_id), json_output=json_output)


@order_app.command("create")
def order_create(
    booking_id: str = typer.Option(..., "--booking-id"),
    passengers_stdin: bool = typer.Option(False, "--passengers-stdin"),
    passengers_file: Path | None = typer.Option(None, "--passengers-file"),  # noqa: B008
    seat_policy: str | None = typer.Option(None, "--seat-policy"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    source = PassengerSource(
        use_stdin=passengers_stdin,
        file_path=passengers_file,
        stdin=sys.stdin,
    )
    result = build_booking_runtime().orders.create(booking_id, source, seat_policy)
    _write_result(result, json_output=json_output)


@order_app.command("status")
def order_status(
    order_no: str = typer.Option(..., "--order-no"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    result = build_booking_runtime().ticketing.poll(order_no, timeout_seconds=120.0)
    _write_result(result, json_output=json_output)


@order_app.command("pay")
def order_pay(
    confirmation_id: str = typer.Option(..., "--confirmation-id"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    result = build_booking_runtime().payments.pay(confirmation_id)
    _write_result(result, json_output=json_output)


@baggage_app.command("list")
def baggage_list(
    booking_id: str = typer.Option(..., "--booking-id"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    _write_result(build_booking_runtime().ancillaries.list_baggage(booking_id), json_output=json_output)


@baggage_app.command("select")
def baggage_select(
    booking_id: str = typer.Option(..., "--booking-id"),
    traveler_id: str = typer.Option(..., "--traveler-id"),
    segment_id: str = typer.Option(..., "--segment-id"),
    baggage_id: str = typer.Option(..., "--baggage-id"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    result = build_booking_runtime().ancillaries.select_baggage(
        booking_id,
        traveler_id,
        segment_id,
        baggage_id,
    )
    _write_result(result, json_output=json_output)


@baggage_app.command("remove")
def baggage_remove(
    booking_id: str = typer.Option(..., "--booking-id"),
    traveler_id: str = typer.Option(..., "--traveler-id"),
    segment_id: str = typer.Option(..., "--segment-id"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    result = build_booking_runtime().ancillaries.remove_baggage(booking_id, traveler_id, segment_id)
    _write_result(result, json_output=json_output)


@seat_app.command("list")
def seat_list(
    booking_id: str = typer.Option(..., "--booking-id"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    _write_result(build_booking_runtime().ancillaries.list_seats(booking_id), json_output=json_output)


@seat_app.command("select")
def seat_select(
    booking_id: str = typer.Option(..., "--booking-id"),
    traveler_id: str = typer.Option(..., "--traveler-id"),
    segment_id: str = typer.Option(..., "--segment-id"),
    seat_id: str = typer.Option(..., "--seat-id"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    result = build_booking_runtime().ancillaries.select_seat(
        booking_id,
        traveler_id,
        segment_id,
        seat_id,
    )
    _write_result(result, json_output=json_output)


@seat_app.command("remove")
def seat_remove(
    booking_id: str = typer.Option(..., "--booking-id"),
    traveler_id: str = typer.Option(..., "--traveler-id"),
    segment_id: str = typer.Option(..., "--segment-id"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    result = build_booking_runtime().ancillaries.remove_seat(booking_id, traveler_id, segment_id)
    _write_result(result, json_output=json_output)
