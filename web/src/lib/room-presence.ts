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
