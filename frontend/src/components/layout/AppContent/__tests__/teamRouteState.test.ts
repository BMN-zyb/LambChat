import { readFileSync } from "node:fs";
import { getTeamRouteRequest } from "../teamRouteState";

const chatAppContentSource = readFileSync(
  new URL("../ChatAppContent.tsx", import.meta.url),
  "utf8",
);

test("reads team use requests from chat query params", () => {
  expect(
    getTeamRouteRequest(new URLSearchParams("agent=team&team=team-123"), null),
  ).toEqual({
    agentId: "team",
    teamId: "team-123",
  });
});

test("reads team use requests from route state", () => {
  expect(
    getTeamRouteRequest(new URLSearchParams(), {
      agentId: "team",
      teamId: "team-456",
    }),
  ).toEqual({
    agentId: "team",
    teamId: "team-456",
  });
});

test("ignores incomplete team use requests", () => {
  expect(getTeamRouteRequest(new URLSearchParams("agent=team"), null)).toBe(
    null,
  );
  expect(getTeamRouteRequest(new URLSearchParams("team=team-123"), null)).toBe(
    null,
  );
});

test("chat app applies team route requests to agent and team selection", () => {
  expect(chatAppContentSource).toMatch(
    /getTeamRouteRequest\(searchParams,\s*location\.state\)/,
  );
  expect(chatAppContentSource).toMatch(/switchAgent\(teamRequest\.agentId\)/);
  expect(chatAppContentSource).toMatch(/selectTeam\(teamRequest\.teamId\)/);
});

test("chat app switches team mode back to a persona-compatible agent when using a persona", () => {
  expect(chatAppContentSource).toMatch(/resolvePersonaAgentId/);
  expect(chatAppContentSource).toMatch(
    /const switchToPersonaAgentMode = useCallback/,
  );
  expect(chatAppContentSource).toMatch(
    /if \(currentAgent !== "team"\) return;/,
  );
  expect(chatAppContentSource).toMatch(/selectTeam\(null\)/);
  expect(chatAppContentSource).toMatch(
    /switchToPersonaAgentMode\(\);[\s\S]*setPersonaPreset\(preset\.id, snapshot\)/,
  );
});
