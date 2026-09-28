from scrapers.tucson_code_violations import feature_to_record, is_open_case


def test_feature_to_record_preserves_official_case_evidence():
    feature = {
        "attributes": {
            "OBJECTID": 42,
            "CASENUMBER": "CE-VIO-012345",
            "status": "Notice of Violation",
            "OPENEDDATE": 1788048000000,
            "CLOSEDDATE": None,
            "LASTCHANGEDON": 1788134400000,
            "DESCRIPTION": "Accumulation of refuse",
            "MainAddress": "123 E TEST ST",
            "UNITORSUITE": None,
            "CITY": "Tucson",
            "POSTALCODE": "85701",
            "District": "6",
            "PARCEL": "117-01-0010",
            "LAT": 32.22,
            "LON": -110.97,
            "casetype": "Property Maintenance",
            "PRO_URL": "https://example.invalid/case",
        }
    }

    record = feature_to_record(feature, fetched_at="2026-08-31T02:30:00Z")

    assert record["county"] == "Pima"
    assert record["source"] == "city_of_tucson_code_enforcement_arcgis"
    assert record["doc_type"] == "Code Violation"
    assert record["doc_number"] == "CE-VIO-012345"
    assert record["apn_norm"] == "117010010"
    assert record["resolved"] is True
    assert record["case_status"] == "Notice of Violation"
    assert record["recorded_date"] == "2026-08-30"
    assert record["last_changed_date"] == "2026-08-31"
    assert record["active_case"] is True
    assert record["site_address"] == "123 E TEST ST"


def test_is_open_case_excludes_closed_statuses():
    assert is_open_case("Active") is True
    assert is_open_case("Court Hearing") is True
    assert is_open_case("Closed - Resolved") is False
    assert is_open_case("Closed - Unfounded") is False