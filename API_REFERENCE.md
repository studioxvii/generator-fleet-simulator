# API Reference

This file documents the current REST API for the Generator Fleet Simulator.

## Conventions

- All write endpoints expect JSON.
- Errors return JSON with an `error` field.
- Most successful write operations return `{ "ok": true, ... }`.

## `GET /api/health`

Returns current service health and runtime metadata.

Fields include:

- `ok`
- `configured`
- `web_host`
- `web_port`
- `modbus_host`
- `modbus_port`
- `modbus_running`
- `num_generators`
- `active_scenario`

## `GET /api/live`

Returns web-process liveness. This endpoint is intentionally shallow and is
used by the Docker health check.

```json
{
  "ok": true,
  "live": true
}
```

## `GET /api/ready`

Returns simulator readiness. This endpoint returns HTTP 200 only after the
simulator runtime is configured and the Modbus server thread is running.
Before startup or when the user chose browser configuration and has not
completed it yet, this endpoint returns HTTP 503 with `ready: false`.

Fields include:

- `ok`
- `ready`
- `configured`
- `web_host`
- `web_port`
- `modbus_host`
- `modbus_port`
- `modbus_running`
- `num_generators`

## `GET /api/state`

Returns simulator state.

Before startup, returns `{ "configured": false, "generators": [] }`.

After startup, returns:

```json
{
  "configured": true,
  "num_generators": 15,
  "summary": {},
  "generators": [],
  "subfleets": []
}
```

## `GET /api/fleet/summary`

Returns a compact fleet summary and metrics snapshot.

Fields:

- `configured`
- `summary` — totals and counts across all generators
- `metrics` — command queue depth and performance counters

## `GET /api/fleet/generators`

Returns a paginated, filterable list of generator states.

Query params:

- `page` — page number (default `1`)
- `page_size` — items per page (default `50`, max `250`)
- `sort` — field to sort by (default `unit_id`)
- `direction` — `asc` or `desc`
- `state` — filter by state (`STOPPED`, `CRANKING`, `RUNNING`, `COOLDOWN`, `FAULT`)
- `rated_kw` — filter by rated kW
- `alarmed` — `1` to return only units with active alarms
- `auto_mode` — `true` or `false`
- `subfleet_id` — filter to a specific subfleet
- `subfleet_scope` — `any`, `assigned`, `unassigned`, `active`, or `other`

## `GET /api/fleet/generator-search`

Returns a flat filtered list of generators (up to 200 results), optimized for type-ahead search and assignment flows.

Query params are the same as `/api/fleet/generators`, plus:

- `limit` — max results to return (default `50`, max `200`)
- `search` — text search across unit ID and name
- `context_subfleet_id` — used with `subfleet_scope=other` to exclude a specific subfleet

## `GET /api/generators/<unit_id>`

Returns the full state snapshot for a single generator unit.

Returns `404` if the simulator is not started or the unit does not exist.

## `GET /api/generators/<unit_id>/registers`

Returns raw Modbus holding register values for a single generator unit.

Returns `404` if the simulator is not started or the unit does not exist.

## `GET /api/subfleets`

Returns the list of all configured subfleets.

## `POST /api/subfleets`

Creates a new subfleet.

```json
{
  "name": "North Block"
}
```

Returns `201` on success with the created subfleet object.

## `PATCH /api/subfleets/<subfleet_id>`

Renames a subfleet.

```json
{
  "name": "North Block Revised"
}
```

## `DELETE /api/subfleets/<subfleet_id>`

Deletes a subfleet and releases all member assignments.

## `POST /api/subfleets/<subfleet_id>/members`

Assigns specific generator unit IDs to a subfleet.

```json
{
  "unit_ids": [1, 3, 5]
}
```

## `POST /api/subfleets/<subfleet_id>/members/query`

Queries for generators matching filter criteria and assigns all matching units to the subfleet.

```json
{
  "state": "RUNNING",
  "rated_kw": 1000,
  "subfleet_scope": "unassigned"
}
```

Returns the matched unit count and the list of assigned unit IDs.

## `DELETE /api/subfleets/<subfleet_id>/members/<unit_id>`

Removes a single generator from a subfleet.

## `POST /api/subfleets/<subfleet_id>/members/query-remove`

Removes generators matching a search term from a subfleet.

```json
{
  "search": "GEN-03"
}
```

## `POST /api/subfleets/<subfleet_id>/commands`

Queues a command to be sent to every member of the subfleet.

```json
{
  "cmd": 1
}
```

See command values in the Modbus reference.

## `POST /api/startup`

Starts or restarts the simulator with a generator size mix.

```json
{
  "size_counts": {
    "500": 5,
    "1000": 3,
    "1500": 2,
    "2000": 0,
    "2500": 0
  },
  "modbus_port": 5020
}
```

Total generators cannot exceed 2000. `modbus_port` is optional (defaults to the configured value).

## `GET /api/scada/topology`

Returns the hierarchy used by the One-Line SCADA tab. The root is always the
fleet; children drill into sub-fleet/unassigned groups, generator size, unit
ranges, and units. Each node includes live rollups for unit count, running
count, kW, utility load, fault count, alarm count, and breaker counts.

Query params:

- `node_id` — selected hierarchy node (default `fleet`)
- `range_size` — number of units per generated range node (default `50`, max `250`)

Common node IDs:

- `fleet`
- `group|subfleet|<subfleet_id>`
- `group|unassigned|-`
- `size|<scope_kind>|<scope_id>|<rated_kw>`
- `range|<scope_kind>|<scope_id>|<rated_kw>|<start_unit>|<end_unit>`
- `unit|<unit_id>`

Returns `404` if the selected node does not exist.

## `GET /api/scada/alarms`

Returns actionable alarms scoped to a SCADA hierarchy selection.

Query params:

- `node_id` — selected hierarchy node (default `fleet`)
- `limit` — max alarm rows to return (default `200`, max `500`)

The response includes `alarm_count`, `alarms`, `truncated`, and `limit`.
Status bits such as ready/running are not returned as actionable alarms.

## `GET /api/scenarios`

Returns the scenario catalog, the currently active run (if any), and recent history.

## `POST /api/scenarios/run`

Starts a named scenario.

```json
{
  "scenario_id": "utility-fail-recovery"
}
```

Built-in scenarios:

- `utility-fail-recovery` — simulates a utility outage and recovery
- `fault-and-reset` — injects a fault and resets a unit
- `parallel-mode-demo` — demonstrates parallel load sharing
- `e-stop-drill` — fleet-wide emergency stop and reset

## `POST /api/scenarios/stop`

Stops the currently running scenario without stopping the simulator.

## `GET /api/export/config.csv`

Exports the current fleet configuration, Modbus register map, alarm bits, and command catalog as a CSV file.

## `GET /api/metrics`

Returns runtime metrics including command queue depth, tick rate, and connected client count.

Returns `{ "configured": false, "metrics": null }` if the simulator is not started.

## Browser security and protocol limits

REST writes reject cross-site browser requests. Use the same origin as the
dashboard. Direct API clients can omit browser Origin and Referer headers.
This is not authentication; keep the server local or on an isolated network.
`GENSIM_TRUSTED_HOSTS` permits additional exact web host names when needed.

The register API supports all configured generator IDs. Modbus TCP has a
one-byte unit field. The server uses 255 generators per TCP port and increments
the port for the next group. `GET /api/modbus/endpoints` returns active ranges
with internal `port` and published `public_port`. The register response adds
`modbus: {"port": 5021, "public_port": 5022, "unit_id": 1}` for generator 256
with the standard Compose settings. Its top-level `unit_id` remains 256.
The config CSV adds a `modbus_mapping` section with each generator address.
`GET /api/ready` also requires the simulation loop to be running. It returns
503 after that loop exits, even if the Modbus thread is still alive.
