import asyncio
import math
import time
import aiohttp
from base64 import b64encode
from flask import Flask, jsonify, render_template, request, Response
from werkzeug.exceptions import BadRequest, UnsupportedMediaType

app = Flask(__name__)

DFS_BASE = "https://api.dataforseo.com/v3"
DFS_TIMEOUT_SECONDS = 15
DISCOVERY_MAX_SECONDS = 45
DISCOVERY_PROCESSING_SECONDS = 10
DISCOVERY_MAX_CALLS = 30


class DiscoveryBudgetReached(Exception):
    pass


class InvalidDataForSEOResponse(Exception):
    pass


@app.after_request
def add_security_headers(response):
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; connect-src 'self'; img-src 'self' data:; "
        "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
        "font-src 'self'; base-uri 'none'; frame-ancestors 'none'"
    )
    return response


@app.errorhandler(UnsupportedMediaType)
def handle_unsupported_media_type(_):
    return jsonify({"error": "Expected Content-Type application/json"}), 415


def clean_body():
    try:
        body = request.get_json()
    except BadRequest as exc:
        raise ValueError("Invalid JSON") from exc
    if not isinstance(body, dict):
        raise ValueError("Expected a JSON object")
    return body


def clean_keywords(body, limit, limit_message, empty_message):
    """COR-002: `keywords` must be array[string]; items are trimmed, empties dropped,
    duplicates removed case-insensitively (first occurrence wins, order preserved)."""
    raw = body.get("keywords", [])
    if not isinstance(raw, list):
        raise ValueError("The keywords field must be an array of strings")
    keywords = []
    seen = set()
    for index, value in enumerate(raw):
        if not isinstance(value, str):
            raise ValueError(f"keywords[{index}] must be a string")
        keyword = value.strip()
        if not keyword:
            continue
        key = keyword.casefold()
        if key in seen:
            continue
        seen.add(key)
        keywords.append(keyword)
    if not keywords:
        raise ValueError(empty_message)
    if len(keywords) > limit:
        raise ValueError(limit_message)
    return keywords


def dataforseo_credentials(body):
    login = str(body.get("login", "")).strip()
    password = str(body.get("password", "")).strip()
    if not login or not password:
        raise ValueError("Enter your DataForSEO login and API password")
    return login, password


def auth_header(login, password):
    token = b64encode((login + ":" + password).encode()).decode()
    return {"Authorization": "Basic " + token, "Content-Type": "application/json"}


def validate_dataforseo_response(data):
    def invalid(message):
        raise InvalidDataForSEOResponse(message)

    def valid_cost(value):
        if isinstance(value, bool):
            return False
        try:
            cost = float(value)
        except (TypeError, ValueError):
            return False
        return math.isfinite(cost) and cost >= 0

    def valid_volume(value):
        if value is None:
            return True
        if isinstance(value, bool):
            return False
        if isinstance(value, int):
            return value >= 0
        return (
            isinstance(value, float)
            and math.isfinite(value)
            and value >= 0
            and value.is_integer()
        )

    def validate_serp_node(node, context=None):
        if isinstance(node, list):
            for value in node:
                validate_serp_node(value, context)
            return
        if not isinstance(node, dict):
            return

        node_type = node.get("type") or context
        if node_type == "organic" and "extended_people_also_search" in node:
            extended = node["extended_people_also_search"]
            if extended is not None and (
                not isinstance(extended, list)
                or any(not isinstance(value, (str, dict)) for value in extended)
            ):
                invalid("DataForSEO returned invalid extended_people_also_search")

        for field in ("items", "options"):
            if field not in node or node[field] is None:
                continue
            children = node[field]
            if not isinstance(children, list) or any(
                not isinstance(child, dict) for child in children
            ):
                invalid(f"DataForSEO returned an invalid list for {field}")
            validate_serp_node(children, node_type)

    def validate_volume(record):
        volume = record.get("search_volume")
        if "search_volume" in record and not valid_volume(volume):
            invalid("DataForSEO returned invalid search_volume")
        keyword_info = record.get("keyword_info")
        if isinstance(keyword_info, dict):
            volume = keyword_info.get("search_volume")
            if "search_volume" in keyword_info and not valid_volume(volume):
                invalid("DataForSEO returned invalid keyword_info.search_volume")
        keyword_data = record.get("keyword_data")
        if isinstance(keyword_data, dict):
            nested_info = keyword_data.get("keyword_info")
            if isinstance(nested_info, dict):
                volume = nested_info.get("search_volume")
                if "search_volume" in nested_info and not valid_volume(volume):
                    invalid(
                        "DataForSEO returned invalid "
                        "keyword_data.keyword_info.search_volume"
                    )

    if not isinstance(data, dict):
        invalid("DataForSEO returned an unexpected JSON structure")
    status_code = data.get("status_code")
    if not isinstance(status_code, int) or isinstance(status_code, bool):
        invalid("DataForSEO returned invalid status_code")
    if not valid_cost(data.get("cost")):
        invalid("DataForSEO returned invalid cost")

    tasks = data.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        invalid("DataForSEO returned a response without a tasks array")

    for task in tasks:
        if not isinstance(task, dict):
            invalid("DataForSEO returned an invalid record in task")
        task_status = task.get("status_code")
        if not isinstance(task_status, int) or isinstance(task_status, bool):
            invalid("DataForSEO returned invalid task status_code")
        if not valid_cost(task.get("cost")):
            invalid("DataForSEO returned invalid task cost")

        result = task.get("result")
        if result is None:
            continue
        if not isinstance(result, list):
            invalid("DataForSEO returned an invalid array for result")

        for result_item in result:
            if not isinstance(result_item, dict):
                invalid("DataForSEO returned an invalid record in result")
            validate_volume(result_item)
            validate_serp_node(result_item)
            items = result_item.get("items")
            if items is None:
                continue
            if not isinstance(items, list):
                invalid("DataForSEO returned an invalid array for items")
            for item in items:
                if not isinstance(item, dict):
                    invalid("DataForSEO returned an invalid record in items")
                validate_volume(item)
                for field in ("keyword_info", "keyword_data"):
                    value = item.get(field)
                    if value is not None and not isinstance(value, dict):
                        invalid(f"DataForSEO returned an invalid field: {field}")
                keyword_data = item.get("keyword_data")
                if keyword_data is not None:
                    nested_info = keyword_data.get("keyword_info")
                    if nested_info is not None and not isinstance(nested_info, dict):
                        invalid(
                            "DataForSEO returned an invalid field: "
                            "keyword_data.keyword_info"
                        )
                if item.get("type") == "google_trends_queries_list":
                    trends_data = item.get("data")
                    if trends_data is None:
                        trends_data = {}
                    elif not isinstance(trends_data, dict):
                        invalid("DataForSEO returned an invalid field: data")
                    for bucket in ("top", "rising"):
                        queries = trends_data.get(bucket)
                        if queries is not None and (
                            not isinstance(queries, list)
                            or any(not isinstance(query, dict) for query in queries)
                        ):
                            invalid(f"DataForSEO returned an invalid list for {bucket}")

    return data


async def _dfs_post(endpoint, payload, login, password, timeout):
    request_timeout = aiohttp.ClientTimeout(total=timeout)
    async with aiohttp.ClientSession(timeout=request_timeout) as session:
        async with session.post(
            DFS_BASE + "/" + endpoint,
            headers=auth_header(login, password),
            json=payload,
        ) as response:
            response.raise_for_status()
            try:
                data = await response.json()
            except (aiohttp.ClientError, ValueError) as exc:
                raise InvalidDataForSEOResponse(
                    "DataForSEO returned invalid or empty JSON"
                ) from exc
            return validate_dataforseo_response(data)


def dfs_post(endpoint, payload, login, password, timeout=DFS_TIMEOUT_SECONDS):
    return asyncio.run(_dfs_post(endpoint, payload, login, password, timeout))


async def _dfs_get(endpoint, login, password, timeout):
    request_timeout = aiohttp.ClientTimeout(total=timeout)
    async with aiohttp.ClientSession(timeout=request_timeout) as session:
        async with session.get(
            DFS_BASE + "/" + endpoint,
            headers=auth_header(login, password),
        ) as response:
            response.raise_for_status()
            try:
                data = await response.json()
            except (aiohttp.ClientError, ValueError) as exc:
                raise InvalidDataForSEOResponse(
                    "DataForSEO returned invalid or empty JSON"
                ) from exc
            return validate_dataforseo_response(data)


def dfs_get(endpoint, login, password, timeout=DFS_TIMEOUT_SECONDS):
    return asyncio.run(_dfs_get(endpoint, login, password, timeout))


def normalize_country_catalog(data):
    countries = {}
    for task in data["tasks"]:
        if task.get("status_code") != 20000:
            continue
        for location in task.get("result") or []:
            if location.get("location_type") != "Country":
                continue
            location_code = location.get("location_code")
            location_name = location.get("location_name")
            country_iso_code = location.get("country_iso_code")
            if (isinstance(location_code, bool)
                    or not isinstance(location_code, int)
                    or location_code <= 0
                    or not isinstance(location_name, str)
                    or not location_name.strip()
                    or not isinstance(country_iso_code, str)
                    or not country_iso_code.strip()):
                raise InvalidDataForSEOResponse("Invalid country in the catalog")

            raw_languages = location.get("available_languages")
            if not isinstance(raw_languages, list):
                raise InvalidDataForSEOResponse("Invalid country language list")
            languages = {}
            for language in raw_languages:
                if not isinstance(language, dict):
                    raise InvalidDataForSEOResponse("Invalid language in the catalog")
                sources = language.get("available_sources")
                if not isinstance(sources, list):
                    raise InvalidDataForSEOResponse("Invalid language sources")
                if "google" not in sources:
                    continue
                code = language.get("language_code")
                name = language.get("language_name")
                if (not isinstance(code, str) or not code.strip()
                        or not isinstance(name, str) or not name.strip()):
                    raise InvalidDataForSEOResponse("Invalid language data")
                code = code.strip()
                languages[code] = {
                    "language_code": code,
                    "language_name": name.strip(),
                }
            if languages:
                countries[location_code] = {
                    "location_code": location_code,
                    "location_name": location_name.strip(),
                    "country_iso_code": country_iso_code.strip(),
                    "languages": sorted(
                        languages.values(),
                        key=lambda row: row["language_name"].casefold(),
                    ),
                }
    if not countries:
        raise InvalidDataForSEOResponse("The catalog contains no supported Google countries")
    return sorted(countries.values(), key=lambda row: row["location_name"].casefold())


def task_error(task):
    return f"{task.get('status_code')}: {task.get('status_message') or 'Unknown DataForSEO error'}"


def task_cost(data):
    return float(data.get("cost") or sum(float(t.get("cost") or 0) for t in data.get("tasks", [])))


def numeric_score(value):
    if value is None:
        return None
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(score):
        return None
    return int(score) if score.is_integer() else score


def score_label(value):
    if value is None or numeric_score(value) is not None:
        return None
    label = str(value).strip()
    return label or None


def google_ads_row(item, source="google_ads", seed=""):
    return {
        "keyword": item.get("keyword", ""),
        "volume": item.get("search_volume") or 0,
        "source": source,
        "seed": seed,
        "kind": "query",
        "score": None,
        "monthly_searches": item.get("monthly_searches") or [],
    }


def labs_row(item, source, seed=""):
    info = item.get("keyword_info") or (item.get("keyword_data") or {}).get("keyword_info") or {}
    return {
        "keyword": item.get("keyword", "") or (item.get("keyword_data") or {}).get("keyword", ""),
        "volume": info.get("search_volume") or 0,
        "source": source,
        "seed": seed,
        "kind": "query",
        "score": None,
    }


def dedupe_rows(rows, deadline=None):
    # Same phrase from several sources is intentionally retained as one row,
    # but all source labels are merged so the UI can show cross-source evidence.
    merged = {}
    for index, row in enumerate(rows):
        if deadline is not None and index % 1000 == 0 and time.monotonic() >= deadline:
            break
        kw = str(row.get("keyword", "")).strip()
        if not kw:
            continue
        key = (kw.casefold(), row.get("kind", "query"))
        if key not in merged:
            row = dict(row)
            raw_score = row.get("score")
            row["score"] = numeric_score(raw_score)
            row["score_label"] = row.get("score_label") or score_label(raw_score)
            row["sources"] = [row.get("source")] if row.get("source") else []
            row["seeds"] = [row.get("seed")] if row.get("seed") else []
            merged[key] = row
            continue
        cur = merged[key]
        if row.get("source") and row["source"] not in cur["sources"]:
            cur["sources"].append(row["source"])
        if row.get("seed") and row["seed"] not in cur["seeds"]:
            cur["seeds"].append(row["seed"])
        if (row.get("volume") or 0) > (cur.get("volume") or 0):
            cur["volume"] = row.get("volume") or 0
        raw_score = row.get("score")
        row_score = numeric_score(raw_score)
        if row_score is not None:
            cur["score"] = max(cur.get("score") or 0, row_score)
        label = row.get("score_label") or score_label(raw_score)
        if label and not cur.get("score_label"):
            cur["score_label"] = label
    return list(merged.values())


def serp_direction_rows(result, seed):
    rows = []

    def add(value, source, kind="query"):
        value = str(value or "").strip()
        if value:
            rows.append({
                "keyword": value,
                "volume": 0,
                "source": source,
                "seed": seed,
                "kind": kind,
                "score": None,
            })

    def walk(node, context=None):
        if isinstance(node, list):
            for value in node:
                walk(value, context)
            return
        if not isinstance(node, dict):
            return

        node_type = node.get("type") or context
        if node_type == "people_also_ask_element":
            add(node.get("title"), "people_also_ask", "question")
        elif node_type in ("related_searches_element", "related_searches"):
            add(node.get("title") or node.get("keyword") or node.get("query"), "related_searches")
        elif node_type in ("people_also_search_element", "people_also_search"):
            add(node.get("title") or node.get("keyword") or node.get("query"), "people_also_search")
        elif node_type == "refinement_chips_option":
            add(node.get("title"), "refinement")
        elif node_type == "organic":
            for ext in node.get("extended_people_also_search") or []:
                if isinstance(ext, str):
                    add(ext, "people_also_search")
                elif isinstance(ext, dict):
                    add(ext.get("title") or ext.get("keyword") or ext.get("query"), "people_also_search")

        for key in ("items", "options"):
            if key in node:
                walk(node.get(key), node_type)

    for item in result.get("items") or []:
        walk(item)
    return rows


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/health")
def health():
    return jsonify({"ok": True})


@app.route("/manifest.webmanifest")
def manifest():
    return jsonify({
        "name": "Search Demand Explorer",
        "short_name": "Demand Explorer",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#0f1117",
        "theme_color": "#7c3aed",
        "description": "Search direction discovery with DataForSEO",
    })


@app.route("/sw.js")
def service_worker():
    js = """const CACHE='demand-explorer-v4';
self.addEventListener('install',e=>{e.waitUntil(caches.open(CACHE).then(c=>c.addAll(['/','/icon.svg','/manifest.webmanifest'])));self.skipWaiting();});
self.addEventListener('activate',e=>{e.waitUntil(caches.keys().then(keys=>Promise.all(keys.filter(k=>k!==CACHE).map(k=>caches.delete(k)))).then(()=>self.clients.claim()));});
self.addEventListener('fetch',e=>{if(e.request.method!=='GET')return;if(new URL(e.request.url).origin!==self.location.origin)return;e.respondWith(fetch(e.request).catch(()=>caches.match(e.request)));});"""
    response = Response(js, mimetype="application/javascript")
    response.headers["Cache-Control"] = "no-cache"
    return response


@app.route("/icon.svg")
def icon():
    svg = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
    <rect width="512" height="512" rx="112" fill="#0f1117"/>
    <circle cx="220" cy="220" r="112" fill="none" stroke="#7c3aed" stroke-width="38"/>
    <path d="M302 302L412 412" stroke="#a78bfa" stroke-width="44" stroke-linecap="round"/>
    <path d="M151 220h138M220 151v138" stroke="#e2e8f0" stroke-width="24" stroke-linecap="round"/>
    </svg>"""
    return Response(svg, mimetype="image/svg+xml")


@app.route("/api/catalog", methods=["POST"])
def catalog():
    try:
        body = clean_body()
        login, password = dataforseo_credentials(body)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    try:
        data = dfs_get("dataforseo_labs/locations_and_languages", login, password)
        for api_task in data["tasks"]:
            if api_task.get("status_code") != 20000:
                error = "DataForSEO: " + task_error(api_task)
                for secret in (login, password):
                    error = error.replace(secret, "[redacted]")
                return jsonify({"error": error}), 502
        return jsonify({"locations": normalize_country_catalog(data)})
    except InvalidDataForSEOResponse:
        return jsonify({"error": "The DataForSEO catalog is empty or invalid"}), 502
    except aiohttp.ClientResponseError as exc:
        return jsonify({"error": "DataForSEO HTTP error: " + str(exc.status)}), 502
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return jsonify({"error": "Could not load the DataForSEO catalog"}), 502


@app.route("/api/volume", methods=["POST"])
def volume():
    try:
        body = clean_body()
        login, password = dataforseo_credentials(body)
        keywords = clean_keywords(
            body,
            limit=1000,
            limit_message="A volume request supports at most 1000 keywords",
            empty_message="Enter at least one keyword",
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    location_code = body.get("location_code")
    language_code = str(body.get("language_code", "")).strip()

    if not location_code:
        return jsonify({"error": "Select a location"}), 400

    task = {"keywords": keywords, "location_code": int(location_code), "sort_by": "search_volume"}
    if language_code:
        task["language_code"] = language_code

    try:
        data = dfs_post("keywords_data/google_ads/search_volume/live", [task], login, password)
        rows = []
        for api_task in data.get("tasks", []):
            if api_task.get("status_code") != 20000:
                return jsonify({"error": "DataForSEO: " + task_error(api_task)}), 502
            rows.extend(google_ads_row(item, "volume") for item in (api_task.get("result") or []))
        return jsonify({"rows": rows, "cost": task_cost(data), "tasks": 1})
    except InvalidDataForSEOResponse:
        return jsonify({
            "error": "The DataForSEO response is empty or invalid; the request may have been processed and charged."
        }), 502
    except aiohttp.ClientResponseError as exc:
        return jsonify({"error": "DataForSEO HTTP error: " + str(exc.status)}), 502
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return jsonify({
            "error": "No DataForSEO response was received; the request may have been processed and charged."
        }), 502


@app.route("/api/discover", methods=["POST"])
def discover():
    try:
        body = clean_body()
        login, password = dataforseo_credentials(body)
        seeds = clean_keywords(
            body,
            limit=200,
            limit_message="A discovery run supports at most 200 seed keywords",
            empty_message="Enter at least one seed keyword",
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    location_code = body.get("location_code")
    language_code = str(body.get("language_code", "")).strip()
    sources = body.get("sources") or ["google_ads"]
    sources = [str(x) for x in sources]
    if not location_code:
        return jsonify({"error": "Select a location"}), 400

    try:
        limit = max(10, min(5000, int(body.get("limit", 200))))
    except (TypeError, ValueError):
        limit = 200
    try:
        related_depth = max(1, min(4, int(body.get("related_depth", 1))))
    except (TypeError, ValueError):
        related_depth = 1

    # Per-user-action caps prevent an accidental expensive fan-out from mobile UI.
    # The 200-seed cap is enforced in clean_keywords().
    expensive_per_seed = {"autocomplete", "labs_suggestions", "labs_related", "trends", "serp"}
    if len(seeds) > 50 and any(s in expensive_per_seed for s in sources):
        return jsonify({"error": "Autocomplete/Labs/Trends/SERP support at most 50 seed keywords per run"}), 400

    per_seed_sources = ("labs_suggestions", "labs_related", "autocomplete", "trends", "serp")
    expected_calls = math.ceil(len(seeds) / 20) if "google_ads" in sources else 0
    expected_calls += int("labs_ideas" in sources)
    expected_calls += len(seeds) * sum(source in sources for source in per_seed_sources)
    if expected_calls > DISCOVERY_MAX_CALLS:
        return jsonify({
            "error": (
                f"This run requires {expected_calls} DataForSEO requests; "
                f"maximum per run: {DISCOVERY_MAX_CALLS}. "
                "Reduce the seed keywords or select fewer sources."
            )
        }), 400

    rows = []
    costs = {}
    calls = {}
    errors = []
    deadline = time.monotonic() + DISCOVERY_MAX_SECONDS
    processing_deadline = deadline + DISCOVERY_PROCESSING_SECONDS
    request_count = 0
    cost_complete = True

    def discover_post(endpoint, payload):
        nonlocal request_count, cost_complete
        if request_count >= DISCOVERY_MAX_CALLS:
            raise DiscoveryBudgetReached("The DataForSEO request limit was reached; partial results are shown.")
        remaining = deadline - time.monotonic()
        if remaining <= 5:
            raise DiscoveryBudgetReached("The discovery time limit was reached; partial results are shown.")
        timeout = min(DFS_TIMEOUT_SECONDS, max(1, remaining - 5))
        request_count += 1
        try:
            data = dfs_post(endpoint, payload, login, password, timeout=timeout)
        except InvalidDataForSEOResponse as exc:
            cost_complete = False
            raise DiscoveryBudgetReached(
                "DataForSEO returned an empty or invalid response; results are partial and the final cost is unknown."
            ) from exc
        except aiohttp.ClientResponseError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            cost_complete = False
            raise DiscoveryBudgetReached(
                "DataForSEO timed out; results are partial and the final cost is unknown."
            ) from exc
        if time.monotonic() >= deadline and not any("The discovery time limit was reached" in error for error in errors):
            errors.append("The discovery time limit was reached; available results are shown.")
        return data

    def discovery_payload():
        clean = dedupe_rows(rows, deadline=processing_deadline)
        clean.sort(key=lambda r: (r.get("volume") or 0, r.get("score") or 0), reverse=True)
        if time.monotonic() >= processing_deadline and not any("Result processing reached the time limit" in error for error in errors):
            errors.append("Result processing reached the time limit; collected results are shown.")
        return {
            "rows": clean[:limit],
            "raw_rows": len(rows),
            "cost": round(sum(costs.values()), 6),
            "cost_complete": cost_complete,
            "costs": {k: round(v, 6) for k, v in costs.items()},
            "calls": calls,
            "errors": errors,
            "sources": sources,
            "seed_count": len(seeds),
        }

    def add_cost(source, data):
        costs[source] = costs.get(source, 0.0) + task_cost(data)
        calls[source] = calls.get(source, 0) + 1

    try:
        # Broad Google Ads expansion: up to 20 seed terms per paid task.
        if "google_ads" in sources:
            for start in range(0, len(seeds), 20):
                chunk = seeds[start:start + 20]
                task = {"keywords": chunk, "location_code": int(location_code), "sort_by": "search_volume"}
                if language_code:
                    task["language_code"] = language_code
                data = discover_post("keywords_data/google_ads/keywords_for_keywords/live", [task])
                add_cost("google_ads", data)
                for api_task in data.get("tasks", []):
                    if api_task.get("status_code") != 20000:
                        errors.append("Google Ads: " + task_error(api_task))
                        continue
                    for item in api_task.get("result") or []:
                        rows.append(google_ads_row(item, "google_ads", " + ".join(chunk)))

        # Category-level expansion: broadest semantic direction discovery.
        if "labs_ideas" in sources:
            task = {
                "keywords": seeds[:200],
                "location_code": int(location_code),
                "limit": min(limit, 1000),
                "order_by": ["keyword_info.search_volume,desc"],
                "include_serp_info": False,
                "include_clickstream_data": False,
            }
            if language_code:
                task["language_code"] = language_code
            data = discover_post("dataforseo_labs/google/keyword_ideas/live", [task])
            add_cost("labs_ideas", data)
            for api_task in data.get("tasks", []):
                if api_task.get("status_code") != 20000:
                    errors.append("Labs Ideas: " + task_error(api_task))
                    continue
                for result in api_task.get("result") or []:
                    for item in result.get("items") or []:
                        rows.append(labs_row(item, "labs_ideas", "multi-seed"))

        # Long tails that actually contain the seed phrase.
        if "labs_suggestions" in sources:
            per_seed_limit = min(limit, 300)
            for seed in seeds:
                task = {
                    "keyword": seed,
                    "location_code": int(location_code),
                    "limit": per_seed_limit,
                    "exact_match": False,
                    "include_serp_info": False,
                    "include_clickstream_data": False,
                }
                if language_code:
                    task["language_code"] = language_code
                data = discover_post("dataforseo_labs/google/keyword_suggestions/live", [task])
                add_cost("labs_suggestions", data)
                for api_task in data.get("tasks", []):
                    if api_task.get("status_code") != 20000:
                        errors.append(f"Suggestions ({seed}): " + task_error(api_task))
                        continue
                    for result in api_task.get("result") or []:
                        for item in result.get("items") or []:
                            rows.append(labs_row(item, "labs_suggestions", seed))

        # Google's "Searches related to", recursively up to depth 4.
        if "labs_related" in sources:
            per_seed_limit = min(limit, 1000)
            for seed in seeds:
                task = {
                    "keyword": seed,
                    "location_code": int(location_code),
                    "depth": related_depth,
                    "limit": per_seed_limit,
                    "include_seed_keyword": False,
                    "include_serp_info": False,
                    "include_clickstream_data": False,
                    "ignore_synonyms": False,
                }
                if language_code:
                    task["language_code"] = language_code
                data = discover_post("dataforseo_labs/google/related_keywords/live", [task])
                add_cost("labs_related", data)
                for api_task in data.get("tasks", []):
                    if api_task.get("status_code") != 20000:
                        errors.append(f"Related ({seed}): " + task_error(api_task))
                        continue
                    for result in api_task.get("result") or []:
                        for item in result.get("items") or []:
                            row = labs_row(item, "labs_related", seed)
                            row["depth"] = item.get("depth")
                            rows.append(row)

        # What Google proposes while a person is typing.
        if "autocomplete" in sources:
            for seed in seeds:
                task = {
                    "keyword": seed,
                    "location_code": int(location_code),
                    "client": "chrome",
                }
                if language_code:
                    task["language_code"] = language_code
                data = discover_post("serp/google/autocomplete/live/advanced", [task])
                add_cost("autocomplete", data)
                for api_task in data.get("tasks", []):
                    if api_task.get("status_code") != 20000:
                        errors.append(f"Autocomplete ({seed}): " + task_error(api_task))
                        continue
                    for result in api_task.get("result") or []:
                        for item in result.get("items") or []:
                            suggestion = item.get("suggestion")
                            if suggestion:
                                rows.append({
                                    "keyword": suggestion,
                                    "volume": 0,
                                    "source": "autocomplete",
                                    "seed": seed,
                                    "kind": "query",
                                    "score": item.get("relevance"),
                                })

        # Google Trends "users who search this also search..." – top and rising.
        if "trends" in sources:
            for seed in seeds:
                task = {
                    "keywords": [seed],
                    "location_code": int(location_code),
                    "type": "web",
                    "time_range": "past_12_months",
                    "item_types": ["google_trends_queries_list"],
                }
                if language_code:
                    task["language_code"] = language_code
                data = discover_post("keywords_data/google_trends/explore/live", [task])
                add_cost("trends", data)
                for api_task in data.get("tasks", []):
                    if api_task.get("status_code") != 20000:
                        errors.append(f"Trends ({seed}): " + task_error(api_task))
                        continue
                    for result in api_task.get("result") or []:
                        for item in result.get("items") or []:
                            if item.get("type") != "google_trends_queries_list":
                                continue
                            trends_data = item.get("data") or {}
                            for bucket in ("top", "rising"):
                                for q in trends_data.get(bucket) or []:
                                    query = q.get("query")
                                    if query:
                                        rows.append({
                                            "keyword": query,
                                            "volume": 0,
                                            "source": "trends_" + bucket,
                                            "seed": seed,
                                            "kind": "query",
                                            "score": q.get("value"),
                                        })

        # Live Google SERP signals: PAA, Related searches, People also search, refinements.
        if "serp" in sources:
            for seed in seeds:
                task = {
                    "keyword": seed,
                    "location_code": int(location_code),
                    "depth": 10,
                    "people_also_ask_click_depth": 1,
                }
                if language_code:
                    task["language_code"] = language_code
                data = discover_post("serp/google/organic/live/advanced", [task])
                add_cost("serp", data)
                for api_task in data.get("tasks", []):
                    if api_task.get("status_code") != 20000:
                        errors.append(f"SERP ({seed}): " + task_error(api_task))
                        continue
                    for result in api_task.get("result") or []:
                        rows.extend(serp_direction_rows(result, seed))

        return jsonify(discovery_payload())
    except DiscoveryBudgetReached as exc:
        errors.append(str(exc))
        return jsonify(discovery_payload())
    except InvalidDataForSEOResponse:
        cost_complete = False
        errors.append(
            "DataForSEO returned an empty or invalid response; partial results are shown and the final cost is unknown."
        )
        return jsonify(discovery_payload())
    except aiohttp.ClientResponseError as exc:
        return jsonify({"error": "DataForSEO HTTP error: " + str(exc.status)}), 502
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return jsonify({"error": "Could not connect to DataForSEO"}), 502


if __name__ == "__main__":
    app.run(debug=False, port=5000)
