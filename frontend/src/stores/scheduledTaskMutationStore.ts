import { createSingletonStore } from "../components/chat/ChatMessage/items/createSingletonStore";

/**
 * Lightweight counter store that increments whenever a scheduled task is
 * created / updated / deleted.  Sidebar subscribes to this to know when
 * to re-fetch its abbreviated task list.
 */
const store = createSingletonStore(0);

/** Increment the mutation counter – call after any successful CUD operation. */
export function notifyScheduledTaskMutation(): void {
  store.set(store.get() + 1);
}

/** Read the current mutation counter (for use as useSyncExternalStore snapshot). */
export function getScheduledTaskMutationVersion(): number {
  return store.get();
}

/** Subscribe to mutation counter changes. */
export function subscribeScheduledTaskMutation(
  listener: () => void,
): () => void {
  return store.subscribe(listener);
}
