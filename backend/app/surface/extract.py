"""Stage 1 — pull candidate elements out of a live page.

**Why an injected DOM walk rather than a raw CDP snapshot.** `DOMSnapshot.captureSnapshot`
returns the page as parallel arrays indexed into a shared string table, which then has to be
re-stitched into a tree, cross-referenced with a separate layout tree, and joined to the
accessibility tree by backend node id. It is precise and it is a great deal of fragile
plumbing. Everything the funnel actually needs — box, computed style, role, accessible
name, tree position — is available directly in page script, in one round trip, in a form
that reads like what it is.

The trade worth stating: this code runs *in* the page, so a hostile page could in principle
lie to it. That changes nothing about the safety model. The page was already the untrusted
input; a lying page can only alter what the agent *perceives*, and every irreversible
consequence of a perception is gated behind a human decision downstream.

**Hit-testing beats geometry.** `elementFromPoint` asks the browser the exact question that
matters — "if a user clicked here, what would receive it?" — where geometric overlap only
approximates it. The result feeds `RawElement.paint_order`, so the occlusion stage inherits
a real answer instead of a heuristic.
"""
from __future__ import annotations

from typing import Any

from app.observation.raw import PageMeta, RawElement

#: Hard ceiling on nodes walked. A pathological page must not hang a turn; the funnel is
#: designed to survive an incomplete list (that is what `droppedCount` is for), so stopping
#: early is safe, whereas walking 200k nodes is not.
MAX_NODES = 4000

EXTRACT_JS = """
(maxNodes) => {
  const SKIP = new Set(['SCRIPT','STYLE','META','LINK','HEAD','NOSCRIPT','TITLE','BR']);
  const INTERACTIVE_TAGS = new Set(['A','BUTTON','INPUT','SELECT','TEXTAREA','SUMMARY','OPTION']);
  const INTERACTIVE_ROLES = new Set([
    'button','link','textbox','checkbox','radio','menuitem','tab','option','switch','combobox'
  ]);

  const vw = window.innerWidth, vh = window.innerHeight;

  function roleOf(el) {
    const explicit = el.getAttribute('role');
    if (explicit) return explicit;
    // Gmail marks people with <span email="..." name="...">. A structured name is the ONLY
    // thing that teaches the tokenizer a person, so surfacing it here is what makes name
    // tokenization work at all downstream.
    if (el.hasAttribute('email')) return 'sender';
    const tag = el.tagName;
    if (tag === 'A') return 'link';
    if (tag === 'BUTTON') return 'button';
    if (tag === 'INPUT') {
      return (el.type === 'checkbox' || el.type === 'radio') ? el.type : 'textbox';
    }
    if (tag === 'TEXTAREA') return 'textbox';
    if (tag === 'SELECT') return 'combobox';
    if (tag === 'TR') return 'listitem';
    if (tag === 'LI') return 'listitem';
    if (/^H[1-6]$/.test(tag)) return 'heading';
    if (el.isContentEditable) return 'textbox';
    return 'generic';
  }

  function nameOf(el, interactiveHint) {
    const aria = el.getAttribute('aria-label');
    if (aria) return aria.trim();
    // A person's display name, not the address: the address is matched by pattern anyway,
    // and the NAME is what needs registering before prose elsewhere mentions it.
    if (el.hasAttribute('email')) {
      return (el.getAttribute('name') || el.textContent || '').trim();
    }
    if (el.tagName === 'IMG') return (el.getAttribute('alt') || '').trim();
    const title = el.getAttribute('title');
    if (title) return title.trim();
    const placeholder = el.getAttribute('placeholder');
    if (placeholder) return placeholder.trim();
    // Own text first. Inheriting descendant text makes every ancestor look identical to its
    // subtree, which is the noise wrapper-collapse exists to remove.
    let own = '';
    for (const node of el.childNodes) {
      if (node.nodeType === 3) own += node.textContent;
    }
    own = own.replace(/\\s+/g, ' ').trim();
    if (own) return own;
    if (el.children.length === 0) return (el.textContent || '').replace(/\\s+/g, ' ').trim();

    // An INTERACTIVE element with no own text is the case that matters: a clickable mail row
    // whose sender and subject live in child spans. Listing it as `[4] generic: ""` gives the
    // model a number it cannot reason about — it can see the row exists and not what it is.
    // Falling back to subtree text only here keeps the ancestor noise away while making the
    // one element you actually click nameable.
    if (interactiveHint) {
      return (el.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 200);
    }
    return '';
  }

  // A secret must never leave the page, and this is the earliest point at which that can
  // be guaranteed. Everything downstream — the funnel, the vault, the wire, the model, the
  // trajectory — only ever sees what this function returns, so redacting HERE is the one
  // place the guarantee is total. The PII vault cannot help: it tokenizes so values can be
  // resolved again later, which is the opposite of what a password needs.
  const SECRET_TYPES = new Set(['password']);
  const SECRET_WORDS = /pass(word|wd)?|pwd|otp|one-?time|2fa|mfa|totp|cvv|cvc|secret|token|credit-?card|cc-?num/i;
  const SECRET_AUTOCOMPLETE = /password|one-time-code|cc-number|cc-csc/i;

  function isSecret(el) {
    const type = (el.getAttribute && el.getAttribute('type') || '').toLowerCase();
    if (SECRET_TYPES.has(type)) return true;
    const autocomplete = el.getAttribute && el.getAttribute('autocomplete') || '';
    if (SECRET_AUTOCOMPLETE.test(autocomplete)) return true;
    // Sites that style their own masked field rather than using type=password still name it
    // something honest, so the attribute soup is worth checking.
    const hints = [el.name, el.id, el.getAttribute && el.getAttribute('aria-label')]
      .filter(Boolean).join(' ');
    return SECRET_WORDS.test(hints);
  }

  function valueOf(el) {
    if (!('value' in el) || typeof el.value !== 'string' || !el.value) return null;
    // Report that it is FILLED, never what with. The agent needs to know whether a field
    // still needs attention; it never needs the characters.
    if (isSecret(el)) return '•'.repeat(8);
    return el.value;
  }

  function isInteractive(el, role) {
    if (INTERACTIVE_TAGS.has(el.tagName)) return true;
    if (INTERACTIVE_ROLES.has(role)) return true;
    if (el.isContentEditable) return true;
    const tabindex = el.getAttribute('tabindex');
    if (tabindex !== null && tabindex !== '-1') return true;
    return typeof el.onclick === 'function';
  }

  const all = document.querySelectorAll('*');
  const out = [];
  const ids = new Map();
  let nextId = 1;

  for (const el of all) {
    if (out.length >= maxNodes) break;
    if (SKIP.has(el.tagName)) continue;

    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    const role = roleOf(el);
    const interactive = isInteractive(el, role);
    const name = nameOf(el, interactive);
    const value = valueOf(el);

    // Nothing to click and nothing to read: not a candidate, and carrying it would only
    // spend budget the funnel needs for elements that matter.
    if (!interactive && !name && !value) continue;

    if (!ids.has(el)) ids.set(el, nextId++);
    const id = ids.get(el);
    const parent = el.parentElement;
    const parentId = parent && ids.has(parent) ? ids.get(parent) : null;

    const displayed =
      style.display !== 'none' &&
      style.visibility !== 'hidden' &&
      parseFloat(style.opacity || '1') > 0.01;

    // Ask the browser what a click would actually hit. This is the authoritative answer to
    // "is this reachable?" — geometric overlap only ever approximates it, and gets a
    // full-viewport overlay wrong in exactly the case that matters (an open dialog).
    // null when the centre is off-screen and the question cannot be asked.
    let receivesPointer = null;
    const cx = rect.left + rect.width / 2;
    const cy = rect.top + rect.height / 2;
    if (displayed && cx >= 0 && cy >= 0 && cx < vw && cy < vh) {
      const hit = document.elementFromPoint(cx, cy);
      receivesPointer = !!(hit && (hit === el || el.contains(hit) || hit.contains(el)));
    }

    out.push({
      nodeId: id,
      role,
      name: name.slice(0, 600),
      value: value ? String(value).slice(0, 600) : null,
      x: rect.left, y: rect.top, width: rect.width, height: rect.height,
      interactive,
      displayed,
      receivesPointer,
      paintOrder: out.length,  // document order: the geometric fallback's tie-breaker
      parentId,
      depth: 0,
    });
  }

  // Gmail's compose panel is a dialog; whether it is OPEN is the difference between
  // "reading the inbox" and "writing an email".
  //
  // `querySelector` alone is wrong here, and wrong in the direction that matters: the
  // compose markup sits in the DOM permanently, hidden by an ancestor, so a presence check
  // reports "compose is open" on a plain inbox forever. A hidden element has a zero-size
  // box — including when the ancestor is what hides it — so measuring is the reliable test.
  const composeEl = document.querySelector(
    '[role="dialog"] [name="subjectbox"], [role="dialog"] [g_editable="true"], dialog[open]'
  );
  const composeBox = composeEl ? composeEl.getBoundingClientRect() : null;
  const composeOpen = !!composeBox && composeBox.width > 0 && composeBox.height > 0;

  return {
    elements: out,
    meta: {
      contextRef: location.href,
      title: document.title,
      viewportWidth: vw,
      viewportHeight: vh,
      scrollX: Math.round(window.scrollX),
      scrollY: Math.round(window.scrollY),
      composeOpen,
    },
  };
}
"""


def parse_elements(raw: list[dict[str, Any]]) -> list[RawElement]:
    return [
        RawElement(
            node_id=int(item["nodeId"]),
            role=str(item.get("role") or "generic"),
            name=str(item.get("name") or ""),
            value=item.get("value"),
            x=float(item.get("x") or 0.0),
            y=float(item.get("y") or 0.0),
            width=float(item.get("width") or 0.0),
            height=float(item.get("height") or 0.0),
            interactive=bool(item.get("interactive")),
            displayed=bool(item.get("displayed", True)),
            paint_order=int(item.get("paintOrder") or 0),
            receives_pointer=item.get("receivesPointer"),
            parent_id=item.get("parentId"),
            depth=int(item.get("depth") or 0),
        )
        for item in raw
    ]


def detect_view(url: str, compose_open: bool) -> str:
    """Which mailbox view is on screen.

    Derived from the URL, which stays **executor-side**: it is an identifier, which is why
    the observation carries an opaque `context_id` instead. The derived view is a small,
    non-identifying fact the agent genuinely needs — "am I in a thread or a list?" — and it
    is safe to send precisely because it is a category, not a reference.
    """
    if compose_open:
        return "compose"
    lowered = url.lower()

    # Signed out, or bounced to Google's login. This has to come first and be explicit:
    # every branch below eventually falls through to "inbox", so a sign-in wall was being
    # reported as a mailbox. The agent then dutifully "summarized the inbox" from a page
    # that said "Couldn't sign you in" - it burned six steps and produced a confident,
    # entirely fictional answer. A view the agent cannot act in must say so by name.
    if "accounts.google.com" in lowered or "/signin" in lowered or "servicelogin" in lowered:
        return "signed_out"
    for fragment, view in (
        ("#sent", "sent"),
        ("#drafts", "drafts"),
        ("#search", "search"),
        ("/calendar", "calendar"),
    ):
        if fragment in lowered:
            return view
    # A thread URL carries a message id after the label: #inbox/<hex>.
    #
    # The fragment check is required, not incidental. Without it any path-shaped URL looks
    # like a thread — a plain `file:///…/inbox.html` was classified as "thread" because the
    # last path segment happened to be long enough. Gmail puts the message id in the
    # fragment, so no fragment means no thread.
    if "#" not in lowered:
        return "inbox"
    fragment = lowered.rsplit("#", 1)[-1]
    if "/" in fragment and len(fragment.rsplit("/", 1)[-1]) >= 8:
        return "thread"
    return "inbox"


def parse_meta(raw: dict[str, Any], *, thread_ref: str | None = None) -> PageMeta:
    url = str(raw.get("contextRef") or "")
    compose_open = bool(raw.get("composeOpen"))
    return PageMeta(
        context_ref=url,
        title=str(raw.get("title") or ""),
        viewport_width=int(raw.get("viewportWidth") or 1280),
        viewport_height=int(raw.get("viewportHeight") or 800),
        scroll_x=int(raw.get("scrollX") or 0),
        scroll_y=int(raw.get("scrollY") or 0),
        view=detect_view(url, compose_open),  # type: ignore[arg-type]
        thread_ref=thread_ref,
        compose_open=compose_open,
    )
