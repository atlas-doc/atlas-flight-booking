"""Composition root for booking workflow commands."""

from __future__ import annotations

from dataclasses import dataclass

from atlas_cli.access import AccessManager
from atlas_cli.ancillaries import AncillaryAdapter, AncillaryService
from atlas_cli.api_client import AtlasApiClient
from atlas_cli.booking_store import BookingStore
from atlas_cli.business_client import AtlasBusinessClient
from atlas_cli.config import InternalSettings
from atlas_cli.endpoints import CustomerMode, EndpointResolver
from atlas_cli.orders import OrderAdapter, OrderService
from atlas_cli.payments import PaymentAdapter, PaymentService
from atlas_cli.routing_normalizer import RoutingNormalizer
from atlas_cli.search_store import SearchStore
from atlas_cli.secure_store import KeyringSecretStore
from atlas_cli.ticketing import QueryOrderAdapter, TicketingService
from atlas_cli.verify import VerifyAdapter, VerifyService


@dataclass(frozen=True)
class BookingRuntime:
    verify: VerifyService
    ancillaries: AncillaryService
    orders: OrderService
    ticketing: TicketingService
    payments: PaymentService


def build_booking_runtime(*, mode: CustomerMode = CustomerMode.PROD) -> BookingRuntime:
    settings = InternalSettings()
    resolver = EndpointResolver(settings)
    secrets = KeyringSecretStore()
    control_api = AtlasApiClient(settings)
    access = AccessManager(api=control_api, secrets=secrets, resolver=resolver, mode=mode)
    business = AtlasBusinessClient(settings)
    normalizer = RoutingNormalizer()
    booking_store = BookingStore(secrets=secrets)
    verify = VerifyService(
        secrets=secrets,
        access=access,
        adapter=VerifyAdapter(business, normalizer),
        search_store=SearchStore(secrets=secrets),
        booking_store=booking_store,
    )
    ancillaries = AncillaryService(
        secrets=secrets,
        access=access,
        adapter=AncillaryAdapter(
            business,
            default_retry_seconds=settings.poll_interval_seconds,
        ),
        booking_store=booking_store,
        default_retry_seconds=settings.poll_interval_seconds,
    )
    orders = OrderService(
        secrets=secrets,
        access=access,
        adapter=OrderAdapter(business),
        booking_store=booking_store,
        order_url=resolver.order_url,
    )
    ticketing = TicketingService(
        secrets=secrets,
        access=access,
        adapter=QueryOrderAdapter(business),
        booking_store=booking_store,
        order_url=resolver.order_url,
    )
    payments = PaymentService(
        secrets=secrets,
        access=access,
        adapter=PaymentAdapter(business),
        booking_store=booking_store,
        ticketing=ticketing,
    )
    return BookingRuntime(
        verify=verify,
        ancillaries=ancillaries,
        orders=orders,
        ticketing=ticketing,
        payments=payments,
    )
