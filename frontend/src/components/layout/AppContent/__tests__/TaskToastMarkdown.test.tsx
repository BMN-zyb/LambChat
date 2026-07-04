/** @vitest-environment jsdom */
import { renderToStaticMarkup } from "react-dom/server";

import { TaskToastMarkdown } from "../TaskToastMarkdown.tsx";

test("renders inline markdown in task toast bodies", () => {
  const html = renderToStaticMarkup(
    <TaskToastMarkdown content="Finished **deploy** with `pnpm build` and [logs](https://example.com/logs)." />,
  );

  expect(html).toMatch(/<strong[^>]*>deploy<\/strong>/);
  expect(html).toMatch(/<code[^>]*>pnpm build<\/code>/);
  expect(html).toMatch(
    /<a[^>]*href="https:\/\/example.com\/logs"[^>]*>logs<\/a>/,
  );
});

test("keeps heavyweight markdown out of task toast bodies", () => {
  const html = renderToStaticMarkup(
    <TaskToastMarkdown
      content={
        '![graph](https://example.com/graph.png)\n\n```ts\nconsole.log("wide")\n```'
      }
    />,
  );

  expect(html).not.toMatch(/<img\b/);
  expect(html).not.toMatch(/<pre\b/);
});
