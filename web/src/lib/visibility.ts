/**
 * A backgrounded room tab does not learn a room closed until it is looked
 * at again. RoomView's poll tick already fetches room_info on an interval,
 * but deliberately skips its own work while `document.hidden` (no point
 * polling a tab nobody is looking at) — so a guest who switched away while
 * the owner closed the room sees nothing until the browser's own
 * background-timer throttling happens to let the next tick run, which can
 * be well past the tick's nominal interval.
 *
 * `onTabVisible` re-invokes the SAME poll the instant the tab regains
 * visibility, instead of adding a second, parallel polling path.
 */
export interface VisibilityDocument {
  readonly hidden: boolean;
  addEventListener(type: 'visibilitychange', listener: () => void): void;
  removeEventListener(type: 'visibilitychange', listener: () => void): void;
}

export function onTabVisible(doc: VisibilityDocument, onVisible: () => void): () => void {
  const handler = () => {
    if (!doc.hidden) onVisible();
  };
  doc.addEventListener('visibilitychange', handler);
  return () => doc.removeEventListener('visibilitychange', handler);
}
