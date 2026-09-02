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
  const SECRET_WORDS = new RegExp(
    'pass(word|wd)?|pwd|otp|one-?time|2fa|mfa|totp|cvv|cvc|secret|token|credit-?card|cc-?num',
    'i'
  );
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

  // The compose body, if one is open. Its CONTENTS are content, never controls.
  //
  // Gmail wraps every line you type in its own <div>, so a five-line email becomes five
  // more elements in the list the moment the agent writes it. Observed live: the agent
  // typed the body successfully, saw "[76] I hope your day..., [83] Wishing you..., [89]
  // Regards,, [93] Sam" appear where one body field had been, concluded the field had
  // "split into multiple textboxes", and cleared and retyped it. Twice. Each successful
  // write looked exactly like a failure, so it kept undoing its own work.
  //
  // Nothing inside the body is separately actionable — you type into the body, never into
  // line three — and `MailContext.body_index` already promises the body is ONE element.
  // Dropping the descendants keeps that promise and stops the write from looking undone.
  const bodyRoot =
    document.querySelector('[role="dialog"] [g_editable="true"]') ||
    document.querySelector(
      '[role="dialog"] [contenteditable="true"]:not([role="combobox"])'
    );

  for (const el of all) {
    if (out.length >= maxNodes) break;
    if (SKIP.has(el.tagName)) continue;
    if (bodyRoot && el !== bodyRoot && bodyRoot.contains(el)) continue;

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
  // The dialog's own box, not just whether it exists. When something is open, the fields
  // INSIDE it are the only ones the agent can act on — and they must not lose a budget
  // contest to two hundred inbox rows behind them.
  const dialogEl = composeEl ? composeEl.closest('[role="dialog"], dialog') || composeEl : null;
  const composeBox = dialogEl ? dialogEl.getBoundingClientRect() : null;
  const composeOpen = !!composeBox && composeBox.width > 0 && composeBox.height > 0;

  // WHETHER each field has content, never what it says. A committed recipient becomes a
  // chip — a separate node — so the input itself reads empty and the agent types the address
  // again on top of the first. Reporting "filled" is the difference between an agent that
  // knows what is left to do and one that guesses.
  //
  // **Every one of these checks is SCOPED, and that scoping is the whole correctness.** An
  // unscoped selector run against the compose dialog finds the wrong element with total
  // confidence, and the agent then behaves perfectly on a false premise — which is far
  // harder to debug than an agent that misbehaves. Two real instances, both fixed here:
  //
  //   * `toFilled` searched the whole dialog for anything chip-shaped, and matched the FROM
  //     row — the signed-in user's OWN address, marked up identically to a recipient. Every
  //     fresh compose reported its recipient as already entered, so the agent skipped it and
  //     proposed sending mail addressed to nobody.
  //   * `bodyFilled` used a bare `textarea` selector. Gmail's recipient field IS a textarea
  //     in some versions, so typing a recipient made the BODY report filled — and the agent
  //     would then skip writing the body and send an empty email.
  //
  // Both are the same mistake: asking "is there anything like this in the dialog?" when the
  // question is "is there anything in THIS field?".
  function textOf(el) {
    if (!el) return '';
    return ((el.value !== undefined ? el.value : el.innerText) || '').trim();
  }
  function filledWithin(root, selectors, excluded) {
    if (!root) return false;
    for (const selector of selectors) {
      for (const el of root.querySelectorAll(selector)) {
        // Never let one field's content answer for another's.
        if (excluded && excluded !== el && excluded.contains(el)) continue;
        if (textOf(el)) return true;
      }
    }
    return false;
  }

  // Widened for the Gmail that actually ships. These were written against a simplified
  // model — `input[aria-label*="To"]` requires an <input> tag, and Gmail's To field is a
  // `div[role="combobox"]`. The selector missed, `toInput` fell through to a nearby LABEL,
  // and the observation reported the To field at the label's index. Same miss for the
  // subject, which simply came back "not found" and sent the agent scrolling for a field
  // that was on screen the whole time.
  //
  // Tag-agnostic and role-aware from here on: match what the element IS, not what tag a
  // 2015 mail client would have used for it.
  const TO_SELECTORS = [
    '[name="to"]',
    'textarea[name="to"]',
    '[role="combobox"][aria-label*="To" i]',
    '[aria-label*="To recipients" i]',
  ];

  // Tried IN ORDER, one selector at a time — never comma-joined.
  //
  // `querySelector('a, b')` returns whichever element comes first in the DOCUMENT, not
  // whichever selector was listed first. Gmail puts a "To - Select contacts" LINK above the
  // real recipient field, so a loose fallback in the list won every time and the carefully
  // ordered specific selectors never got a look in. `toInput` became that link,
  // `recipientArea()` walked up from it into a region containing the FROM row, the sender's
  // own address was found there, and a brand-new compose window reported its recipient as
  // already entered. The agent then correctly skipped the recipient and sent nothing to
  // nobody.
  //
  // Ordering only means something if each selector is tried on its own.
  function firstMatch(root, selectors) {
    if (!root) return null;
    for (const selector of selectors) {
      const found = root.querySelector(selector);
      if (found) return found;
    }
    return null;
  }
  const toInput = firstMatch(dialogEl, TO_SELECTORS);

  // The recipients ROW: the input plus the chips beside it, and nothing else.
  function recipientArea() {
    if (!toInput) return null;
    // Up to the row holding the input and its chips. Gmail nests these a few levels; four
    // reaches the recipients row without escaping into the header that holds From. The
    // `!== dialogEl` guard stops it swallowing the whole dialog when the input sits shallow.
    let node = toInput;
    for (let i = 0; i < 4 && node.parentElement && node.parentElement !== dialogEl; i++) {
      node = node.parentElement;
    }
    return node;
  }
  const toArea = recipientArea();

  // No To input found -> report EMPTY, never filled. The two mistakes are not equal:
  // "empty" when it is full makes the agent type a duplicate, which it can see in the next
  // observation and fix; "full" when it is empty makes it skip the recipient silently and
  // there is nothing downstream that can notice. When unsure, choose the recoverable error.
  const toFilled = composeOpen && !!toInput && (
    // The input's OWN value first: when the To field sits directly under the dialog,
    // `toArea` IS that input, and `querySelectorAll` never returns its own root.
    !!textOf(toInput) ||
    filledWithin(toArea, TO_SELECTORS) ||
    // The chip case: a committed recipient leaves the input empty and a removable pill
    // beside it — now looked for only WHERE a recipient can actually be.
    !!(toArea && toArea.querySelector(
      '[data-hovercard-id], [email], .afV, [role="option"][aria-selected="true"]'
    ))
  );
  const SUBJECT_SELECTORS = [
    '[name="subjectbox"]',
    '[name="subject"]',
    '[aria-label*="Subject" i]',
    '[placeholder*="Subject" i]',
  ];
  const subjectFilled = composeOpen && filledWithin(dialogEl, SUBJECT_SELECTORS, toArea);
  // Recipient fields excluded by selector AND by region. An address typed into To must
  // never make the body look written.
  const BODY_SELECTORS = [
    '[g_editable="true"]',
    '[aria-label*="Message Body" i]',
    '[role="textbox"][aria-label*="Body" i]',
    '[contenteditable="true"]:not([role="combobox"])',
    'textarea:not([name="to"]):not([name="cc"]):not([name="bcc"])'
  ];
  const bodyFilled = composeOpen && filledWithin(dialogEl, BODY_SELECTORS, toArea);

  // WHERE each field is, as the node id this walk already assigned. The funnel turns these
  // into the `[N]` the model sees, so the agent is told the number instead of hunting for
  // it in a list that renumbers every turn.
  //
  // `ids` only holds elements the walk kept, so a field pruned as uninteresting resolves to
  // null rather than to a number that indexes nothing — the agent is told "not on screen",
  // which is true and actionable, instead of being handed a lie.
  function nodeIdOf(selectors, scope) {
    const root = scope || dialogEl;
    if (!root) return null;
    for (const selector of selectors) {
      for (const el of root.querySelectorAll(selector)) {
        if (ids.has(el)) return ids.get(el);
      }
    }
    return null;
  }
  const toNode = composeOpen && toInput && ids.has(toInput) ? ids.get(toInput) : null;
  const subjectNode = composeOpen ? nodeIdOf(SUBJECT_SELECTORS) : null;
  const bodyNode = composeOpen ? nodeIdOf(BODY_SELECTORS) : null;

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
      toFilled,
      subjectFilled,
      bodyFilled,
      toNode,
      subjectNode,
      bodyNode,
      focusBox: composeOpen
        ? { x: composeBox.left, y: composeBox.top,
            width: composeBox.width, height: composeBox.height }
        : null,
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
    box = raw.get("focusBox")
    focus_box = None
    if isinstance(box, dict):
        try:
            focus_box = (
                float(box["x"]), float(box["y"]), float(box["width"]), float(box["height"])
            )
        except (KeyError, TypeError, ValueError):
            focus_box = None
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
        to_filled=bool(raw.get("toFilled")),
        subject_filled=bool(raw.get("subjectFilled")),
        body_filled=bool(raw.get("bodyFilled")),
        to_node=raw.get("toNode"),
        subject_node=raw.get("subjectNode"),
        body_node=raw.get("bodyNode"),
        focus_box=focus_box,
    )
