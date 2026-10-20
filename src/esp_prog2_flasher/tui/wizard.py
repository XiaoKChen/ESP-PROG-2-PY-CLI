"""Step-by-step wizards: one field at a time, then an optional y/n confirm summary.

A wizard is TTSSH's "New connection" frame: completed fields muted above, the current field
behind an accent left bar, enter to accept, shift+tab to step back, esc to back out. A
field is either typed text or a filterable pick-list; a pick-list choice may open the file
browser. Which fields come next is recomputed from the answers so far, so steps can depend
on earlier choices. A wizard with a confirm ends on a summary answered with y or n/esc.

Owns:        the step shapes (TextStep, ChoiceStep, Confirm, WizardSpec), the answers
             map, the step order and back-tracking, and the wizard's key handling.
Not here:    which steps a flow has and what its answers mean (flows.py); the browser
             (browser.py); starting jobs (../app.py).
Invariants:  the wizard dismisses with the answers only after the last step (and, with a
             confirm, only on "y"); every other way out dismisses None. In a text field every
             printable key, "/" included, is typed literally.
Depends on:  textual, rich, browser.py, commands.py, screens.py, widgets.py.
See also:    flows.py, ../app.py, tests/test_app.py
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Final

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.widgets import Input, Static

from .browser import BrowserScreen, BrowseSpec
from .commands import COMMANDS_KEY, popup_commands
from .screens import ChromeScreen
from .widgets import ChromeState, DetailsView, Field, FilterList, GlobalCommand, Hint, ListRow

# Step key -> value. Choice values and typed text are both strings; flows.py interprets them.
Answers = dict[str, str]
# A choice with this value opens the step's file browser instead of being the answer.
BROWSE_VALUE: Final[str] = "\x00browse"
_BLANK_SHOWN: Final[str] = "(blank)"


@dataclass(frozen=True, slots=True)
class Choice:
    value: str
    label: str
    note: str = ""


@dataclass(frozen=True, slots=True)
class TextStep:
    """A typed field; ``validate`` returns an error to show, or None to accept."""

    key: str
    label: str
    default: str = ""
    placeholder: str = ""
    validate: Callable[[str, Answers], str | None] | None = None


@dataclass(frozen=True, slots=True)
class ChoiceStep:
    """A pick-list; a BROWSE_VALUE choice opens ``browse`` and answers with the picked path."""

    key: str
    label: str
    choices: tuple[Choice, ...]
    default: str = ""
    browse: BrowseSpec | None = None


Step = TextStep | ChoiceStep


@dataclass(frozen=True, slots=True)
class Confirm:
    """The summary a write is confirmed against: what will be written where."""

    title: str
    rows: tuple[Field, ...]
    verb: str


@dataclass(frozen=True, slots=True)
class WizardSpec:
    """A wizard: its steps given the answers so far, and the optional closing confirm."""

    title: str
    subtitle: str
    steps: Callable[[Answers], Sequence[Step]]
    confirm: Callable[[Answers], Confirm] | None = None


class WizardScreen(ChromeScreen[Answers | None]):
    """Walk a WizardSpec; dismiss with the answers, or None when the user backs out."""

    AUTO_FOCUS = ""  # nothing: keys go to the screen, so typing filters
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("shift+tab", "back", "Previous field", show=False, priority=True)
    ]
    DEFAULT_CSS = """
    WizardScreen {
        & #wizard { overflow-y: auto; }
        & #wizard-title { color: $accent; text-style: bold; }
        & #wizard-subtitle { color: $muted; margin: 0 0 1 0; }
        & #wizard-done { margin: 0 0 1 2; }
        & #wizard-step { height: auto; border-left: heavy $accent; padding: 0 0 0 1; }
        & #step-label { color: $accent; text-style: bold; }
        & #step-input { background: $background; }
        & #step-list { height: auto; max-height: 12; }
        & #step-error { color: $error; text-style: bold; }
        & #confirm-prompt { margin: 1 0 0 0; color: $warning; text-style: bold; }
    }
    """

    def __init__(self, chrome: ChromeState, spec: WizardSpec) -> None:
        super().__init__(chrome)
        self.spec = spec
        self._answers: Answers = {}  # every answer given, kept as the prefill when going back
        self._order: list[str] = []  # the keys answered so far, in order
        self._shown: dict[str, tuple[str, str]] = {}  # key -> (field label, answer as shown)
        self._step: Step | None = None
        self._confirm: Confirm | None = None

    def compose_body(self) -> ComposeResult:
        with Vertical(id="wizard", classes="panel -focused"):
            yield Static(id="wizard-title")
            yield Static(id="wizard-subtitle")
            yield DetailsView(id="wizard-done")
            with Vertical(id="wizard-step"):
                yield Static(id="step-label")
                yield Input(compact=True, id="step-input")
                yield FilterList(id="step-list")
                yield DetailsView(id="confirm-rows")
                yield Static(id="confirm-prompt")
                yield Static(id="step-error")

    @property
    def answered(self) -> Answers:
        return {key: self._answers[key] for key in self._order}

    @property
    def step(self) -> Step | None:
        return self._step

    @property
    def confirm(self) -> Confirm | None:
        return self._confirm

    @property
    def choice_list(self) -> FilterList:
        return self.query_one("#step-list", FilterList)

    @property
    def text_input(self) -> Input:
        return self.query_one("#step-input", Input)

    def on_mount(self) -> None:
        self.query_one("#wizard-title", Static).update(Text(self.spec.title))
        self.query_one("#wizard-subtitle", Static).update(Text(self.spec.subtitle))
        self._advance()

    # ----- flow ------------------------------------------------------------------

    def _advance(self) -> None:
        answered = self.answered
        steps = self.spec.steps(answered)
        self._step = next((step for step in steps if step.key not in answered), None)
        self._confirm = None
        if self._step is None:
            if self.spec.confirm is None:
                self.dismiss(answered)
                return
            self._confirm = self.spec.confirm(answered)
        self.choice_list.query_text = ""
        self._draw()

    def _accept(self, step: Step, value: str, shown: str) -> None:
        self._answers[step.key] = value
        self._order.append(step.key)
        self._shown[step.key] = (step.label, shown or _BLANK_SHOWN)
        self._advance()

    def action_back(self) -> None:
        """Step back to the previous field, keeping its answer as the prefill."""
        if not self._order:
            return
        self._order.pop()
        self._advance()

    # ----- drawing ---------------------------------------------------------------

    def _draw(self) -> None:
        step, confirm = self._step, self._confirm
        done = self.query_one("#wizard-done", DetailsView)
        done.set_content([self._shown[key] for key in self._order])
        # The summary restates every answer, so the confirm shows only the summary.
        done.display = bool(self._order) and confirm is None
        self.text_input.display = isinstance(step, TextStep)
        self.choice_list.display = isinstance(step, ChoiceStep)
        self.query_one("#confirm-rows").display = confirm is not None
        self.query_one("#confirm-prompt").display = confirm is not None
        self._set_error("")
        if isinstance(step, TextStep):
            self.text_input.placeholder = step.placeholder
            self.text_input.value = self._answers.get(step.key, step.default)
            self.text_input.cursor_position = len(self.text_input.value)
            self.text_input.focus()
        else:
            self.set_focus(None)
        if isinstance(step, ChoiceStep):
            self._show_choices(step)
        elif confirm is not None:
            self.query_one("#step-label", Static).update("Summary")
            self.query_one("#confirm-rows", DetailsView).set_content(confirm.rows)
            prompt = Text(f"? {confirm.verb} now?  y / n")
            self.query_one("#confirm-prompt", Static).update(prompt)
        else:
            self.query_one("#step-label", Static).update(Text(step.label if step else ""))
        self.refresh_footer()
        self.call_after_refresh(self.query_one("#wizard").scroll_end, animate=False)

    def _show_choices(self, step: ChoiceStep) -> None:
        query = self.choice_list.query_text
        terms = query.lower().split()
        rows = [
            ListRow(choice.value, choice.label, choice.note)
            for choice in step.choices
            if all(term in f"{choice.label} {choice.note}".lower() for term in terms)
        ]
        self.choice_list.empty_text = "Nothing matches. esc clears the filter."
        self.choice_list.set_rows(rows, self._answers.get(step.key, step.default))
        label = Text(step.label)
        if query:
            label.append(f"  › {query}▏")
        self.query_one("#step-label", Static).update(label)

    def _set_error(self, message: str) -> None:
        error = self.query_one("#step-error", Static)
        error.display = bool(message)
        error.update(Text(f"✗ {message}") if message else "")

    def footer_hints(self) -> Sequence[Hint]:
        back: list[Hint] = [("shift+tab", "back")] if self._order else []
        if self._confirm is not None:
            return [("y", self._confirm.verb.lower()), ("n/esc", "cancel"), *back]
        if isinstance(self._step, ChoiceStep):
            query = self.choice_list.query_text
            hints: list[Hint] = [("↑/↓", "move"), ("enter", "pick"), ("type", "to filter")]
            return [*hints, *back, ("esc", "clear filter" if query else "back out")]
        return [("enter", "next"), *back, ("esc", "back out"), ("ctrl+c", "quit")]

    # ----- keys ------------------------------------------------------------------

    def on_key(self, event: events.Key) -> None:
        if self._confirm is not None:
            event.stop()
            if event.character in ("y", "Y"):
                self.dismiss(self.answered)
            elif event.character in ("n", "N") or event.key == "escape":
                self.dismiss(None)
            return
        step = self._step
        if isinstance(step, TextStep):
            # The Input owns every other key; its bindings run after this handler.
            if event.key == "escape":
                event.stop()
                self.dismiss(None)
            return
        if isinstance(step, ChoiceStep):
            self._choice_key(step, event)

    def _choice_key(self, step: ChoiceStep, event: events.Key) -> None:
        choice_list = self.choice_list
        handled = True
        if event.character == COMMANDS_KEY:
            self.open_popup(
                popup_commands((), frozenset()), lambda c: self.post_message(GlobalCommand(c))
            )
        elif event.key == "escape":
            if choice_list.query_text:
                choice_list.query_text = ""
                self._show_choices(step)
                self.refresh_footer()
            else:
                self.dismiss(None)
        elif event.key == "enter":
            self._pick(step)
        else:
            outcome = choice_list.route_key(event)
            handled = outcome is not None
            if outcome == "filtered":
                self._show_choices(step)
                self.refresh_footer()
        if handled:
            event.stop()
            event.prevent_default()

    def _pick(self, step: ChoiceStep) -> None:
        row = self.choice_list.selected
        if row is None:
            return
        if row.key == BROWSE_VALUE and step.browse is not None:

            def picked(path: Path | None) -> None:
                if path is not None:
                    self._accept(step, str(path), str(path))

            self.app.push_screen(BrowserScreen(self.chrome, step.browse), picked)
            return
        self._accept(step, row.key, row.label)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        step = self._step
        if not isinstance(step, TextStep):
            return
        value = event.value.strip()
        error = step.validate(value, self.answered) if step.validate else None
        if error:
            self._set_error(error)
            return
        self._accept(step, value, value)
