# Google Geocoding Integration Plan

This document outlines what would be needed to replace the current OpenRouteService (ORS) geocoding step with Google Geocoding, while keeping ORS for route optimization.

## Goal

Improve address resolution quality by using Google Geocoding for:

- trip stop addresses
- optional start/departure address

Keep ORS for:

- route optimization (`/optimization`)
- route map rendering paths that still depend on the ORS key

## Current State

Today, `src/tsp_email_optimizer/adapters/routing/ors_optimizer.py` does two jobs:

- geocodes addresses with `https://api.openrouteservice.org/geocode/search`
- optimizes the route with `https://api.openrouteservice.org/optimization`

Related wiring:

- `src/tsp_email_optimizer/config.py` loads `OPENROUTESERVICE_API_KEY`
- `src/tsp_email_optimizer/main.py` injects that key into `OpenRouteServiceOptimizer`
- `.env.example` only documents the ORS key, not a Google Maps key

This means switching geocoding providers is mostly an adapter/config change, but the geocoding logic is currently coupled to the ORS optimizer class.

## Recommended Approach

Recommended implementation path:

1. Keep ORS as the optimization provider.
2. Introduce a dedicated Google geocoding adapter.
3. Move address lookup responsibility out of `OpenRouteServiceOptimizer`, or at minimum isolate it behind a private helper abstraction.

Why this is the better approach:

- it separates "get coordinates" from "optimize route"
- it avoids a class named `OpenRouteServiceOptimizer` containing Google-specific geocoding behavior
- it makes future provider fallback easier (for example Google first, ORS fallback later)

Minimal-change alternative:

- keep `OpenRouteServiceOptimizer` as the main class
- replace only `_query_best_geocode_candidate()` and related scoring logic with Google-specific code

This would be faster, but it would leave the class name and responsibilities misleading.

## Google Requirements

To use Google Geocoding, you would need:

- a Google Cloud project
- billing enabled for that project
- the Geocoding API enabled
- an API key with appropriate restrictions

Operational implications:

- Google Geocoding is billable
- usage is quota-limited
- quota overages return Google-specific statuses such as `OVER_QUERY_LIMIT`
- some failures are returned as response payload statuses instead of HTTP 4xx/5xx only

## Proposed Configuration Changes

Update `src/tsp_email_optimizer/config.py` and `.env.example`.

### Required new env var

- `GOOGLE_GEOCODING_API_KEY`

### Recommended optional env vars

- `GOOGLE_GEOCODING_LANGUAGE=es`
- `GOOGLE_GEOCODING_REGION=cl`
- `GOOGLE_GEOCODING_COMPONENTS=country:CL`

These optional fields would help bias results toward Chile and Spanish-language addresses, which looks aligned with the current inputs and logs.

### Keep existing env vars

Do not remove:

- `OPENROUTESERVICE_API_KEY`
- `ORS_PROFILE`

They are still required for optimization and map rendering.

## Suggested Code Changes

## 1) Add a Google geocoding adapter

Create a new file such as:

- `src/tsp_email_optimizer/adapters/geocoding/google_geocoder.py`

Suggested responsibility:

- `geocode_one(address: str) -> tuple[float, float, float | None] | None`

Possible implementation details:

- call `https://maps.googleapis.com/maps/api/geocode/json`
- pass `address=<input>`
- pass `key=<GOOGLE_GEOCODING_API_KEY>`
- optionally pass `language`, `region`, and `components`
- parse `results[0].geometry.location.lat/lng`

## 2) Decide how to integrate it with routing

Recommended structure:

- keep route optimization in `src/tsp_email_optimizer/adapters/routing/ors_optimizer.py`
- inject a geocoder dependency into that optimizer

Example direction:

- rename `OpenRouteServiceOptimizer` later to something provider-neutral like `RouteOptimizer`
- or keep the name for now and accept a `geocoder` object in `__init__`

If you want the lowest-risk first step, keep the current class name and inject the geocoder.

## 3) Update composition root

Update `src/tsp_email_optimizer/main.py` so that:

- ORS optimizer still receives the ORS key and profile
- Google geocoder receives the Google API key and geocoding options
- the optimizer uses the Google geocoder for address lookup

## 4) Update config model

Extend `AppConfig` in `src/tsp_email_optimizer/config.py` with fields like:

- `google_geocoding_api_key: str`
- `google_geocoding_language: str | None`
- `google_geocoding_region: str | None`
- `google_geocoding_components: str | None`

## 5) Update env template

Extend `.env.example` with:

- `GOOGLE_GEOCODING_API_KEY=your_google_maps_key`
- `GOOGLE_GEOCODING_LANGUAGE=es`
- `GOOGLE_GEOCODING_REGION=cl`
- `GOOGLE_GEOCODING_COMPONENTS=country:CL`

## Google-Specific Matching Differences

This is the main non-trivial part of the change.

The current ORS logic depends on provider-specific fields:

- ORS returns candidate `features`
- candidate scoring uses ORS `properties.confidence`
- the code rejects results below `_MIN_ACCEPTED_CONFIDENCE`
- the code also blends provider confidence with token overlap and city/country hints

Google Geocoding does not expose the same `confidence` field, so the scoring logic must be adapted.

### Current ORS-specific code areas to revisit

In `src/tsp_email_optimizer/adapters/routing/ors_optimizer.py`, these methods are the main change points:

- `_geocode_one()`
- `_query_best_geocode_candidate()`
- `_build_scored_candidate()`
- `_build_feature_text()`

### Recommended Google scoring policy

Instead of ORS confidence, derive a quality score from:

- token overlap between input address and returned address components
- `partial_match` penalty
- `location_type` bonus/penalty
- Chile/city hints bonus
- result type preference

Suggested heuristics:

- prefer `street_address` over broader types like `route` or `locality`
- prefer `ROOFTOP` over `RANGE_INTERPOLATED`
- penalize `GEOMETRIC_CENTER` and `APPROXIMATE`
- penalize `partial_match=true`
- keep the existing token overlap logic because it is provider-agnostic and already valuable

### Handling `geocode_confidence`

The domain model `GeocodedStop.geocode_confidence` is optional, so there are two valid options:

1. store a derived internal score normalized to `0..1`
2. store `None` for Google results and rely on notes/logging instead

Recommendation:

- keep the field populated with a derived normalized score so existing CSV exports remain informative

## Request/Response Handling Changes

Google Geocoding requires slightly different error handling than ORS geocoding.

Recommended behavior:

- treat HTTP transport failures as retryable errors
- inspect JSON `status`
- handle `OK` with parsed results
- handle `ZERO_RESULTS` as an unresolved address
- handle `OVER_QUERY_LIMIT`, `OVER_DAILY_LIMIT`, `REQUEST_DENIED`, and `INVALID_REQUEST` as hard failures with explicit logs
- handle `UNKNOWN_ERROR` as retryable
- log `error_message` when present

## Caching Impact

The existing geocode cache can largely stay as-is.

Current cache file:

- `data/geocode_cache.csv` via `geocode_cache_path`

Recommended cache changes:

- add a `provider` column so cached ORS and Google results are distinguishable
- optionally add `formatted_address`
- optionally add `location_type`
- optionally add `partial_match`

Why this matters:

- old ORS cache entries could mask new Google behavior
- provider-aware cache records make migration safer and easier to debug

## Migration Considerations

Before enabling Google geocoding in production-like runs:

1. Decide whether to invalidate the existing cache entirely.
2. Or version the cache format and only trust rows with `provider=google`.

Recommendation:

- safest path is to version the cache or start a new cache file for Google-backed geocoding

## Logging Changes

Update logs to make the provider explicit.

Suggested logging improvements:

- log `provider=google` on geocode requests
- log returned `formatted_address`
- log `location_type`
- log `partial_match`
- keep API key masking behavior

This will make it much easier to compare ORS vs Google quality during rollout.

## Testing Plan

There is currently no `tests/` directory, so verification should be split into targeted unit tests and manual checks.

### Unit tests to add

- parses a successful Google geocoding response
- returns `None` for `ZERO_RESULTS`
- raises a clear error for `REQUEST_DENIED` or quota issues
- penalizes `partial_match` correctly
- prefers `ROOFTOP` over `APPROXIMATE`
- preserves cache behavior for repeated lookups

### Manual validation cases

Use a set of real addresses that are currently problematic in ORS, especially:

- incomplete street addresses
- addresses with comuna/city hints
- addresses containing `entre` / `esquina`
- addresses in Vina del Mar / Valparaiso region

Validation checklist:

1. Compare ORS vs Google geocode results for the same input set.
2. Confirm more addresses resolve successfully.
3. Confirm lat/lon still work with ORS optimization.
4. Confirm low-quality Google matches are not silently accepted.
5. Confirm CSV output and reply flow still work normally.

## Rollout Strategy

Recommended rollout in two small phases:

### Phase 1

- add Google geocoder
- wire it into the optimizer
- keep current behavior elsewhere unchanged

### Phase 2

- improve scoring heuristics based on real failed addresses
- decide whether to add provider fallback
- optionally rename/refactor the optimizer class for cleaner boundaries

## File Change List

Expected files to update:

- `src/tsp_email_optimizer/config.py`
- `src/tsp_email_optimizer/main.py`
- `src/tsp_email_optimizer/adapters/routing/ors_optimizer.py`
- `.env.example`

Expected new file:

- `src/tsp_email_optimizer/adapters/geocoding/google_geocoder.py`

Optional follow-up refactor files:

- `src/tsp_email_optimizer/domain/ports.py` if you decide to formalize a geocoder interface

## Dependency Impact

No new Python dependency should be required for the basic integration.

The project already uses `requests`, which is sufficient for calling Google Geocoding.

## Risks and Open Decisions

- Billing must be enabled on Google Cloud.
- Google quota and costs need to be monitored.
- Some ambiguous addresses may still need custom scoring rules.
- You need to decide whether to keep a single optimizer class or separate geocoding/routing more cleanly.
- You need to decide whether to preserve `geocode_confidence` as a derived metric or leave it empty for Google responses.

## Recommended Next Implementation Order

1. Add Google env vars to config and `.env.example`.
2. Implement a standalone Google geocoder adapter.
3. Integrate it into the current optimizer without changing route optimization logic.
4. Make cache provider-aware.
5. Validate against the address samples that currently fail in ORS.
6. Only then consider refactoring class names and interfaces.
