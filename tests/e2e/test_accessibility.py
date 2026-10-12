"""Structural accessibility checks over every portal page (UX-04).

Not a substitute for a screen-reader audit; it guarantees the structure such tools rely on:
page language, one h1, a label for every visible control, unique ids, alt text, named
buttons, no positive tabindex, scoped table headers.
"""

from __future__ import annotations

from html.parser import HTMLParser
from typing import Any

from cosmic.core.config import Settings
from cosmic.db.engine import Database
from cosmic.web.app import create_app
from cosmic.worker.jobs import build_registry
from tests.e2e.test_trading_portal import PROPOSAL, _post, _setup
from tests.helpers import client_for, full_login, make_user
from tests.worker_helpers import run_queued

CONTROLS = {"input", "select", "textarea"}
UNLABELLED_INPUT_TYPES = {"hidden", "submit", "button", "reset", "image"}


class Audit(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.problems: list[str] = []
        self.lang = False
        self.h1 = 0
        self.ids: dict[str, int] = {}
        self.label_for: set[str] = set()
        self.controls: list[tuple[str, dict[str, str], bool]] = []
        self.label_depth = 0
        self.button_text: list[str] | None = None
        self.button_attrs: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs_list: list[tuple[str, str | None]]) -> None:
        attrs = {k: v or "" for k, v in attrs_list}
        if tag == "html" and attrs.get("lang"):
            self.lang = True
        if tag == "h1":
            self.h1 += 1
        if "id" in attrs:
            self.ids[attrs["id"]] = self.ids.get(attrs["id"], 0) + 1
        if tag == "label":
            self.label_depth += 1
            if attrs.get("for"):
                self.label_for.add(attrs["for"])
        if tag in CONTROLS and attrs.get("type", "text") not in UNLABELLED_INPUT_TYPES:
            self.controls.append((tag, attrs, self.label_depth > 0))
        if tag == "img" and "alt" not in attrs:
            self.problems.append(f"img without alt: {attrs.get('src')}")
        if attrs.get("tabindex", "0").lstrip("-").isdigit() and int(attrs.get("tabindex", "0")) > 0:
            self.problems.append(f"positive tabindex on <{tag}>")
        if tag == "th" and "scope" not in attrs:
            self.problems.append("table header without scope")
        if tag == "button":
            self.button_text, self.button_attrs = [], attrs

    def handle_endtag(self, tag: str) -> None:
        if tag == "label":
            self.label_depth = max(0, self.label_depth - 1)
        if tag == "button" and self.button_text is not None:
            if not "".join(self.button_text).strip() and not self.button_attrs.get("aria-label"):
                self.problems.append("button without an accessible name")
            self.button_text = None

    def handle_data(self, data: str) -> None:
        if self.button_text is not None:
            self.button_text.append(data)

    def report(self) -> list[str]:
        out = list(self.problems)
        if not self.lang:
            out.append("<html> has no lang")
        if self.h1 != 1:
            out.append(f"{self.h1} h1 elements (expected 1)")
        out += [f"duplicate id {i}" for i, n in self.ids.items() if n > 1]
        for tag, attrs, wrapped in self.controls:
            named = wrapped or attrs.get("aria-label") or attrs.get("aria-labelledby")
            if not named and attrs.get("id") not in self.label_for:
                out.append(f"unlabelled <{tag} name={attrs.get('name')!r}>")
        return out


def audit(html: str) -> list[str]:
    a = Audit()
    a.feed(html)
    return a.report()


async def test_every_page_is_structurally_accessible(settings: Settings, db: Database) -> None:
    clock, ctx = await _setup(settings, db)
    app = create_app(settings, db=db, clock=clock)
    ctx.services["trading"] = app.state.trading
    owner = await make_user(db, clock)
    problems: dict[str, Any] = {}
    async with client_for(app) as anon:
        for path in ("/login",):
            problems[path] = audit((await anon.get(path)).text)
    async with client_for(app) as c:
        await full_login(c, owner, clock)
        await _post(c, "/ops/mode", {"mode": "PAPER_TRADING", "reason": "accessibility"}, "/ops")
        await _post(c, "/ops/paper-account", {"alias": "paper-main", "capital": "10000"}, "/ops")
        await run_queued(ctx, build_registry(ctx))
        r = await _post(c, "/proposals", PROPOSAL, "/proposals/new")
        intent = r.headers["location"]  # type: ignore[attr-defined]
        await _post(c, "/ops/emergency-stop", {"reason": "accessibility alerts"}, "/ops")
        pages = [
            "/",
            "/alerts",
            "/approvals",
            intent,
            "/proposals/new",
            "/portfolio",
            "/orders",
            "/research",
            "/strategies",
            "/market",
            "/market/SPOT:BTC/USD@COINBASE",
            "/sources",
            "/ai",
            "/ops",
            "/golive",
            "/settings/risk",
            "/audit",
            "/settings/telegram",
            "/search?q=trend",
        ]
        for path in pages:
            resp = await c.get(path)
            assert resp.status_code == 200, (path, resp.status_code)
            problems[path] = audit(resp.text)
    failing = {p: v for p, v in problems.items() if v}
    assert not failing, failing


def test_the_checker_catches_each_problem() -> None:
    bad = (
        '<html><body><h1>a</h1><h1>b</h1><input name="q"><select name="s"></select>'
        '<img src="x.png"><button></button><div id="d"></div><span id="d"></span>'
        '<a tabindex="3">x</a><table><tr><th>h</th></tr></table></body></html>'
    )
    found = " | ".join(audit(bad))
    for expected in (
        "no lang",
        "2 h1",
        "unlabelled <input",
        "unlabelled <select",
        "img without alt",
        "button without",
        "duplicate id d",
        "positive tabindex",
        "without scope",
    ):
        assert expected in found, expected
    good = (
        '<html lang="en"><h1>t</h1><label for="q">Q</label><input id="q" name="q">'
        '<label>S <select name="s"></select></label><input type="hidden" name="h">'
        "<button>Go</button></html>"
    )
    assert audit(good) == []
