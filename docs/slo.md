# KubeCommerce - Service Level Objectives (SLOs)

These SLOs cover the **public API**, i.e. requests that reach `gateway-api`.

> These are **demonstration targets**, not measured claims of real-world reliability.
> They become evidence only after the observability stack (Phase 9/10) has recorded
> traffic for the stated window. Do not quote them as achieved until measured.

## 1. Objectives

| SLO | Target | Window |
|---|---|---|
| Availability | **99.9%** of valid requests are successful | rolling 30 days |
| Latency | **95%** of valid requests complete in **< 500 ms** | rolling 30 days |

## 2. What counts as a successful request

A request is **counted in the SLI** (in-scope) when it reaches the gateway and is
attributable to a valid client identity/route. A request is **successful** when the
response is not a server-side or unexpected failure.

**Failures (numerator excluded from success):**

- all `5xx` responses (server errors, including dependency failures surfaced as 502/503/504);
- timeouts emitted by the gateway/fronting gateway (treated as errors).

**Intentional client outcomes excluded from both numerator and denominator failure set**
(i.e. they do not burn error budget), with justification:

| Outcome | Why excluded |
|---|---|
| `401 Unauthorized` | A client without/with an expired token is expected; it is the auth mechanism working, not a service failure. |
| `404 Not Found` | Expected for genuinely absent resources (e.g. probing a deleted id); it does not indicate service ill-health. |
| `422 Unprocessable Entity` | Expected request-schema/validation rejection; the service is functioning correctly. |

All other `4xx` (e.g. `400`, `403`, `429`) are treated as **neutral**: they stay in the
denominator but are not counted as successes. `429` (rate-limited) specifically indicates
the service correctly protecting itself; it should be monitored separately but is not a
reliability failure. If rate limiting ever dominates traffic, revisit the objective.

> Rationale for the exclusion list: error budgets should measure the reliability of the
> *service*, not the mistakes of callers. Excluding every 4xx would hide real problems,
> so only these well-understood, intentional cases are excluded.

## 3. SLIs (PromQL sketches)

Metrics follow the RED pattern exposed by the shared observability library:
`http_requests_total{service,method,route,status}` and
`http_request_duration_seconds_bucket{service,route,le}`.

### Availability SLI (success ratio, 5-minute rate)

```promql
# numerator: successful responses (exclude 5xx and the intentional 4xx set)
sum(rate(http_requests_total{service="gateway-api",status!~"5..",status!~"4(01|04|22)"}[5m]))
/
# denominator: all responses
sum(rate(http_requests_total{service="gateway-api"}[5m]))
```

### Availability over the rolling 30-day window

```promql
sum(increase(http_requests_total{service="gateway-api",status!~"5..",status!~"4(01|04|22)"}[30d]))
/
sum(increase(http_requests_total{service="gateway-api"}[30d]))
```

### Latency SLI (fraction under 500 ms, 5-minute rate)

```promql
sum(rate(http_request_duration_seconds_bucket{service="gateway-api",le="0.5"}[5m]))
/
sum(rate(http_request_duration_seconds_count{service="gateway-api"}[5m]))
```

> The histogram buckets must include `0.5`. Use consistent `route` **templates**, never
> raw URLs with IDs (high cardinality).

## 4. Error budget

For a 99.9% availability SLO over a rolling 30-day window:

```text
Total window          = 30 days × 24 h × 60 min = 43,200 minutes
Allowed failure ratio = 1 − 0.999 = 0.001 (0.1%)
Error budget          = 0.001 × 43,200 min = 43.2 min = 43 min 12 s
```

So the public API may accumulate at most **43 minutes 12 seconds** of failure time (or
0.1% of requests) per 30 days before the objective is missed.

Error budget remaining (%):

```promql
1 - (
  (1 - (sum(rate(http_requests_total{service="gateway-api",status!~"5..",status!~"4(01|04|22)"}[30d]))
        / sum(rate(http_requests_total{service="gateway-api"}[30d]))))
  / 0.001
)
```

## 5. Burn-rate alerting (advanced extension)

Burn rate = `error_ratio / (1 − SLO)` = observed failure rate / 0.001. A burn rate of 1
consumes the entire budget in exactly the window.

| Alert | Long window | Short window | Burn rate | Budget consumed | Action |
|---|---|---|---|---|---|
| Fast burn (page) | 1 h | 5 m | 14.4 | 2% in 1 h | page on-call |
| Medium burn (ticket) | 6 h | 30 m | 6 | 5% in 6 h | investigate |
| Slow burn (ticket) | 3 d | 6 h | 1 | 10% in 3 d | backlog triage |

Example multi-window expression shape:

```promql
# error ratio over 1h and 5m, alert when both exceed 14.4 × 0.001
(sum(rate(http_requests_total{service="gateway-api",status=~"5.."}[1h]))
 / sum(rate(http_requests_total{service="gateway-api"}[1h])))
> 0.0144
and
(sum(rate(http_requests_total{service="gateway-api",status=~"5.."}[5m]))
 / sum(rate(http_requests_total{service="gateway-api"}[5m])))
> 0.0144
```

Latency counterpart: alert when the p95 latency SLO (95% < 500 ms) is breached for a
sustained window, e.g. the under-500 ms fraction `< 0.95` for 10 minutes. This aligns with
the guide's example alert `p95 latency > 750 ms for 10 minutes`; the 750 ms value is the
alerting threshold, not the SLO.

## 6. Grafana panels (Phase 9)

Dashboard "API SLO" panels:

1. **SLI success ratio** - `Availability SLI` (time series, with 0.999 target line).
2. **SLO target** - stat/annotation showing `99.9%` and the rolling 30-day value.
3. **Error budget remaining** - stat/gauge of the PromQL above (green/amber/red bands).
4. **Burn rate** - time series with thresholds at 1, 6, 14.4.
5. **Latency SLI** - fraction under 500 ms, with 0.95 target line.
6. **p50 / p95 / p99 latency** - supporting RED view.
7. **Error rate by status class** - 4xx (excluding intentional) vs 5xx.
8. **Error budget burn-down** - budget consumed over the window.

## 7. Relationship to other targets

- Alert rules and dashboards are defined in Phase 9; this document is the contract they
  implement.
- The load test (`make load-test`) provides the traffic used to demonstrate scaling and
  dashboards; it is not a substitute for a measured 30-day window.
