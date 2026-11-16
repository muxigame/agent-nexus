// Invite codes in the browser (design §11.5): what the join page shows before it sends anything.
//   decodeInvite("anx1.…" | "agentnexus://enroll?invite=anx1.…") → {hub, id, secret} | null
// The server validates again (agent_nexus/invites.py decode_invite); this only previews the gateway.

export function inviteFromText(text) {
  let value = String(text || "").replace(/\s+/g, "");
  if (/^agentnexus:\/\//i.test(value)) {
    const query = value.split("?")[1] || "";
    value = new URLSearchParams(query).get("invite") || "";
  }
  return value;
}

export function decodeInvite(text) {
  const code = inviteFromText(text);
  if (!code.startsWith("anx1.")) return null;
  try {
    const body = code.slice(5).replace(/-/g, "+").replace(/_/g, "/");
    const padded = body + "=".repeat((4 - (body.length % 4)) % 4);
    const binary = atob(padded);
    const bytes = Uint8Array.from(binary, (ch) => ch.charCodeAt(0));
    const data = JSON.parse(new TextDecoder().decode(bytes));
    if (!data || typeof data.hub !== "string" || typeof data.id !== "string" || typeof data.secret !== "string") return null;
    if (!/^https?:\/\/[^/?#\s]+/.test(data.hub)) return null;
    return { hub: data.hub.replace(/\/+$/, ""), id: data.id, secret: data.secret };
  } catch {
    return null;
  }
}

/** The host part of a hub URL, for "将加入 hub.example.com". */
export function hubHost(hub) {
  try { return new URL(hub).host; } catch { return hub || ""; }
}
