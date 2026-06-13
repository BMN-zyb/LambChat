import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";

/**
 * Source-string tests for the useWebPush hook.
 * Validates key behavioral patterns without mounting React.
 */

const source = readFileSync(
  join(process.cwd(), "src/hooks/useWebPush.ts"),
  "utf-8",
);

test("hook checks for serviceWorker and PushManager availability", () => {
  assert.match(source, /"serviceWorker"\s+in\s+navigator/);
  assert.match(source, /"PushManager"\s+in\s+window/);
});

test("hook fetches VAPID public key via pushApi", () => {
  assert.match(source, /pushApi\.getVapidPublicKey/);
});

test("hook sets status to unavailable when push not supported", () => {
  // Both unavailable paths should exist
  const unavailableMatches = source.match(/"unavailable"/g);
  assert.ok(unavailableMatches && unavailableMatches.length >= 2);
});

test("subscribe calls pushManager.subscribe with userVisibleOnly and applicationServerKey", () => {
  assert.match(source, /userVisibleOnly:\s*true/);
  assert.match(source, /applicationServerKey:\s*urlBase64ToUint8Array/);
});

test("subscribe requests Notification permission before subscribing", () => {
  assert.match(source, /Notification\.requestPermission/);
  assert.match(source, /permission\s*!==\s*"granted"/);
});

test("subscribe sends subscription to backend via pushApi.subscribe", () => {
  assert.match(source, /pushApi\.subscribe/);
});

test("unsubscribe calls pushManager.unsubscribe and pushApi.unsubscribe", () => {
  assert.match(source, /existing\.unsubscribe\(\)/);
  assert.match(source, /pushApi\.unsubscribe/);
});

test("urlBase64ToUint8Array handles base64url encoding", () => {
  assert.match(source, /urlBase64ToUint8Array/);
  // Should replace URL-safe characters
  assert.match(source, /replace\(/);
  assert.match(source, /-/);
  assert.match(source, /_/);
  // Should handle padding
  assert.match(source, /"="/);
});

test("hook exports PushStatus type with all expected states", () => {
  assert.match(source, /PushStatus/);
  assert.match(source, /"idle"/);
  assert.match(source, /"loading"/);
  assert.match(source, /"subscribed"/);
  assert.match(source, /"unavailable"/);
  assert.match(source, /"error"/);
});

test("hook checks existing subscription via pushManager.getSubscription", () => {
  assert.match(source, /pushManager\.getSubscription/);
  assert.match(source, /"subscribed"/);
});

test("subscribe returns false when push is unavailable", () => {
  assert.match(source, /status\s*===\s*"unavailable".*return/);
});
