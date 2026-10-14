#!/usr/bin/env python3
"""Attended Cityline helper. Real booking selectors MUST be verified by the user.

No automatic reload, CAPTCHA solving, queue bypass, API calls, or payment.
Run: python cityline_helper.py --demo
     python cityline_helper.py --config config.json
"""
from __future__ import annotations

import argparse
import copy
import json
import queue
import re
import signal
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from playwright.sync_api import (
    BrowserContext, Error as PlaywrightError, Locator, Page,
    TimeoutError as PlaywrightTimeout, sync_playwright,
)

BASE = Path(__file__).resolve().parent
DEFAULTS: dict[str, Any] = {
    "event_url": "https://shows.cityline.com/en/2026/361intoeternity.html",
    "browser_channel": "",
    "profile_directory": ".cityline_profile",
    "retry_interval_seconds": 5,
    "result_timeout_seconds": 25,
    "max_attempts": 20,
    "selectors": {
        "entry": "",
        "ticket_area": "",
        "attempt": "",
        "unavailable_result": "",
        "dismiss_unavailable": "",
        "checkout_ready": "",
        "busy": "",
    },
}
# Match visible messages, not hidden HTML templates or merely the page URL.
QUEUE_RE = re.compile(
    r"You are in the queue|System will try access again for you automatically|"
    r"\u6392\u968a\u4e2d|\u6392\u961f\u4e2d", re.I,
)
BLOCK_RE = re.compile(
    r"too many requests|access denied|session (?:has )?expired|"
    r"this page will not auto.retry|"
    r"\u81ea\u52d5\u767b\u5165\u88ab\u7d42\u6b62|"
    r"\u4f1a\u8bdd\u5df2\u8fc7\u671f|\u6703\u8a71\u5df2\u904e\u671f", re.I,
)
HUMAN_RE = re.compile(
    r"verify (?:you are|that you are) human|human verification|"
    r"\u771f\u4eba\u9a8c\u8bc1|\u771f\u4eba\u9a57\u8b49", re.I,
)
PAYMENT_RE = re.compile(
    r"\bpay(?:ment)?\b|checkout|confirm (?:order|purchase)|place order|"
    r"\u4ed8\u6b3e|\u652f\u4ed8|\u78ba\u8a8d\u8a02\u55ae|"
    r"\u786e\u8ba4\u8ba2\u5355|\u4e0b\u5355|\u4e0b\u55ae", re.I,
)
ENTRY_RE = re.compile(r"^\s*Go\s+to\s+Purchase\s*$", re.I)
PROCEED_RE = re.compile(r"^\s*Proceed\s*$", re.I)


def log(message: str) -> None:
    # Do not log URLs, cookies, order contents, or payment details.
    print(time.strftime("[%H:%M:%S]"), message, flush=True)


def alarm() -> None:
    try:
        import winsound
        winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
    except (ImportError, RuntimeError):
        print("\a", end="", flush=True)


def load_config(path: Path) -> dict[str, Any]:
    cfg = copy.deepcopy(DEFAULTS)
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("Config must be a JSON object.")
    for key, value in data.items():
        if key not in cfg:
            raise ValueError(f"Unknown config key: {key}")
        if key == "selectors":
            if not isinstance(value, dict):
                raise ValueError("selectors must be an object.")
            for name, spec in value.items():
                if name not in cfg["selectors"]:
                    raise ValueError(f"Unknown selector: {name}")
                if not isinstance(spec, (str, dict)):
                    raise ValueError(f"Invalid selector specification: {name}")
            cfg[key].update(value)
        else:
            cfg[key] = value
    # These are local safeguards, NOT a website-approved request frequency.
    interval = float(cfg["retry_interval_seconds"])
    timeout = float(cfg["result_timeout_seconds"])
    count = cfg["max_attempts"]
    if not (5 <= interval <= 3600):
        raise ValueError("retry_interval_seconds must be between 5 and 3600.")
    if not (5 <= timeout <= 300):
        raise ValueError("result_timeout_seconds must be between 5 and 300.")
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 100:
        raise ValueError("max_attempts must be an integer between 1 and 100.")
    parts = urlsplit(cfg["event_url"])
    if parts.scheme != "https" or not cityline_host(parts.hostname or ""):
        raise ValueError("Real mode requires an official Cityline HTTPS event URL.")
    if cfg["browser_channel"] not in ("", "chrome", "msedge"):
        raise ValueError('browser_channel must be "", "chrome", or "msedge".')
    if not isinstance(cfg["profile_directory"], str) or not cfg["profile_directory"].strip():
        raise ValueError("profile_directory must be a dedicated, nonempty directory.")
    return cfg


def cityline_host(host: str) -> bool:
    return host == "cityline.com" or host.endswith(".cityline.com")


def page_key(url: str) -> tuple[str, str, str]:
    parts = urlsplit(url)
    return parts.scheme, parts.netloc, parts.path


def locator(page: Page, spec: str | dict[str, Any]) -> Locator:
    """Accept CSS/Playwright selector strings, or explicit role/text objects.

    Example: {"role": "button", "name": "Try tickets", "exact": true,
              "within": "#ticket-form", "frame": "iframe#booking"}
    No eval() or arbitrary Python code is accepted from config.
    """
    if isinstance(spec, str):
        if not spec.strip():
            raise ValueError("Missing selector; inspect the real page first.")
        return page.locator(spec)
    if not isinstance(spec, dict):
        raise ValueError("Selector must be a string or object.")
    allowed = {"css", "role", "name", "text", "exact", "within", "frame"}
    if set(spec) - allowed:
        raise ValueError("Unknown selector object fields.")
    root: Any = page
    frames = spec.get("frame", [])
    if isinstance(frames, str):
        frames = [frames] if frames else []
    for frame in frames:
        root = root.frame_locator(frame)
    if spec.get("within"):
        root = root.locator(spec["within"])
    kinds = sum(key in spec for key in ("css", "role", "text"))
    if kinds != 1:
        raise ValueError("Specify exactly one of css, role, or text.")
    if "css" in spec:
        return root.locator(spec["css"])
    if "role" in spec:
        if "name" not in spec:
            raise ValueError("Role locators require an exact accessible name.")
        return root.get_by_role(spec["role"], name=spec["name"], exact=spec.get("exact", True))
    return root.get_by_text(spec["text"], exact=spec.get("exact", True))


def visible_matches(loc: Locator) -> list[Locator]:
    count = loc.count()
    if count > 50:
        raise ValueError("Selector is too broad (>50 matches); use a narrower one.")
    return [loc.nth(i) for i in range(count) if loc.nth(i).is_visible()]


def one(loc: Locator, label: str) -> Locator:
    matches = visible_matches(loc)
    if len(matches) != 1:
        raise ValueError(f"{label}: expected one visible match, found {len(matches)}.")
    return matches[0]


class Helper:
    def __init__(self, context: BrowserContext, page: Page, cfg: dict[str, Any],
                 config_path: Path | None = None, *, demo_mode: bool = False) -> None:
        self.context, self.page, self.cfg = context, page, cfg
        self.demo_mode = demo_mode
        self.config_path = config_path
        self.entry_armed = self.retry_armed = self.pending = False
        self.entry_used = self.finished = False
        self.attempts = 0
        self.next_attempt_at = 0.0
        self.result_deadline = 0.0
        self.follow_popup_until = 0.0
        self.blocked_http: int | None = None
        self.last_notice = ""
        self.commands: queue.Queue[str] = queue.Queue()
        self.known_pages: set[Page] = set()
        for tab in context.pages:
            self.attach(tab)
        context.on("page", self.new_page)

    def attach(self, page: Page) -> None:
        if page in self.known_pages:
            return
        self.known_pages.add(page)
        page.on("response", self.on_response)
        # Leave native JS dialogs for the human; never accept them automatically.
        page.on("dialog", lambda _dialog: self.stop("Native browser dialog: handle it manually."))

    def new_page(self, page: Page) -> None:
        self.attach(page)
        if page.opener() != self.page:
            return
        if self.retry_armed or self.pending:
            self.stop("A new tab opened during selection. Check it manually; no more retries.")
            self.page = page
        elif time.monotonic() <= self.follow_popup_until:
            self.page = page
            self.follow_popup_until = 0
            self.entry_armed = False
            log("Following the tab opened by the purchase entry; not opening another session.")

    def on_response(self, response: Any) -> None:
        try:
            request = response.request
            if (request.frame.page == self.page
                    and cityline_host(urlsplit(response.url).hostname or "")
                    and response.status in (403, 429)
                    and (request.is_navigation_request()
                         or request.resource_type in ("xhr", "fetch"))):
                self.blocked_http = response.status
                self.stop(f"HTTP {response.status}: automatic clicks disabled for this run.")
        except PlaywrightError:
            pass

    def notice(self, message: str, sound: bool = False) -> None:
        if message != self.last_notice:
            self.last_notice = message
            log(message)
            if sound:
                alarm()

    def stop(self, reason: str) -> None:
        self.entry_armed = self.retry_armed = self.pending = False
        self.follow_popup_until = 0
        self.notice("PAUSED: " + reason, sound=True)

    def configured(self, name: str) -> bool:
        return bool(self.cfg["selectors"].get(name))

    def get(self, name: str) -> Locator:
        return locator(self.page, self.cfg["selectors"][name])

    def shown(self, name: str) -> bool:
        return self.configured(name) and bool(visible_matches(self.get(name)))

    def visible_message(self, pattern: re.Pattern[str]) -> bool:
        for frame in self.page.frames:
            if visible_matches(frame.get_by_text(pattern)):
                return True
        return False

    def state(self) -> str:
        if self.page.is_closed():
            return "CLOSED"
        if self.shown("checkout_ready"):
            return "CHECKOUT"
        if self.blocked_http or self.visible_message(BLOCK_RE):
            return "BLOCKED"
        if self.visible_message(QUEUE_RE):
            return "QUEUE"
        if self.visible_message(HUMAN_RE):
            return "HUMAN"
        # An unknown or newly returned CAPTCHA/proceed screen is never clicked.
        if self.shown("ticket_area"):
            return "TICKETS"
        if visible_matches(self.page.get_by_role("button", name=PROCEED_RE)):
            return "HUMAN"
        if page_key(self.page.url) == page_key(self.cfg["event_url"]):
            return "EVENT"
        return "MANUAL"

    def reload_config(self) -> None:
        if self.config_path:
            new_cfg = load_config(self.config_path)
            if page_key(new_cfg["event_url"]) != page_key(self.cfg["event_url"]):
                raise ValueError("Do not change event_url in a live browser session.")
            self.cfg = new_cfg

    def validate_target(self) -> Locator:
        host = urlsplit(self.page.url).hostname or ""
        local_demo = self.demo_mode and page_key(self.page.url) == page_key("about:blank")
        if not cityline_host(host) and not local_demo:
            raise ValueError("Only the configured local demo or an official Cityline page may be automated.")
        area = one(self.get("ticket_area"), "ticket_area")
        target = one(self.get("attempt"), "attempt")
        if area.evaluate("el => /^(BODY|HTML)$/.test(el.tagName)"):
            raise ValueError("ticket_area must be the ticket form, not body/html.")
        handle = area.element_handle()
        if handle is None:
            raise ValueError("Ticket form disappeared.")
        try:
            if not target.evaluate("(el, area) => area.contains(el)", handle):
                raise ValueError("attempt must be inside ticket_area, in the same frame.")
        finally:
            handle.dispose()
        name = target.evaluate(
            "el => [el.innerText, el.getAttribute('aria-label'), el.value].filter(Boolean).join(' ')"
        )
        if PAYMENT_RE.search(name):
            raise ValueError("Refusing a payment/order-confirmation action as attempt.")
        return target

    def arm_entry(self) -> None:
        if self.finished or self.blocked_http:
            raise ValueError("Automatic actions are disabled for this run.")
        if self.entry_used or self.state() != "EVENT":
            raise ValueError("go is only for the original event page, before entry.")
        self.entry_armed = True
        self.retry_armed = False
        self.notice("ENTRY ARMED: waiting for one enabled Go to Purchase; no reloads.")

    def arm_retries(self) -> None:
        if self.finished or self.blocked_http:
            raise ValueError("Automatic actions are disabled for this run.")
        if self.pending or self.retry_armed:
            raise ValueError("Already active; use stop before changing configuration.")
        self.reload_config()
        for name in ("ticket_area", "attempt", "unavailable_result", "checkout_ready"):
            if not self.configured(name):
                raise ValueError(f"Configure {name} from the REAL booking page first.")
        # Validate selector syntax even when a result/order marker is not present yet.
        for name in self.cfg["selectors"]:
            if self.configured(name):
                self.get(name).count()
        if self.state() != "TICKETS":
            raise ValueError("Not on a verified ticket selection page; remain manual.")
        self.validate_target()
        if self.attempts >= self.cfg["max_attempts"]:
            raise ValueError("Attempt cap reached; do not start another automatic loop.")
        self.retry_armed = True
        self.entry_armed = False
        self.notice("RETRIES ARMED: keep hands off selection controls; stop before manual actions.")

    def tick(self) -> None:
        if self.finished:
            return
        state = self.state()
        if state == "CHECKOUT":
            self.finished = True  # irreversible for this program run
            self.stop("ORDER/PAYMENT marker found. Inspect details and pay manually; not a purchase confirmation.")
            return
        if state in ("CLOSED", "BLOCKED", "HUMAN", "QUEUE"):
            had_actions = self.entry_armed or self.retry_armed or self.pending
            self.entry_armed = self.retry_armed = self.pending = False
            if state in ("HUMAN", "QUEUE"):
                self.entry_used = True
            self.notice({
                "CLOSED": "Page closed. Use tabs / use N; no new session is created.",
                "BLOCKED": "BLOCKED/EXPIRED: handle manually. No retry or refresh.",
                "HUMAN": "MANUAL: finish real-person verification and click Proceed yourself.",
                "QUEUE": "QUEUE: observe only. The site handles its own queue traffic.",
            }[state], sound=state == "HUMAN" or (had_actions and state != "QUEUE"))
            return
        now = time.monotonic()
        if self.pending:
            # The unavailable marker was absent immediately BEFORE this attempt.
            # A visible marker here is therefore a new outcome, not old static text.
            if self.shown("unavailable_result") and not self.shown("busy"):
                self.pending = False
                self.next_attempt_at = now + float(self.cfg["retry_interval_seconds"])
                self.notice("Fresh unavailable result. Waiting before another permitted attempt.")
            elif now >= self.result_deadline:
                self.stop("Result uncertain/timed out. Check the page/order manually; do NOT blindly retry.")
            return
        if self.retry_armed:
            if state != "TICKETS":
                self.stop("Left the verified ticket form. Check the next screen manually.")
                return
            if self.attempts >= self.cfg["max_attempts"]:
                self.stop("Maximum attempts reached. Continue manually if appropriate.")
                return
            if now >= self.next_attempt_at:
                self.try_once()
            return
        if self.entry_armed and state == "EVENT":
            entry = self.get("entry") if self.configured("entry") else (
                self.page.get_by_role("link", name=ENTRY_RE).or_(
                    self.page.get_by_role("button", name=ENTRY_RE))
            )
            matches = visible_matches(entry)
            if len(matches) > 1:
                self.stop("Multiple purchase entries matched. Narrow selectors.entry; no guessed click.")
            elif len(matches) == 1 and matches[0].is_enabled():
                self.entry_armed = False
                self.entry_used = True  # set BEFORE click; timeouts never repeat it
                self.follow_popup_until = now + 30
                try:
                    matches[0].click(timeout=4000)
                    log("Purchase entry clicked ONCE. Waiting; no reloads.")
                except PlaywrightError:
                    self.stop("Entry click outcome uncertain. Inspect the browser manually.")
        elif not self.entry_armed:
            self.notice("MONITOR ONLY: log in/verify/select manually. Use arm only after configuring selectors.")

    def try_once(self) -> None:
        if self.state() != "TICKETS" or self.shown("busy"):
            return
        target = self.validate_target()
        if not target.is_enabled():
            self.notice("Target disabled: waiting for the page to enable it; never force-clicking.")
            return
        # A static 'temporarily unavailable' badge on the ticket itself is NOT a response.
        if self.shown("unavailable_result"):
            if not self.configured("dismiss_unavailable"):
                self.stop("Old unavailable result remains. Dismiss it manually, then arm again.")
                return
            dismiss = one(self.get("dismiss_unavailable"), "dismiss_unavailable")
            label = dismiss.evaluate("el => [el.innerText, el.getAttribute('aria-label'), el.value].join(' ')")
            if PAYMENT_RE.search(label):
                raise ValueError("Refusing a payment/confirmation action as dismiss_unavailable.")
            # An explicit error-close action only, never a general 'confirm' dialog.
            dismiss.click(timeout=2000)
            self.get("unavailable_result").wait_for(state="hidden", timeout=2500)
            if self.state() != "TICKETS":
                self.stop("Page changed while dismissing an error. Check manually.")
                return
            target = self.validate_target()
        # Strict absence/visibility checks immediately before the single click.
        if self.shown("checkout_ready") or self.shown("busy") or self.shown("unavailable_result"):
            return
        self.attempts += 1
        self.pending = True  # set BEFORE click; a timeout may follow a successful dispatch
        self.result_deadline = time.monotonic() + float(self.cfg["result_timeout_seconds"])
        log(f"Attempt {self.attempts}/{self.cfg['max_attempts']}: one click, then wait for a fresh result.")
        try:
            target.click(timeout=3000)
        except PlaywrightError:
            self.stop("Click outcome uncertain. Automatic retries stopped; inspect any existing order.")

    def check(self) -> None:
        if self.retry_armed or self.entry_armed or self.pending:
            self.stop("Checking configuration; automatic clicks paused.")
        self.reload_config()
        for name in ("ticket_area", "attempt", "unavailable_result", "dismiss_unavailable", "checkout_ready", "busy"):
            if self.configured(name):
                loc = self.get(name)
                log(f"{name}: total={loc.count()}, visible={len(visible_matches(loc))}")
            else:
                log(f"{name}: NOT CONFIGURED")
        log("Current state: " + self.state())

    def handle(self, command: str) -> bool:
        """Return True only for explicit quit. All Playwright calls stay on this thread."""
        command = command.strip()
        if command == "go":
            self.arm_entry()
        elif command == "arm":
            self.arm_retries()
        elif command == "stop":
            self.stop("Stopped by user; browser remains open.")
        elif command == "check":
            self.check()
        elif command == "inspect":
            self.stop("Inspector open. Pick locators without triggering real booking actions; then Resume.")
            self.page.pause()
            self.context.set_default_timeout(2000)
        elif command == "tabs":
            for i, page in enumerate(self.context.pages):
                # Host only: no private URL tokens or page contents in logs.
                log(f"{i}: {urlsplit(page.url).hostname or '(blank)'}" + (" [tracked]" if page == self.page else ""))
        elif command.startswith("use "):
            index = int(command.split()[1])
            if not 0 <= index < len(self.context.pages):
                raise ValueError("Invalid tab index. Run tabs.")
            self.stop("Changing tracked tab; retries must be armed manually again.")
            self.page = self.context.pages[index]
            self.page.bring_to_front()
        elif command == "quit":
            return True
        elif command in ("help", "?"):
            log("go | arm | stop | check | inspect | tabs | use N | quit")
            log("quit closes this browser. Do NOT quit while queueing or before completing payment.")
        elif command:
            raise ValueError("Unknown command. Use help.")
        return False


def demo_config(url: str) -> dict[str, Any]:
    cfg = copy.deepcopy(DEFAULTS)
    cfg["event_url"] = url
    cfg["profile_directory"] = ".demo_profile"
    cfg["selectors"].update({
        "ticket_area": "#ticket-form", "attempt": "#try-ticket",
        "unavailable_result": "#unavailable", "dismiss_unavailable": "#dismiss",
        "checkout_ready": "#order-summary", "busy": "#busy",
    })
    return cfg


def read_commands(commands: queue.Queue[str]) -> None:
    # The input thread must NEVER call Playwright (the sync API is not thread-safe).
    while True:
        try:
            commands.put(input().strip())
        except EOFError:
            return


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=BASE / "config.json")
    parser.add_argument("--demo", action="store_true", help="Local simulation only; no Cityline requests.")
    args = parser.parse_args()
    try:
        if args.demo:
            cfg, config_path = demo_config("about:blank"), None
            log("LOCAL DEMO ONLY. Ordinary checkbox, not a real CAPTCHA or real ticket purchase.")
        else:
            cfg, config_path = load_config(args.config.resolve()), args.config.resolve()
        with sync_playwright() as playwright:
            options: dict[str, Any] = {
                "headless": False, "no_viewport": True,
                "handle_sigint": False, "handle_sigterm": False,
            }
            if cfg["browser_channel"]:
                options["channel"] = cfg["browser_channel"]
            profile = Path(cfg["profile_directory"])
            if not profile.is_absolute():
                profile = BASE / profile
            context = playwright.chromium.launch_persistent_context(str(profile), **options)
            context.set_default_timeout(2000)
            context.set_default_navigation_timeout(30000)
            page = context.pages[0] if context.pages else context.new_page()
            helper = Helper(context, page, cfg, config_path, demo_mode=args.demo)
            # First navigation only. There is intentionally no reload() anywhere.
            try:
                if args.demo:
                    page.set_content((BASE / "demo" / "index.html").read_text(encoding="utf-8"))
                else:
                    page.goto(cfg["event_url"], wait_until="domcontentloaded", timeout=30000)
            except PlaywrightTimeout:
                log("Initial navigation timed out. Browser kept open; check it manually.")
            except PlaywrightError:
                log("Initial navigation failed. Browser kept open; check it manually.")
            # Ctrl+C pauses actions instead of closing the paid/queued browser session.
            old_handler = signal.signal(signal.SIGINT, lambda *_: helper.commands.put("stop"))
            threading.Thread(target=read_commands, args=(helper.commands,), daemon=True).start()
            helper.handle("help")
            log("Default = observe only. Log in manually. Type go only when you intend to enter the sale.")
            try:
                quitting = False
                while context.pages and not quitting:
                    try:
                        # Handle commands on the same thread as all browser operations.
                        if not helper.commands.empty():
                            quitting = helper.handle(helper.commands.get_nowait())
                        if not quitting:
                            helper.tick()
                    except (ValueError, PlaywrightError, OSError) as exc:
                        # Do not dump Playwright call logs (which may contain private text).
                        reason = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
                        helper.stop(reason + " Manual check required.")
                    except Exception as exc:
                        helper.stop(f"Unexpected {type(exc).__name__}; browser kept open for manual control.")
                    if not quitting and context.pages:
                        try:
                            # DOM polling, not a website reload or a ticket API request.
                            context.pages[0].wait_for_timeout(300)
                        except PlaywrightError:
                            if context.pages:
                                helper.stop("A tab was closed or disconnected. Use tabs / use N if needed.")
                            else:
                                break
            finally:
                signal.signal(signal.SIGINT, old_handler)
                context.close()
    except (ValueError, OSError, PlaywrightError) as exc:
        log(f"Startup/connection error ({type(exc).__name__}). Check config and browser installation.")
        if isinstance(exc, (ValueError, OSError)):
            log(str(exc))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
