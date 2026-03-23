# Folium Map Visualization Implementation Plan

This document captures the recommended steps to add map visualization (addresses + route) to the current email-driven TSP optimizer.

## Goal

Generate an HTML map per optimized request using `folium`, showing:

- start location
- ordered stops
- route line
- useful popup details

Output should be saved locally (MVP), then optionally referenced in reply emails and/or attached.

## Scope Overview

### Phase 1 (MVP - recommended first)

- Add a map renderer adapter using `folium`.
- Save map files to `data/maps/<request_id>.html`.
- Draw:
  - start marker
  - numbered stop markers
  - polyline through optimized stops (straight segments)
- Optionally include map file path in reply text.

### Phase 2 (quality improvement)

- Render a road-following route geometry instead of straight lines.
- Use ORS directions geometry for the optimized sequence.
- Improve marker colors/icons and popup metadata.

### Optional Phase 3 (distribution)

- Attach map HTML to outgoing email.
- Or host/share map URL via another channel (if needed).

## Detailed Implementation Steps

## 1) Add dependency

Update `requirements.txt`:

- add `folium`

Install:

- `pip install -r requirements.txt`

## 2) Add configuration for map output

Update `src/tsp_email_optimizer/config.py`:

- add `map_enabled: bool` (default `true`)
- add `map_output_path: str` (default `./data/maps`)

Suggested env vars:

- `MAP_ENABLED=true`
- `MAP_OUTPUT_PATH=./data/maps`

## 3) Define a map rendering port (clean architecture)

Update `src/tsp_email_optimizer/domain/ports.py` with a protocol, for example:

- `RouteMapRenderer.render(route: OptimizedRoute) -> str | None`

Return value:

- absolute/relative path to generated HTML file, or `None` when disabled.

This keeps `EmailOptimizationPipeline` independent from concrete `folium` code.

## 4) Implement Folium adapter

Create:

- `src/tsp_email_optimizer/adapters/visualization/folium_route_map.py`

Responsibilities:

- create output directory
- build `folium.Map` centered on start or first stop
- add start marker (distinct style)
- add numbered stop markers (`1..N`)
- add line through `[start] + ordered_stops` or just ordered stops (based on desired behavior)
- fit bounds to all points
- save HTML file as `<request_id>.html`
- return saved path

Suggested popup content:

- request id
- stop order
- original index
- address
- geocode confidence (if available)

## 5) Wire adapter in composition root

Update `src/tsp_email_optimizer/main.py`:

- instantiate the folium renderer in `build_pipeline()`
- pass it into pipeline constructor

If `map_enabled` is false, inject a no-op implementation or `None`.

## 6) Integrate into pipeline flow

Update `src/tsp_email_optimizer/services/pipeline.py`:

- after optimization + persistence, call map renderer
- capture returned `map_path`
- continue even if map generation fails (log warning/error only)

Recommended behavior:

- map generation should not block sending successful optimization replies
- errors in map rendering should not flip request status to failed

## 7) Include map reference in reply

Update `src/tsp_email_optimizer/services/reply_builder.py`:

- extend `build_success_reply(...)` to accept optional `map_path: str | None`
- add a line such as `Mapa generado en: <path>` when available

This keeps user visibility without changing SMTP attachment logic yet.

## 8) Logging and observability

Add logs for:

- map generation started/completed
- output file path
- number of rendered points
- non-fatal rendering failures

## 9) Testing checklist

### Unit-level

- renderer creates file for non-empty routes
- renderer handles missing `start_location`
- renderer handles single/empty points defensively
- pipeline tolerates renderer exceptions

### Manual E2E

1. Run app once with a known test email.
2. Confirm optimization still works and reply is sent.
3. Verify HTML exists in `data/maps/`.
4. Open map and validate marker order and labels.
5. Confirm reply includes map path (if enabled).

## 10) Phase 2 enhancement details (road geometry)

To improve visual accuracy:

- call ORS directions for optimized coordinate sequence
- decode returned geometry/polyline
- render decoded line in `folium`

Notes:

- this adds one additional ORS call per processed request
- keep graceful fallback to straight-line polyline if geometry call fails

## 11) Estimated effort

- Phase 1 MVP: ~2-4 hours
- Phase 2 road geometry: +3-6 hours
- Email attachment support: +2-4 hours

## 12) Suggested file change list

- `requirements.txt`
- `src/tsp_email_optimizer/config.py`
- `src/tsp_email_optimizer/domain/ports.py`
- `src/tsp_email_optimizer/main.py`
- `src/tsp_email_optimizer/services/pipeline.py`
- `src/tsp_email_optimizer/services/reply_builder.py`
- `src/tsp_email_optimizer/adapters/visualization/folium_route_map.py` (new)

## 13) Acceptance criteria for MVP

- app still processes emails end-to-end
- optimized routes continue to be saved to CSV
- for successful optimizations, an HTML map is generated per `request_id`
- map clearly shows start + ordered stops + connecting path
- map generation failures do not break optimization/reply flow
