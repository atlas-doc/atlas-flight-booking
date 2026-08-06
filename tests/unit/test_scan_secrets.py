from scripts.scan_secrets import main, scan_text


def test_secret_scan_rejects_jwt_and_long_tokens() -> None:
    text = "Token: " + "ey" + "JhbGciOiJIUzI1NiJ9." + "a" * 24 + "." + "b" * 20
    findings = scan_text(text)
    assert findings == ["jwt-like token"]


def test_secret_scan_rejects_long_token_in_curl_header() -> None:
    token = "temporary-" + "authorization-token-value"
    text = f"--header 'x-atlas-cli-auth-token: {token}'"

    assert scan_text(text) == ["sensitive header value"]


def test_secret_scan_accepts_redacted_placeholders() -> None:
    text = "\n".join(
        [
            "Token: <REDACTED_JWT>",
            "ak: <REDACTED_AK>",
            "sk: <REDACTED_SK>",
            "/cli/auth/token/<REDACTED_CLI_AUTH_TOKEN>/status",
            "https://example.invalid/?cliAuthToken=YOUR_CLI_AUTH_TOKEN",
            "https://example.invalid/?cliAuthToken={cli_auth_token}",
            "https://example.invalid/?cliAuthToken=token_value_is_redacted",
            "token: str",
        ]
    )
    assert scan_text(text) == []


def test_secret_scan_rejects_unmistakable_passenger_privacy_probe_values() -> None:
    text = " ".join(
        [
            "LEAK" + "CHECK/PRIVACY",
            "PRIVACY" + "DOC0001",
            "privacy.probe" + "@example.invalid",
            "0065-" + "55555555",
        ]
    )

    assert scan_text(text) == ["passenger fixture value", "email address"]


def test_secret_scan_allows_explicitly_synthetic_passenger_fixtures() -> None:
    text = " ".join(["SYNTHETIC/EXAMPLE", "SYNTHETICDOC0001", "synthetic@example.invalid"])

    assert scan_text(text) == []


def test_directory_scan_skips_known_test_source_that_contains_synthetic_fixtures(tmp_path) -> None:
    source = tmp_path / "tests/unit/test_logging_config.py"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text('{"cliAuthToken":"synthetic-test-value"}\n', encoding="utf-8")

    assert main([str(tmp_path)]) == 0
