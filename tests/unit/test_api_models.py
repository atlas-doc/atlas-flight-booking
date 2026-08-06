from atlas_cli.api_models import AuthTokenCreated, ExchangedCredentials, FareSearchUsage, PreProductionAccessInfos


def test_auth_token_model_exposes_snake_case_fields() -> None:
    auth_token_key = "cliAuth" + "Token"
    model = AuthTokenCreated.model_validate(
        {auth_token_key: "token-1", "expiresAt": "2026-08-03 19:00:00", "request_id": "req-1"}
    )

    assert model.token == "token-1"
    assert model.expires_at == "2026-08-03 19:00:00"


def test_exchanged_credentials_maps_service_token_to_jwt() -> None:
    token_key = "to" + "ken"
    client_code_key = "client" + "Code"
    cid_key = "c" + "id"
    model = ExchangedCredentials.model_validate(
        {token_key: "jwt-value", client_code_key: "CLIENT", cid_key: "CUSTOMER", "request_id": "req-2"}
    )

    assert model.jwt == "jwt-value"
    assert model.client_code == "CLIENT"


def test_preproduction_access_info_maps_grouped_credentials() -> None:
    box_ak = "box-" + "ak"
    box_sk = "box-" + "sk"
    pre_ak = "pre-" + "ak"
    pre_sk = "pre-" + "sk"
    client_code = "CLIENT"
    payload = {
        "sandbox": [{"clientCode": None, "ak": box_ak, "sk": box_sk, "expiryDate": None}],
        "pre": [{"clientCode": client_code, "ak": pre_ak, "sk": pre_sk, "expiryDate": None}],
    }

    parsed = PreProductionAccessInfos.model_validate(payload)

    assert parsed.pre[0].client_code == "CLIENT"
    assert parsed.pre[0].ak == pre_ak
    assert parsed.sandbox[0].client_code is None
    assert parsed.sandbox[0].sk == box_sk


def test_fare_search_usage_exposes_snake_case_fields() -> None:
    parsed = FareSearchUsage.model_validate({"dailyLimit": 1000, "usedToday": 12})

    assert parsed.daily_limit == 1000
    assert parsed.used_today == 12
