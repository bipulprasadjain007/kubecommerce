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

A request is **counted in the SLI** (in-scope) when it reaches the gateway, is
attributable to a valid client identity/route, and targets a real API route.
Operational endpoints are **not** in scope: `/health/*` (liveness/readiness/startup)
and `/metrics` are probed constantly by Kubernetes and Prometheus, and the shared
middleware records them, so their 200s would otherwise dilute the success ratio.
The availability SLI selectors therefore always carry `route!~"/health.*|/metrics"`.

**Successful (numerator):** the response is neither a server-side failure nor one of
the intentional client outcomes below - i.e. status is **not `5xx`, not `401`,
not `404`, and not `422`**.

**Failures:** only `5xx` responses (server errors, including dependency failures
surfaced as 502/503/504) and gateway timeouts (recorded as 5xx/connection errors)
count against the objective. They stay in the denominator as failures.

**Intentional client outcomes excluded from both numerator and denominator failure set**
(i.e. they do not burn error budget), with justification:

| Outcome | Why excluded |
|---|---|
| `401 Unauthorized` | A client without/with an expired token is expected; it is the auth mechanism working, not a service failure. |
| `404 Not Found` | Expected for genuinely absent resources (e.g. probing a deleted id); it does not indicate service ill-health. |
| `422 Unprocessable Entity` | Expected request-schema/validation rejection; the service is functioning correctly. |

All other `4xx` (e.g. `400`, `403`, `429`) are **in-scope and non-failing**: the
numerator only removes `5xx` and the three intentional statuses, so these requests
remain in both the numerator and the denominator and do not burn budget. Treat them
as non-failures, not as evidence of health. `429` (rate-limited) specifically
indicates the service correctly protecting itself and should be monitored
separately; if a non-failing class comes to dominate traffic, revisit the objective.

> Rationale for the exclusion list: error budgets should measure the reliability of the
> *service*, not the mistakes of callers. Excluding every 4xx would hide real problems,
> so only these well-understood, intentional cases are excluded.

**The exact availability SLI** (success ratio; `kubecommerce_http_*` are the exported
metric names) is:

```promql
# denominator: in-scope responses (drop probes and the intentional client errors)
sum(rate(kubecommerce_http_requests_total{route!~"/health.*|/metrics",status!~"4(01|04|22)"}[5m]))
/
# numerator: in-scope responses that are also not 5xx
sum(rate(kubecommerce_http_requests_total{route!~"/health.*|/metrics",status!~"5..",status!~"4(01|04|22)"}[5m]))
```

## 3. SLIs (PromQL sketches)

Metrics follow the RED pattern exposed by the shared observability library:
`kubecommerce_http_requests_total{service,method,route,status}` and
`kubecommerce_http_request_duration_seconds_bucket{service,route,le}`.

### Availability SLI (success ratio, 5-minute rate)

```promql
# denominator: in-scope responses (drop probes and intentional client errors)
sum(rate(kubecommerce_http_requests_total{service="gateway-api",route!~"/health.*|/metrics",status!~"4(01|04|22)"}[5m]))
/
# numerator: in-scope responses that are not 5xx
sum(rate(kubecommerce_http_requests_total{service="gateway-api",route!~"/health.*|/metrics",status!~"5..",status!~"4(01|04|22)"}[5m]))
```

These sketches show a single service (`gateway-api`) for readability. The alert rules
aggregate with `sum by (namespace, service)` so every service is covered; filtering these
expressions by `service="<name>"` gives the equivalent per-service view.

### Availability over the rolling 30-day window

```promql
# denominator: in-scope responses over 30 days
sum(increase(kubecommerce_http_requests_total{service="gateway-api",route!~"/health.*|/metrics",status!~"4(01|04|22)"}[30d]))
/
# numerator: in-scope responses that are not 5xx
sum(increase(kubecommerce_http_requests_total{service="gateway-api",route!~"/health.*|/metrics",status!~"5..",status!~"4(01|04|22)"}[30d]))
```

### Latency SLI (fraction under 500 ms, 5-minute rate)

```promql
sum(rate(kubecommerce_http_request_duration_seconds_bucket{service="gateway-api",le="0.5"}[5m]))
/
sum(rate(kubecommerce_http_request_duration_seconds_count{service="gateway-api"}[5m]))
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
# 1 - (burn consumed / tolerated burn), using the exact in-scope definition above
1 - (
  (1 - (sum(rate(kubecommerce_http_requests_total{service="gateway-api",route!~"/health.*|/metrics",status!~"5..",status!~"4(01|04|22)"}[30d]))
        / sum(rate(kubecommerce_http_requests_total{service="gateway-api",route!~"/health.*|/metrics",status!~"4(01|04|22)"}[30d]))))
  / 0.001
)
```

## 5. Error-budget burn-rate alerting

Burn rate = `error_ratio / (1 − SLO)` = observed error ratio / 0.001. A burn rate of
1 consumes exactly the whole budget over the measurement window; 14.4x consumes 2% of
the 30-day budget in 1 hour, and 6x consumes 5% in 6 hours.

Two **multi-window** rules implement this in
`kubecommerce-gitops/platform/monitoring/rules/kubecommerce-alerts.yaml` (group
`kubecommerce.slo.burnrate`). Each requires the burn rate to exceed the threshold on
a **short** and a **long** window before firing, which suppresses brief spikes while
still catching a sustained burn:

| Alert | Short window | Long window | Burn rate | Budget consumed | Severity / action |
|---|---|---|---|---|---|
| `KubeCommerceSLOBurnRateFast` | 5 m | 1 h | 14.4x | 2% in 1 h | `critical` - page on-call |
| `KubeCommerceSLOBurnRateSlow` | 30 m | 6 h | 6x | 5% in 6 h | `warning` - open a ticket |

Threshold = burn rate × (1 − SLO) = burn rate × 0.001, i.e. `0.0144` for the fast
rule and `0.006` for the slow rule. Using the **exact availability SLI** from
section 2, the error ratio is `1 − success_ratio`, where
`success = rate(status!~"5..", status!~"4(01|04|22)")` and
`in-scope = rate(status!~"4(01|04|22)")`, both filtered by
`route!~"/health.*|/metrics"`. The rules in `kubecommerce-alerts.yaml` implement
exactly these expressions:

```promql
# Fast burn (page): error ratio > 14.4 * 0.001 on BOTH 5m and 1h
(
  (1 - (
    sum by (namespace, service) (
      rate(kubecommerce_http_requests_total{route!~"/health.*|/metrics",status!~"5..",status!~"4(01|04|22)"}[5m])
    )
    / sum by (namespace, service) (
      rate(kubecommerce_http_requests_total{route!~"/health.*|/metrics",status!~"4(01|04|22)"}[5m])
    )
  )) > 14.4 * 0.001
)
and
(
  (1 - (
    sum by (namespace, service) (
      rate(kubecommerce_http_requests_total{route!~"/health.*|/metrics",status!~"5..",status!~"4(01|04|22)"}[1h])
    )
    / sum by (namespace, service) (
      rate(kubecommerce_http_requests_total{route!~"/health.*|/metrics",status!~"4(01|04|22)"}[1h])
    )
  )) > 14.4 * 0.001
)
```

```promql
# Slow burn (ticket): error ratio > 6 * 0.001 on BOTH 30m and 6h
(
  (1 - (
    sum by (namespace, service) (
      rate(kubecommerce_http_requests_total{route!~"/health.*|/metrics",status!~"5..",status!~"4(01|04|22)"}[30m])
    )
    / sum by (namespace, service) (
      rate(kubecommerce_http_requests_total{route!~"/health.*|/metrics",status!~"4(01|04|22)"}[30m])
    )
  )) > 6 * 0.001
)
and
(
  (1 - (
    sum by (namespace, service) (
      rate(kubecommerce_http_requests_total{route!~"/health.*|/metrics",status!~"5..",status!~"4(01|04|22)"}[6h])
    )
    / sum by (namespace, service) (
      rate(kubecommerce_http_requests_total{route!~"/health.*|/metrics",status!~"4(01|04|22)"}[6h])
    )
  )) > 6 * 0.001
)
```

The rule `for` windows are `2m` (fast) and `15m` (slow): the short/long PromQL windows
already provide the multi-window confirmation, and `for` adds a final debounce so a
single scrape cannot page. An idle service emits no `kubecommerce_http_requests_total`
series, so the ratio is absent (or `0/0 = NaN`, which is never `> threshold`) and the
rule cannot fire on no traffic.

A slower, budget-wide `3d / 6h at 1x` (10% in 3 days) ticket rule is a reasonable
extension once the platform has weeks of measured traffic; it is intentionally not
enabled yet because the demo environment does not retain that much history.

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
