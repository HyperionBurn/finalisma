/**
 * Mount a real React element into a jsdom container under `node --test`.
 *
 * Lives under web/ (not tests/) so its bare `react` / `react-dom/client`
 * imports resolve against web/node_modules the same way a component's own
 * imports do — a test file under tests/ has no node_modules ancestor of
 * its own, so importing React directly from there would fail resolution
 * even with jsdom installed. Import this helper by relative path instead
 * of importing React directly from a tests/*.js file.
 */
import { act } from 'react';
import * as React from 'react';
import { createRoot } from 'react-dom/client';

export { act, React };

export async function mount(element, { settleMs = 30 } = {}) {
  const container = document.createElement('div');
  document.body.appendChild(container);
  const root = createRoot(container);

  const settle = () => new Promise((resolve) => setTimeout(resolve, settleMs));

  await act(async () => {
    root.render(element);
    await settle();
  });

  return {
    container,
    async rerender(nextElement) {
      await act(async () => {
        root.render(nextElement);
        await settle();
      });
    },
    unmount() {
      act(() => root.unmount());
      container.remove();
    },
  };
}
