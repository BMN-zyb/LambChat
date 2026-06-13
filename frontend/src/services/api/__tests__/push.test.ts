import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";

/**
 * Source-string tests for the push API service.
 * Validates endpoint URLs, HTTP methods, and payload shapes without network calls.
 */

const source = readFileSync(
  join(process.cwd(), "src/services/api/push.ts"),
  "utf-8",
);

test("pushApi.getVapidPublicKey calls the correct endpoint with skipAuth", () => {
  assert.match(source, /api\/push\/vapid-public-key/);
  assert.match(source, /skipAuth:\s*true/);
});

test("pushApi.subscribe sends POST to /api/push/subscribe", () => {
  assert.match(source, /method:\s*"POST"/);
  assert.match(source, /api\/push\/subscribe/);
  // Verify it sends endpoint, keys, and user_agent
  assert.match(source, /endpoint/);
  assert.match(source, /user_agent/);
});

test("pushApi.unsubscribe sends POST to /api/push/unsubscribe", () => {
  // The unsubscribe function should contain the endpoint
  assert.match(source, /api\/push\/unsubscribe/);
  assert.match(source, /method:\s*"POST"/);
});

test("pushApi.deleteAllSubscriptions sends DELETE to /api/push/subscriptions", () => {
  assert.match(source, /api\/push\/subscriptions/);
  assert.match(source, /method:\s*"DELETE"/);
});

test("exports PushSubscriptionJSON interface with required fields", () => {
  assert.match(source, /PushSubscriptionJSON/);
  assert.match(source, /endpoint:\s*string/);
  assert.match(source, /keys:.*p256dh.*auth/s);
  assert.match(source, /expirationTime/);
});

test("exports PushSubscriptionResponse interface", () => {
  assert.match(source, /PushSubscriptionResponse/);
  assert.match(source, /user_agent:\s*string/);
  assert.match(source, /last_used_at/);
});
