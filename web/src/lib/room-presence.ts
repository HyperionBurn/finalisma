/**
 * "0" is a confident answer: it means the service told us the room is
 * empty. Before the first room_info/room_poll response actually lands, the
 * true count simply is not known yet, and rendering 0 in that gap claims
 * knowledge the page does not have — for a product whose whole pitch is
 * that agents are really in a room together, briefly asserting the room is
 * empty is the worst possible first impression (MPAI-153). A dash says "I
 * do not know yet"; these format the same value everywhere a genuinely
 * known zero is still a real zero, not a permanent dash — `known` flips
 * once and stays true.
 */
export function knownCountDisplay(known: boolean, count: number): string {
  return known ? String(count) : '—';
}

export function knownHeadDisplay(known: boolean, head: number): string {
  return known ? `#${String(head).padStart(3, '0')}` : '#—';
}

/**
 * The visual forms above lean on convention a screen reader cannot hear:
 * the em-dash reads as "hyphen" or nothing, and "#000" as "pound zero
 * zero zero". So the AX tree got no meaningful loading state and no spoken
 * value — the same not-known-vs-known-and-empty ambiguity MPAI-153 removed
 * for sighted users, still live for everyone else (MPAI-159). These carry
 * words instead: the unknown state says it is not known yet, the known
 * state speaks the plain value (a real zero included). Pair each with its
 * *Display sibling marked aria-hidden so exactly one reaches the AX tree.
 */
export function knownCountLabel(known: boolean, count: number): string {
  return known ? String(count) : 'not known yet';
}

export function knownHeadLabel(known: boolean, head: number): string {
  return known ? `position ${head}` : 'not known yet';
}

export interface RoomCountersKnown {
  /** room_info resolved: the roster / "who is here" count is real. */
  members: boolean;
  /**
   * The initial room_poll resolved AND its events were applied: the event
   * count and the `head` position derived from it are real.
   */
  log: boolean;
}

/**
 * The member roster comes from room_info; the event log (and `head`,
 * derived as the last event's seq) comes from a separate room_poll. They
 * resolve independently, and loadAll awaits a third call — roomLink, owner
 * only — between applying room_info and applying the poll. So a single
 * "loaded" flag set after room_info commits a render where members is real
 * but events is still the initial empty array: a confident "0" / "position
 * 0" for a log that has not loaded (MPAI-163 — the MPAI-153 bug surviving
 * in a second field). Each counter must be governed by ITS OWN source
 * resolving, never by the other's.
 */
export function roomCountersKnown(infoLoaded: boolean, logLoaded: boolean): RoomCountersKnown {
  return { members: infoLoaded, log: logLoaded };
}
