import asyncio
import math
import time
import aiohttp
from base64 import b64encode
from flask import Flask, jsonify, render_template, request, Response

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


def clean_body():
    return request.get_json(silent=True) or {}


def dataforseo_credentials(body):
    login = str(body.get("login", "")).strip()
    password = str(body.get("password", "")).strip()
    if not login or not password:
        raise ValueError("Введите DataForSEO login и API password")
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
                invalid("DataForSEO вернул некорректный extended_people_also_search")

        for field in ("items", "options"):
            if field not in node or node[field] is None:
                continue
            children = node[field]
            if not isinstance(children, list) or any(
                not isinstance(child, dict) for child in children
            ):
                invalid(f"DataForSEO вернул некорректный список {field}")
            validate_serp_node(children, node_type)

    def validate_volume(record):
        volume = record.get("search_volume")
        if "search_volume" in record and not valid_volume(volume):
            invalid("DataForSEO вернул некорректный search_volume")
        keyword_info = record.get("keyword_info")
        if isinstance(keyword_info, dict):
            volume = keyword_info.get("search_volume")
            if "search_volume" in keyword_info and not valid_volume(volume):
                invalid("DataForSEO вернул некорректный keyword_info.search_volume")
        keyword_data = record.get("keyword_data")
        if isinstance(keyword_data, dict):
            nested_info = keyword_data.get("keyword_info")
            if isinstance(nested_info, dict):
                volume = nested_info.get("search_volume")
                if "search_volume" in nested_info and not valid_volume(volume):
                    invalid(
                        "DataForSEO вернул некорректный "
                        "keyword_data.keyword_info.search_volume"
                    )

    if not isinstance(data, dict):
        invalid("DataForSEO вернул JSON неожиданного формата")
    status_code = data.get("status_code")
    if not isinstance(status_code, int) or isinstance(status_code, bool):
        invalid("DataForSEO вернул некорректный status_code")
    if not valid_cost(data.get("cost")):
        invalid("DataForSEO вернул некорректный cost")

    tasks = data.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        invalid("DataForSEO вернул ответ без массива tasks")

    for task in tasks:
        if not isinstance(task, dict):
            invalid("DataForSEO вернул некорректную запись task")
        task_status = task.get("status_code")
        if not isinstance(task_status, int) or isinstance(task_status, bool):
            invalid("DataForSEO вернул некорректный status_code задачи")
        if not valid_cost(task.get("cost")):
            invalid("DataForSEO вернул некорректный cost задачи")

        result = task.get("result")
        if result is None:
            continue
        if not isinstance(result, list):
            invalid("DataForSEO вернул некорректный массив result")

        for result_item in result:
            if not isinstance(result_item, dict):
                invalid("DataForSEO вернул некорректную запись result")
            validate_volume(result_item)
            validate_serp_node(result_item)
            items = result_item.get("items")
            if items is None:
                continue
            if not isinstance(items, list):
                invalid("DataForSEO вернул некорректный массив items")
            for item in items:
                if not isinstance(item, dict):
                    invalid("DataForSEO вернул некорректную запись items")
                validate_volume(item)
                for field in ("keyword_info", "keyword_data"):
                    value = item.get(field)
                    if value is not None and not isinstance(value, dict):
                        invalid(f"DataForSEO вернул некорректное поле {field}")
                keyword_data = item.get("keyword_data")
                if keyword_data is not None:
                    nested_info = keyword_data.get("keyword_info")
                    if nested_info is not None and not isinstance(nested_info, dict):
                        invalid(
                            "DataForSEO вернул некорректное поле "
                            "keyword_data.keyword_info"
                        )
                if item.get("type") == "google_trends_queries_list":
                    trends_data = item.get("data")
                    if trends_data is None:
                        trends_data = {}
                    elif not isinstance(trends_data, dict):
                        invalid("DataForSEO вернул некорректное поле data")
                    for bucket in ("top", "rising"):
                        queries = trends_data.get(bucket)
                        if queries is not None and (
                            not isinstance(queries, list)
                            or any(not isinstance(query, dict) for query in queries)
                        ):
                            invalid(f"DataForSEO вернул некорректный список {bucket}")

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
                    "DataForSEO вернул некорректный или пустой JSON"
                ) from exc
            return validate_dataforseo_response(data)


def dfs_post(endpoint, payload, login, password, timeout=DFS_TIMEOUT_SECONDS):
    return asyncio.run(_dfs_post(endpoint, payload, login, password, timeout))


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


@app.route("/api/volume", methods=["POST"])
def volume():
    body = clean_body()
    try:
        login, password = dataforseo_credentials(body)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    keywords = [str(k).strip() for k in body.get("keywords", []) if str(k).strip()]
    location_code = body.get("location_code")
    language_code = str(body.get("language_code", "")).strip()

    if not keywords:
        return jsonify({"error": "Введите хотя бы один ключ"}), 400
    if len(keywords) > 1000:
        return jsonify({"error": "За один volume-запрос можно отправить не больше 1000 ключей"}), 400
    if not location_code:
        return jsonify({"error": "Выберите регион"}), 400

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
            "error": "Ответ DataForSEO пустой или некорректный; запрос мог быть обработан и тарифицироваться."
        }), 502
    except aiohttp.ClientResponseError as exc:
        return jsonify({"error": "DataForSEO HTTP error: " + str(exc.status)}), 502
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return jsonify({
            "error": "Ответ DataForSEO не получен; запрос мог быть обработан и тарифицироваться."
        }), 502


@app.route("/api/discover", methods=["POST"])
def discover():
    body = clean_body()
    try:
        login, password = dataforseo_credentials(body)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    seeds = []
    seen = set()
    for value in body.get("keywords", []):
        seed = str(value).strip()
        if seed and seed.casefold() not in seen:
            seen.add(seed.casefold())
            seeds.append(seed)

    location_code = body.get("location_code")
    language_code = str(body.get("language_code", "")).strip()
    sources = body.get("sources") or ["google_ads"]
    sources = [str(x) for x in sources]
    if not seeds:
        return jsonify({"error": "Введите хотя бы один seed-ключ"}), 400
    if not location_code:
        return jsonify({"error": "Выберите регион"}), 400

    try:
        limit = max(10, min(5000, int(body.get("limit", 200))))
    except (TypeError, ValueError):
        limit = 200
    try:
        related_depth = max(1, min(4, int(body.get("related_depth", 1))))
    except (TypeError, ValueError):
        related_depth = 1

    # Per-user-action caps prevent an accidental expensive fan-out from mobile UI.
    if len(seeds) > 200:
        return jsonify({"error": "За один discovery-запуск максимум 200 seed-ключей"}), 400
    expensive_per_seed = {"autocomplete", "labs_suggestions", "labs_related", "trends", "serp"}
    if len(seeds) > 50 and any(s in expensive_per_seed for s in sources):
        return jsonify({"error": "Для Autocomplete/Labs/Trends/SERP максимум 50 seed-ключей за запуск"}), 400

    per_seed_sources = ("labs_suggestions", "labs_related", "autocomplete", "trends", "serp")
    expected_calls = math.ceil(len(seeds) / 20) if "google_ads" in sources else 0
    expected_calls += int("labs_ideas" in sources)
    expected_calls += len(seeds) * sum(source in sources for source in per_seed_sources)
    if expected_calls > DISCOVERY_MAX_CALLS:
        return jsonify({
            "error": (
                f"Этот запуск потребует {expected_calls} запросов DataForSEO; "
                f"максимум за один запуск — {DISCOVERY_MAX_CALLS}. "
                "Уменьшите число seed-ключей или выберите меньше источников."
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
            raise DiscoveryBudgetReached("Достигнут лимит запросов DataForSEO; показаны частичные результаты.")
        remaining = deadline - time.monotonic()
        if remaining <= 5:
            raise DiscoveryBudgetReached("Лимит времени discovery достигнут; показаны частичные результаты.")
        timeout = min(DFS_TIMEOUT_SECONDS, max(1, remaining - 5))
        request_count += 1
        try:
            data = dfs_post(endpoint, payload, login, password, timeout=timeout)
        except InvalidDataForSEOResponse as exc:
            cost_complete = False
            raise DiscoveryBudgetReached(
                "DataForSEO вернул пустой или некорректный ответ; результаты частичные, итоговая стоимость неизвестна."
            ) from exc
        except aiohttp.ClientResponseError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            cost_complete = False
            raise DiscoveryBudgetReached(
                "DataForSEO не ответил вовремя; результаты частичные, итоговая стоимость неизвестна."
            ) from exc
        if time.monotonic() >= deadline and not any("Лимит времени discovery достигнут" in error for error in errors):
            errors.append("Лимит времени discovery достигнут; показаны полученные результаты.")
        return data

    def discovery_payload():
        clean = dedupe_rows(rows, deadline=processing_deadline)
        clean.sort(key=lambda r: (r.get("volume") or 0, r.get("score") or 0), reverse=True)
        if time.monotonic() >= processing_deadline and not any("Обработка результатов прервана" in error for error in errors):
            errors.append("Обработка результатов прервана по лимиту времени; показан собранный фрагмент.")
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
            "DataForSEO вернул пустой или некорректный ответ; показаны частичные результаты, итоговая стоимость неизвестна."
        )
        return jsonify(discovery_payload())
    except aiohttp.ClientResponseError as exc:
        return jsonify({"error": "DataForSEO HTTP error: " + str(exc.status)}), 502
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return jsonify({"error": "Не удалось связаться с DataForSEO"}), 502


if __name__ == "__main__":
    app.run(debug=False, port=5000)
