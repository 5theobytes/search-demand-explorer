# Country and language catalog — specification

**Status:** approved by the owner
**Date:** 2026-09-29

## Goal

Replace the short manually maintained country and language list with the current catalog of supported Google locations and languages from DataForSEO. Each run continues to use one selected country and one language.

## Existing behavior

- `templates/index.html` contains 12 countries with `data-langs` and manually maintained names for eight languages.
- `/api/volume` and `/api/discover` accept `location_code` and `language_code` and pass them to provider requests.
- Discovery can call Google Ads, DataForSEO Labs, Autocomplete, Google Trends and SERP; source-specific errors appear as warnings.

## Design

### Catalog source

Add `POST /api/catalog`, accepting a DataForSEO login and API password in the same way as the existing API routes. The backend fetches `GET /v3/dataforseo_labs/locations_and_languages` using the existing `DFS_BASE` and `auth_header`.

Return only `Country` records. Keep language pairs whose `available_sources` includes `google`; include a country only when at least one supported language remains. Return `location_code`, `location_name`, `country_iso_code` and its `language_code`/`language_name` pairs. The catalog request is free. Validate the response structure; neither credentials nor catalog data are stored on the server.

This catalog defines supported country-language pairs for DataForSEO Labs. Other Google sources may have separate restrictions; their errors remain in the existing discovery warnings field.

### Interface

- Load `/api/catalog` once after restoring or saving credentials.
- Populate the country selector from the catalog, and the language selector from the selected country's languages.
- When the country changes, retain the language if supported there; otherwise select the first published language.
- Preserve the existing fallback list when the catalog is unavailable. Show an understandable warning while keeping the current selection usable.
- Keep catalog data only in tab memory. Credentials retain the application's existing `sessionStorage` behavior.

### Search execution

Keep both search routes and their request formats unchanged. A run uses one selected country; fan-out across multiple countries is outside this feature. Preserve partial results and warnings for optional sources.

## Acceptance criteria

1. A successful catalog load shows supported countries beyond the original 12 values.
2. Only the selected country's published languages are available.
3. Selected codes continue to reach `/api/volume` and `/api/discover` without changing search semantics.
4. Catalog failures disclose no credentials and retain the fallback list with an error message.
5. Search remains a single-country operation.

## Out of scope

- Searching several or all countries simultaneously.
- Catalogs for search engines other than Google.
- Server-side storage of credentials or catalog data.
- Redesigning screens, results or the search API schema.

## Limitations and risks

The list reflects the selected DataForSEO catalog's coverage rather than every country in the world. The provider may add or remove countries and languages. A pair's presence does not guarantee that every connected source returns data for it; the existing discovery flow handles those errors.

## Implementation verification

Check catalog construction from the provider response, fallback on failure, country changes with retention of a supported language, and passing selected codes to both search routes. The original catalog task did not add or run automated tests.
