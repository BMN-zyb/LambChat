/**
 * When no virtual keyboard is open, return null so the CSS fallback `100dvh`
 * is used — this ensures full-screen coverage on iOS PWA standalone mode where
 * `visualViewport.height` can be smaller than the actual screen.
 *
 * When the keyboard IS open (visual viewport significantly smaller than window),
 * return the measured height so the app shrinks to stay above the keyboard.
 */
const KEYBOARD_THRESHOLD_PX = 100;

export function isKeyboardViewport({
  visualViewportHeight,
  windowInnerHeight,
}: {
  visualViewportHeight?: number | null;
  windowInnerHeight?: number | null;
}): boolean {
  const vvHeight = visualViewportHeight ?? null;
  const wHeight = windowInnerHeight ?? null;

  return !!vvHeight && !!wHeight && wHeight - vvHeight > KEYBOARD_THRESHOLD_PX;
}

export function getAppViewportHeightCssValue({
  visualViewportHeight,
  windowInnerHeight,
}: {
  visualViewportHeight?: number | null;
  windowInnerHeight?: number | null;
}): string | null {
  const vvHeight = visualViewportHeight ?? null;
  const wHeight = windowInnerHeight ?? null;

  if (!vvHeight || !wHeight || vvHeight <= 0 || wHeight <= 0) {
    return null;
  }

  // Keyboard likely open — use measured visual viewport height
  if (
    isKeyboardViewport({
      visualViewportHeight: vvHeight,
      windowInnerHeight: wHeight,
    })
  ) {
    return `${Math.round(vvHeight)}px`;
  }

  // No keyboard — let CSS 100dvh handle full-screen height
  return null;
}

function parsePixelValue(value: string | null | undefined): number | null {
  if (!value || !value.endsWith("px")) {
    return null;
  }

  const parsed = Number.parseFloat(value);
  return Number.isFinite(parsed) ? parsed : null;
}

export function shouldUpdateAppViewportHeight(
  previousValue: string | null | undefined,
  nextValue: string | null,
): boolean {
  if (previousValue === nextValue) return false;
  if (previousValue == null || nextValue == null) return true;

  const previousPx = parsePixelValue(previousValue);
  const nextPx = parsePixelValue(nextValue);

  if (previousPx === null || nextPx === null) {
    return previousValue !== nextValue;
  }

  return Math.abs(nextPx - previousPx) > 2;
}
