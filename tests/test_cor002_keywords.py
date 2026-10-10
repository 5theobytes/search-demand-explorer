import json

import pytest

from app import clean_keywords

BASE = {"login": "user", "password": "secret", "location_code": 2840}


def post(client, path, body):
    return client.post(path, data=json.dumps(body), content_type="application/json")


# --- helper -----------------------------------------------------------------


def test_helper_valid_list_passthrough():
    assert clean_keywords({"keywords": ["a", "b"]}, 10, "limit", "empty") == ["a", "b"]


def test_helper_trims_whitespace():
    assert clean_keywords({"keywords": ["  solar panels \t", "\nbattery"]}, 10, "l", "e") == [
        "solar panels",
        "battery",
    ]


def test_helper_drops_empty_strings():
    assert clean_keywords({"keywords": ["", "   ", "x", "\n"]}, 10, "l", "e") == ["x"]


def test_helper_dedupes_case_insensitively_preserving_order():
    assert clean_keywords({"keywords": ["Solar", "battery", "solar", "SOLAR "]}, 10, "l", "e") == [
        "Solar",
        "battery",
    ]


@pytest.mark.parametrize("bad", ["solar", {"a": 1}, 42, None, True])
def test_helper_rejects_non_list(bad):
    with pytest.raises(ValueError, match="array of strings"):
        clean_keywords({"keywords": bad}, 10, "l", "e")


@pytest.mark.parametrize("item", [1, 1.5, None, True, ["nested"], {"k": "v"}])
def test_helper_rejects_non_string_items(item):
    with pytest.raises(ValueError, match=r"keywords\[1\] must be a string"):
        clean_keywords({"keywords": ["ok", item]}, 10, "l", "e")


def test_helper_missing_key_is_empty():
    with pytest.raises(ValueError, match="^empty$"):
        clean_keywords({}, 10, "limit", "empty")


def test_helper_limit_applies_after_cleanup():
    body = {"keywords": ["a", "A", "", "b"]}
    assert clean_keywords(body, 2, "limit", "empty") == ["a", "b"]
    with pytest.raises(ValueError, match="^limit$"):
        clean_keywords({"keywords": ["a", "b", "c"]}, 2, "limit", "empty")


# --- /api/volume ------------------------------------------------------------


def test_volume_valid_list_reaches_provider(client, captured_post):
    res = post(client, "/api/volume", {**BASE, "keywords": [" solar ", "battery", "Solar"]})
    assert res.status_code == 200, res.get_json()
    assert captured_post[0]["endpoint"] == "keywords_data/google_ads/search_volume/live"
    assert captured_post[0]["payload"][0]["keywords"] == ["solar", "battery"]


def test_volume_non_list_400(client):
    res = post(client, "/api/volume", {**BASE, "keywords": "solar"})
    assert res.status_code == 400
    assert res.get_json() == {"error": "The keywords field must be an array of strings"}


def test_volume_list_with_ints_400(client):
    res = post(client, "/api/volume", {**BASE, "keywords": ["solar", 42]})
    assert res.status_code == 400
    assert res.get_json() == {"error": "keywords[1] must be a string"}


def test_volume_only_empties_400(client):
    res = post(client, "/api/volume", {**BASE, "keywords": ["", "  "]})
    assert res.status_code == 400
    assert res.get_json() == {"error": "Enter at least one keyword"}


def test_volume_over_limit_400(client):
    res = post(client, "/api/volume", {**BASE, "keywords": [f"k{i}" for i in range(1001)]})
    assert res.status_code == 400
    assert res.get_json() == {"error": "A volume request supports at most 1000 keywords"}


def test_volume_exactly_limit_ok(client, captured_post):
    res = post(client, "/api/volume", {**BASE, "keywords": [f"k{i}" for i in range(1000)]})
    assert res.status_code == 200, res.get_json()
    assert len(captured_post[0]["payload"][0]["keywords"]) == 1000


# --- /api/discover ----------------------------------------------------------


def test_discover_non_list_400(client):
    res = post(client, "/api/discover", {**BASE, "keywords": {"a": 1}})
    assert res.status_code == 400
    assert res.get_json() == {"error": "The keywords field must be an array of strings"}


def test_discover_list_with_ints_400(client):
    res = post(client, "/api/discover", {**BASE, "keywords": [1, 2]})
    assert res.status_code == 400
    assert res.get_json() == {"error": "keywords[0] must be a string"}


def test_discover_over_limit_400(client):
    res = post(client, "/api/discover", {**BASE, "keywords": [f"k{i}" for i in range(201)]})
    assert res.status_code == 400
    assert res.get_json() == {"error": "A discovery run supports at most 200 seed keywords"}


def test_discover_trims_dedupes_and_chunks_by_20(client, captured_post):
    seeds = [f" seed{i} " for i in range(25)] + ["SEED0"]
    res = post(client, "/api/discover", {**BASE, "keywords": seeds, "sources": ["google_ads"]})
    assert res.status_code == 200, res.get_json()
    assert res.get_json()["seed_count"] == 25
    assert [len(c["payload"][0]["keywords"]) for c in captured_post] == [20, 5]
    assert captured_post[0]["payload"][0]["keywords"][0] == "seed0"


# --- COR-001 unchanged ------------------------------------------------------


@pytest.mark.parametrize("path", ["/api/volume", "/api/discover"])
def test_cor001_malformed_json_400(client, path):
    res = client.post(path, data="{not json", content_type="application/json")
    assert res.status_code == 400
    assert res.get_json() == {"error": "Invalid JSON"}


@pytest.mark.parametrize("path", ["/api/volume", "/api/discover"])
def test_cor001_non_object_400(client, path):
    res = client.post(path, data="[1, 2]", content_type="application/json")
    assert res.status_code == 400
    assert res.get_json() == {"error": "Expected a JSON object"}


@pytest.mark.parametrize("path", ["/api/volume", "/api/discover"])
def test_cor001_wrong_media_type_415(client, path):
    res = client.post(path, data="login=x", content_type="text/plain")
    assert res.status_code == 415
    assert res.get_json() == {"error": "Expected Content-Type application/json"}


def test_credentials_checked_before_keywords(client):
    res = post(client, "/api/volume", {"keywords": "bad", "location_code": 2840})
    assert res.status_code == 400
    assert res.get_json() == {"error": "Enter your DataForSEO login and API password"}
