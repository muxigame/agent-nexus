// Operating-system marks for device avatars (os_family from the gateway, agent_nexus/osinfo.py).
// Simplified, recognisable shapes drawn for this console (not the vendors' artwork).
// Each mark is a 24x24 SVG body; colours are fixed so the marks read on light and dark surfaces.

const MARKS = {
  windows: {
    label: "Windows",
    body: '<path fill="#0078D4" d="M3 5.2 10.4 4.2v7.1H3zM11.4 4.05 21 2.7v8.6h-9.6zM3 12.3h7.4v7.1L3 18.4zM11.4 12.3H21V21l-9.6-1.35z"/>',
  },
  macos: {
    label: "macOS",
    body: '<path fill="currentColor" d="M16.4 12.6c0-2.2 1.8-3.3 1.9-3.4-1-1.5-2.6-1.7-3.2-1.7-1.4-.1-2.6.8-3.3.8-.7 0-1.7-.8-2.9-.8-1.5 0-2.9.9-3.6 2.2-1.6 2.7-.4 6.8 1.1 9 .8 1.1 1.6 2.3 2.8 2.2 1.1 0 1.5-.7 2.9-.7 1.3 0 1.7.7 2.9.7 1.2 0 2-1.1 2.7-2.2.9-1.2 1.2-2.4 1.2-2.5 0 0-2.4-.9-2.5-3.6zM14.2 6c.6-.7 1-1.8.9-2.8-.9 0-2 .6-2.6 1.3-.6.7-1.1 1.7-.9 2.7 1 .1 2-.5 2.6-1.2z"/>',
  },
  ios: {
    label: "iOS",
    body: '<rect x="6" y="1.8" width="12" height="20.4" rx="2.6" fill="none" stroke="currentColor" stroke-width="1.6"/>' +
      '<rect x="9.8" y="3.4" width="4.4" height="1.3" rx=".65" fill="currentColor"/>' +
      '<path fill="currentColor" d="M13.6 12.6c0-1 .8-1.5.9-1.6-.5-.7-1.2-.8-1.5-.8-.6 0-1.2.4-1.5.4-.3 0-.8-.4-1.4-.4-.7 0-1.3.4-1.7 1-.7 1.3-.2 3.1.5 4.1.4.5.8 1.1 1.3 1 .5 0 .7-.3 1.4-.3.6 0 .8.3 1.4 0.3 0.6 0 .9-.5 1.3-1 .4-.6.6-1.1.6-1.2 0 0-1.2-.4-1.2-1.5zM12.6 9.6c.3-.3.5-.8.4-1.3-.4 0-.9.3-1.2.6-.3.3-.5.8-.4 1.3.4 0 .9-.3 1.2-.6z"/>',
  },
  android: {
    label: "Android",
    body: '<g fill="#3DDC84"><path d="M3 18.5a9 9 0 0 1 18 0z"/><path d="M6.4 8.4 4.6 5.3a.6.6 0 1 1 1-.6l1.9 3.2zM17.6 8.4l1.8-3.1a.6.6 0 1 0-1-.6l-1.9 3.2z"/></g>' +
      '<circle cx="8.3" cy="14.3" r="1.15" fill="#fff"/><circle cx="15.7" cy="14.3" r="1.15" fill="#fff"/>',
  },
  ubuntu: {
    label: "Ubuntu",
    body: '<circle cx="12" cy="12" r="10.5" fill="#E95420"/>' +
      '<circle cx="12" cy="12" r="4.6" fill="none" stroke="#fff" stroke-width="1.8"/>' +
      '<g fill="#fff" stroke="#E95420" stroke-width="1.1"><circle cx="16.6" cy="12" r="2.1"/><circle cx="9.7" cy="16" r="2.1"/><circle cx="9.7" cy="8" r="2.1"/></g>',
  },
  debian: {
    label: "Debian",
    body: '<path fill="none" stroke="#D70A53" stroke-width="2.2" stroke-linecap="round" d="M13.2 20.6c-4.7 0-8.4-3.7-8.4-8.4S8.3 3.6 13 3.6c3.9 0 6.9 2.9 6.9 6.6 0 3.2-2.4 5.6-5.4 5.6-2.5 0-4.4-1.8-4.4-4.1 0-1.9 1.4-3.3 3.2-3.3 1.4 0 2.4 1 2.4 2.2"/>',
  },
  fedora: {
    label: "Fedora",
    body: '<circle cx="12" cy="12" r="10.5" fill="#51A2DA"/>' +
      '<path fill="none" stroke="#fff" stroke-width="2.3" stroke-linecap="round" d="M10.4 19V10.2a3 3 0 0 1 3-3h1.4M7.7 12.6h6.2"/>',
  },
  arch: {
    label: "Arch Linux",
    body: '<path fill="#1793D1" d="M12 1.8c-.9 2.3-1.5 3.8-2.5 6 .6.6 1.4 1.4 2.6 2.2-1.3-.5-2.2-1.1-2.9-1.7C7.8 11.2 5.8 15 2.2 21.5c2.6-1.5 4.7-2.5 6.6-2.8-.1-.4-.1-.8-.1-1.2v-.1c.1-2.1 1.2-3.8 2.5-3.7 1.3.1 2.3 2 2.2 4.2 0 .4-.1.8-.1 1.1 1.9.4 3.9 1.3 6.5 2.8-.5-1-1-1.8-1.4-2.6-.7-.6-1.5-1.3-3.1-2.1 1.1.3 1.9.6 2.5.9C15.9 12.3 15 9.2 12 1.8z"/>',
  },
  linux: {
    label: "Linux",
    body: '<rect x="2" y="3.5" width="20" height="17" rx="3" fill="#2B2F36"/>' +
      '<path fill="none" stroke="#F5C211" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="m6.5 9 3.2 3-3.2 3"/>' +
      '<path stroke="#fff" stroke-width="2" stroke-linecap="round" d="M12 15.5h5"/>',
  },
};

/** `{label, svg}` for an os_family, or null when there is no mark for it. */
export function osMark(family, { size = 18 } = {}) {
  const mark = MARKS[family];
  if (!mark) return null;
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="${size}" height="${size}" aria-hidden="true" focusable="false">${mark.body}</svg>`;
  return { label: mark.label, svg };
}

export const OS_FAMILIES = Object.keys(MARKS);
