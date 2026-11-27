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

/** The installer to feature for this browser: an Android phone gets the APK, a Mac the disk image
 * (Apple silicon unless only Intel is published; browsers do not tell the chip), else Windows. */
export function featured(desktop, userAgent = (typeof navigator !== "undefined" && navigator.userAgent) || "") {
  const pick = (platform, arch) => desktop.find((item) => item.platform === platform && (!arch || item.arch === arch));
  if (/Android/i.test(userAgent)) return pick("android") || pick("windows") || desktop[0];
  if (/Macintosh|Mac OS X/i.test(userAgent) && !/iPhone|iPad/i.test(userAgent)) return pick("macos", "arm64") || pick("macos") || pick("windows") || desktop[0];
  return pick("windows") || desktop[0];
}
