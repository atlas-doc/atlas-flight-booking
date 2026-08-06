from __future__ import annotations

import io
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from test_booking_flow import _offer, _passengers, _runtime, _script

from atlas_cli.passengers import PassengerSource


def _concurrently(call):
    with ThreadPoolExecutor(max_workers=2) as pool:
        return list(pool.map(lambda _: call(), range(2)))


def test_two_simultaneous_order_creates_dispatch_one_order_request(tmp_path: Path) -> None:
    """Removing the atomic begin-order transition would let both contenders post an order."""
    script = _script(include_ancillaries=False)
    runtime = _runtime(tmp_path, script, offer=_offer(ancillary_supported=()))
    verified = runtime.verify.verify("off_3")
    booking_id = str(verified.data["booking_id"])
    traveler_id = str(verified.data["travelers"][0]["traveler_id"])

    results = _concurrently(
        lambda: runtime.orders.create(
            booking_id,
            PassengerSource(use_stdin=True, file_path=None, stdin=io.StringIO(_passengers(traveler_id))),
            None,
        )
    )

    assert [result.code for result in results].count("PAYMENT_CONFIRMATION_REQUIRED") == 1
    assert script.paths.count("/order.do") == 1
    assert script.paths == ["/verify.do", "/order.do"]


def test_two_simultaneous_payment_confirms_dispatch_one_payment_request(tmp_path: Path) -> None:
    """Removing atomic confirmation consumption would let both threads submit balance payment."""
    script = _script(include_ancillaries=False)
    runtime = _runtime(tmp_path, script, offer=_offer(ancillary_supported=()))
    verified = runtime.verify.verify("off_3")
    booking_id = str(verified.data["booking_id"])
    traveler_id = str(verified.data["travelers"][0]["traveler_id"])
    created = runtime.orders.create(
        booking_id,
        PassengerSource(use_stdin=True, file_path=None, stdin=io.StringIO(_passengers(traveler_id))),
        None,
    )
    confirmation_id = str(created.data["payment_confirmation_id"])

    results = _concurrently(lambda: runtime.payments.pay(confirmation_id))

    assert [result.code for result in results].count("TICKETED") == 1
    assert script.paths.count("/pay.do") == 1
    assert script.paths.count("/queryOrderDetails.do") == 1
    assert script.paths == ["/verify.do", "/order.do", "/pay.do", "/queryOrderDetails.do"]
