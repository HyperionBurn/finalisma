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
