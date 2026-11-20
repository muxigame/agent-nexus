// Pure settings and helpers of the remote terminal page (importable by node tests).

// Close to Windows Terminal's default look: Cascadia, One Half Dark-ish colours.
export const TERMINAL_THEME = {
  background: "#0c0c0c", foreground: "#d4d4d4", cursor: "#d4d4d4", cursorAccent: "#0c0c0c",
  selectionBackground: "#264f78",
  black: "#0c0c0c", red: "#e06c75", green: "#98c379", yellow: "#e5c07b", blue: "#61afef", magenta: "#c678dd", cyan: "#56b6c2", white: "#dcdfe4",
  brightBlack: "#5a6374", brightRed: "#e06c75", brightGreen: "#98c379", brightYellow: "#e5c07b", brightBlue: "#61afef",
  brightMagenta: "#c678dd", brightCyan: "#56b6c2", brightWhite: "#ffffff",
};
export const TERMINAL_FONT = '"Cascadia Mono", "Cascadia Code", "JetBrains Mono", "Sarasa Mono SC", Consolas, Menlo, "Microsoft YaHei Mono", monospace';

/** ws(s)://host<base>/<id>/terminal/ws?shell=…&cols=…&rows=… — base is /api/devices (gateway) or /api/remote/devices (client). */
export function terminalSocketURL(deviceId, { shell = "", cols = 120, rows = 32 } = {}, loc = location, base = "/api/devices") {
  const scheme = loc.protocol === "https:" ? "wss" : "ws";
  const query = new URLSearchParams({ shell, cols: String(cols), rows: String(rows) });
  return `${scheme}://${loc.host}${base}/${encodeURIComponent(deviceId)}/terminal/ws?${query}`;
}

/** Where the terminal page talks to: the gateway console (owner) or the local client (this device's credential). */
export const GATEWAY_TERMINAL_API = {
  devices: "/api/devices",
  info: (id) => `/api/devices/${encodeURIComponent(id)}/terminal`,
  socketBase: "/api/devices",
  external: null,
  back: { label: "设备", href: "#/devices" },
};
export const CLIENT_TERMINAL_API = {
  devices: "/api/devices",
  info: (id) => `/api/remote/devices/${encodeURIComponent(id)}/terminal`,
  socketBase: "/api/remote/devices",
  external: (id) => `/api/remote/devices/${encodeURIComponent(id)}/terminal/external`,
  back: { label: "设备", href: "#/devices" },
};

/** Keys a phone keyboard lacks, as the bytes a terminal sends. */
export const EXTRA_KEYS = [
  ["Esc", "\x1b"], ["Tab", "\t"], ["Ctrl+C", "\x03"], ["Ctrl+D", "\x04"], ["Ctrl+L", "\x0c"],
  ["↑", "\x1b[A"], ["↓", "\x1b[B"], ["←", "\x1b[D"], ["→", "\x1b[C"],
];
