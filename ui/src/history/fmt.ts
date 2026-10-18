const pad = (n: number): string => String(n).padStart(2, "0");

/** Epoch seconds -> local "YYYY-MM-DD HH:MM". */
export function formatDateTime(epochSeconds: number): string {
  const d = new Date(epochSeconds * 1000);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** 118000 -> "118,000" (exact, for titles). */
export function formatExact(n: number): string {
  return Number.isFinite(n) ? Math.round(n).toLocaleString("en-US") : "0";
}

/** "alice@box", or whichever half is known. */
export function machineLabel(user: string | null | undefined, host: string | null | undefined, id: string): string {
  const u = (user ?? "").trim();
  const h = (host ?? "").trim();
  if (u && h) return `${u}@${h}`;
  return u || h || id;
}
