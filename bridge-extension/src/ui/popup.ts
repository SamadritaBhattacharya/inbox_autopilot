/**
 * The popup: pair this browser, and say plainly whether the bridge is up.
 *
 * Status is asked of the service worker rather than tracked here, because the popup is
 * destroyed every time it closes — any state it held would be a lie the moment it reopened.
 */
import { describeStatus, type BridgeStatus } from "./status";

const DEFAULT_URL = "ws://localhost:8000/ws/bridge";

const el = <T extends HTMLElement>(id: string): T => {
  const node = document.getElementById(id);
  if (!node) throw new Error(`popup is missing #${id}`);
  return node as T;
};

async function refresh(): Promise<void> {
  const dot = el<HTMLSpanElement>("dot");
  const label = el<HTMLSpanElement>("state");

  try {
    const reply = (await chrome.runtime.sendMessage({ type: "status" })) as
      | { status: BridgeStatus; running: boolean; account?: string; paired?: boolean }
      | undefined;
    if (!reply) throw new Error("no reply");
    dot.dataset.state = reply.status.state;
    label.textContent = describeStatus(reply.status, reply.running, reply.account ?? "");

    // Once paired, the code field is clutter that invites someone to paste a burnt code.
    // The button becomes the way OUT rather than the way in.
    const paired = Boolean(reply.paired);
    document.getElementById("pair-fields")?.toggleAttribute("hidden", paired);
    el<HTMLButtonElement>("save").hidden = paired;
    el<HTMLButtonElement>("unpair").hidden = !paired;
  } catch {
    // The worker is asleep and has not woken yet. Saying "starting" beats an error the user
    // can do nothing about and which resolves itself in a second.
    dot.dataset.state = "connecting";
    label.textContent = "starting…";
  }
}

async function load(): Promise<void> {
  const stored = await chrome.storage.local.get({
    pairingCode: "",
    backendUrl: DEFAULT_URL,
  });
  el<HTMLInputElement>("code").value = String(stored.pairingCode ?? "");
  el<HTMLInputElement>("url").value = String(stored.backendUrl || DEFAULT_URL);
}

async function save(): Promise<void> {
  const button = el<HTMLButtonElement>("save");
  button.disabled = true;
  button.textContent = "Connecting…";

  await chrome.storage.local.set({
    pairingCode: el<HTMLInputElement>("code").value.trim(),
    backendUrl: el<HTMLInputElement>("url").value.trim() || DEFAULT_URL,
  });
  await chrome.runtime.sendMessage({ type: "reconnect" });

  button.disabled = false;
  button.textContent = "Save and connect";
  await refresh();
}

async function unpair(): Promise<void> {
  await chrome.runtime.sendMessage({ type: "unpair" });
  el<HTMLInputElement>("code").value = "";
  await refresh();
}

el<HTMLButtonElement>("save").addEventListener("click", () => void save());
el<HTMLButtonElement>("unpair").addEventListener("click", () => void unpair());
void load();
void refresh();
// The socket may come up a moment after the popup opens; one poll is cheaper than plumbing
// a push channel to a window that usually lives for three seconds.
setInterval(() => void refresh(), 1500);
