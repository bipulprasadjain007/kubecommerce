// =============================================================================
// KubeCommerce - k6 order-flow load test.
//
// Run via:  make load-test
//   or:     k6 run -e BASE_URL=... -e VUS=20 -e DURATION=2m tests/load/order-flow.js
//
// Environment:
//   BASE_URL            gateway base URL        (default http://localhost:8080)
//   CATALOG_URL         catalog direct URL      (default http://localhost:8002)
//   INTERNAL_API_TOKEN  internal mutation token (default local dev placeholder)
//   VUS                 peak virtual users      (default 20)
//   DURATION            sustained stage length  (default 2m)
//
// Thresholds follow the guide: failed request rate < 5%, p95 < 750 ms.
// No secrets are hardcoded; the internal token is a local placeholder only.
// =============================================================================
import http from 'k6/http';
import { check, sleep } from 'k6';
import { Counter } from 'k6/metrics';

const BASE_URL = (__ENV.BASE_URL || 'http://localhost:8080').replace(/\/+$/, '');
const CATALOG_URL = (__ENV.CATALOG_URL || 'http://localhost:8002').replace(/\/+$/, '');
const INTERNAL_API_TOKEN = __ENV.INTERNAL_API_TOKEN || 'dev-internal-token-change-me';
const VUS = Number(__ENV.VUS || 20);
const DURATION = __ENV.DURATION || '2m';

const orderCreated = new Counter('order_created_total');

export const options = {
  scenarios: {
    order_flow: {
      executor: 'ramping-vus',
      startVUs: 0,
      gracefulRampDown: '10s',
      stages: [
        { duration: '30s', target: VUS },
        { duration: DURATION, target: VUS },
        { duration: '30s', target: 0 },
      ],
    },
  },
  thresholds: {
    http_req_failed: ['rate<0.05'],
    http_req_duration: ['p(95)<750'],
  },
  tags: { testid: 'order-flow' },
};

function jsonHeaders(extra) {
  return Object.assign({ 'Content-Type': 'application/json' }, extra || {});
}

// setup() runs once: register a unique user, log in, and create a product.
export function setup() {
  const uniq = `${Date.now()}-${Math.floor(Math.random() * 100000)}`;
  const email = `load-${uniq}@example.com`;
  const password = 'L0ad-Test-Password!42';

  const reg = http.post(
    `${BASE_URL}/api/auth/users`,
    JSON.stringify({ email, password }),
    { headers: jsonHeaders(), tags: { endpoint: 'register' } },
  );
  check(reg, { 'register 2xx': (r) => r.status >= 200 && r.status < 300 });

  const login = http.post(
    `${BASE_URL}/api/auth/login`,
    JSON.stringify({ email, password }),
    { headers: jsonHeaders(), tags: { endpoint: 'login' } },
  );
  check(login, { 'login 2xx': (r) => r.status >= 200 && r.status < 300 });
  const token = login.json('access_token');

  const product = http.post(
    `${CATALOG_URL}/products`,
    JSON.stringify({
      sku: `LOAD-${uniq}`,
      name: `Load Product ${uniq}`,
      description: 'created by k6 order-flow.js',
      price_cents: 1999,
      stock: 100000,
    }),
    {
      headers: jsonHeaders({ 'X-Internal-Token': INTERNAL_API_TOKEN }),
      tags: { endpoint: 'create-product' },
    },
  );
  check(product, { 'create product 2xx': (r) => r.status >= 200 && r.status < 300 });
  const productId = product.json('id');

  if (!token || !productId) {
    throw new Error('setup failed: could not obtain an access token or product id');
  }
  return { token, productId, email };
}

// default() runs every iteration: browse the catalog, then place an order.
export default function (data) {
  const authHeaders = { Authorization: `Bearer ${data.token}` };

  const list = http.get(`${BASE_URL}/api/catalog/products`, {
    headers: authHeaders,
    tags: { endpoint: 'list-products' },
  });
  check(list, { 'list products 2xx': (r) => r.status >= 200 && r.status < 300 });

  const payload = JSON.stringify({
    items: [{ product_id: data.productId, quantity: 1 }],
  });
  const order = http.post(`${BASE_URL}/api/orders`, payload, {
    headers: jsonHeaders(authHeaders),
    tags: { endpoint: 'create-order' },
  });
  check(order, { 'create order 2xx': (r) => r.status >= 200 && r.status < 300 });
  if (order.status >= 200 && order.status < 300) {
    orderCreated.add(1);
  }

  sleep(1);
}

// Write the full summary to artifacts/ (created by scripts/load-test.sh) and a
// concise summary to stdout.
export function handleSummary(data) {
  const outPath = 'artifacts/k6-order-flow-summary.json';
  const concise = {
    metrics: {
      http_req_failed: data.metrics.http_req_failed,
      http_req_duration: data.metrics.http_req_duration,
      http_reqs: data.metrics.http_reqs,
      order_created_total: data.metrics.order_created_total,
    },
  };
  return {
    stdout: JSON.stringify(concise, null, 2) + '\n',
    [outPath]: JSON.stringify(data, null, 2),
  };
}
