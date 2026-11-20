// Copy-paste install commands (downloads page, invite dialog). Pure: no DOM, no imports.

/** Shell commands that download the newest Ubuntu package from this gateway and install it. */
export function ubuntuInstallCommand({ server = false, origin = location.origin } = {}) {
  const recommends = server ? "--no-install-recommends " : "";
  return `cd /tmp && curl -fL -o agent-nexus.deb ${origin}/downloads/latest/amd64.deb && sudo apt install -y ${recommends}./agent-nexus.deb`;
}

/** Install, join with `code` and run as a systemd service: one paste on a server without a desktop. */
export function ubuntuServerJoinCommand(code, origin = location.origin) {
  return `${ubuntuInstallCommand({ server: true, origin })} && agent-nexus join ${code} && agent-nexus service enable`;
}
