"""Unit tests for the clamping layer (slice-4 spec B6.5).

These are the tests that prove acceptance criterion 12: whatever the model
invents, nothing oversized, misspelled or absurd leaves the service.
"""

from datetime import date
from typing import get_args

import pytest

from app import sanitize
from app.schemas import EditAction

TODAY = date(2026, 9, 2)


class TestCleanTitle:
    def test_truncates_a_500_char_title_to_200(self) -> None:
        title = sanitize.clean_title("A" * 500)
        assert title is not None
        assert len(title) == 200

    def test_collapses_whitespace_and_strips(self) -> None:
        assert sanitize.clean_title("  Call   the\n dentist  ") == "Call the dentist"

    def test_a_newline_becomes_a_space_not_nothing(self) -> None:
        """Control stripping must not weld words together."""
        assert sanitize.clean_title("Buy\nmilk") == "Buy milk"

    def test_a_tab_becomes_a_space_too(self) -> None:
        assert sanitize.clean_title("Buy\tmilk") == "Buy milk"

    def test_a_carriage_return_newline_becomes_one_space(self) -> None:
        assert sanitize.clean_title("Buy\r\nmilk") == "Buy milk"

    def test_drops_trailing_period(self) -> None:
        assert sanitize.clean_title("Buy milk.") == "Buy milk"

    @pytest.mark.parametrize("value", ["", "   ", "...", None, 42, {"title": "x"}])
    def test_rejects_unusable_titles(self, value: object) -> None:
        assert sanitize.clean_title(value) is None


class TestStripControlChars:
    def test_drops_nul_and_ansi_escapes(self) -> None:
        assert sanitize.strip_control_chars("a\x00b\x1b[31mc") == "ab[31mc"

    def test_drops_bidi_overrides_and_zero_width_joiners(self) -> None:
        assert sanitize.strip_control_chars("pay‮bob‍") == "paybob"

    def test_keeps_newlines_and_tabs_when_asked(self) -> None:
        assert sanitize.strip_control_chars("a\nb\tc", keep_newlines=True) == "a\nb\tc"

    def test_drops_nul_even_when_keeping_newlines(self) -> None:
        assert sanitize.strip_control_chars("a\x00\nb", keep_newlines=True) == "a\nb"

    def test_keeps_ordinary_unicode(self) -> None:
        assert sanitize.strip_control_chars("café é你好") == (
            "café é你好"
        )


class TestControlCharsInCleaners:
    def test_title_keeps_words_separated_while_dropping_controls(self) -> None:
        assert sanitize.clean_title("Buy\x00\nmilk\x1b and\tbread") == (
            "Buy milk and bread"
        )

    def test_title_drops_control_characters(self) -> None:
        assert sanitize.clean_title("Call‮ the\x00 dentist\x1b") == (
            "Call the dentist"
        )

    def test_description_drops_controls_but_keeps_newlines(self) -> None:
        assert sanitize.clean_description("line one\x00\nline​ two") == (
            "line one\nline two"
        )

    def test_summary_drops_controls_but_keeps_newlines(self) -> None:
        assert sanitize.clean_summary("One.\x00\n\nTwo‮.") == "One.\nTwo."

    def test_a_title_of_only_control_characters_is_rejected(self) -> None:
        assert sanitize.clean_title("\x00\x1b‍") is None


class TestCleanDescription:
    def test_truncates_to_2000(self) -> None:
        description = sanitize.clean_description("b" * 3000)
        assert description is not None
        assert len(description) == 2000

    def test_blank_becomes_none(self) -> None:
        assert sanitize.clean_description("   \n ") is None
        assert sanitize.clean_description(None) is None


class TestCleanPriority:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("low", "low"),
            ("HIGH", "high"),
            ("  Medium ", "medium"),
            ("URGENT", "medium"),
            ("critical", "medium"),
            ("", "medium"),
            (None, "medium"),
            (3, "medium"),
        ],
    )
    def test_coerces_unknown_priorities_to_medium(
        self, value: object, expected: str
    ) -> None:
        assert sanitize.clean_priority(value) == expected


class TestCleanDueDate:
    def test_parses_an_iso_date(self) -> None:
        assert sanitize.clean_due_date("2026-09-05", today=TODAY) == date(2026, 9, 5)

    def test_rejects_prose(self) -> None:
        assert sanitize.clean_due_date("next tuesday", today=TODAY) is None

    def test_rejects_a_date_far_in_the_future(self) -> None:
        assert sanitize.clean_due_date("2099-01-01", today=TODAY) is None

    def test_rejects_a_date_far_in_the_past(self) -> None:
        assert sanitize.clean_due_date("1970-01-01", today=TODAY) is None

    def test_accepts_a_date_inside_the_horizon(self) -> None:
        assert sanitize.clean_due_date("2028-01-01", today=TODAY) == date(2028, 1, 1)

    @pytest.mark.parametrize("value", [None, "", "   ", 20260905, "2026-13-45"])
    def test_rejects_junk(self, value: object) -> None:
        assert sanitize.clean_due_date(value, today=TODAY) is None


class TestCleanTags:
    def test_normalizes_dedupes_and_drops_invalid(self) -> None:
        assert sanitize.clean_tags(["Home", "home", "bad!", "  work  "]) == [
            "home",
            "work",
        ]

    def test_collapses_inner_whitespace(self) -> None:
        assert sanitize.clean_tags(["deep    work"]) == ["deep work"]

    def test_caps_at_five(self) -> None:
        tags = sanitize.clean_tags([f"tag{index}" for index in range(12)])
        assert len(tags) == 5

    @pytest.mark.parametrize(
        "value", ["", "  ", "-leading", "x" * 31, "emoji \U0001f600", "a/b", None, 7]
    )
    def test_rejects_invalid_tags(self, value: object) -> None:
        assert sanitize.normalize_tag(value) is None

    def test_non_list_input_is_empty(self) -> None:
        assert sanitize.clean_tags("home,work") == []


class TestCleanTitles:
    def test_caps_twelve_subtasks_at_max_items(self) -> None:
        titles = sanitize.clean_titles(
            [f"Step {index}" for index in range(12)], max_items=5
        )
        assert titles == [f"Step {index}" for index in range(5)]

    def test_never_exceeds_the_hard_cap_of_ten(self) -> None:
        titles = sanitize.clean_titles(
            [f"Step {index}" for index in range(30)], max_items=99
        )
        assert len(titles) == 10

    def test_drops_empty_titles(self) -> None:
        assert sanitize.clean_titles(["", "  ", "Do it", "."], max_items=5) == ["Do it"]

    def test_drops_case_insensitive_duplicates(self) -> None:
        assert sanitize.clean_titles(["Call Bob", "call bob"], max_items=5) == [
            "Call Bob"
        ]


class TestCleanSummary:
    def test_truncates_3000_chars_to_at_most_800_on_a_word_boundary(self) -> None:
        summary = sanitize.clean_summary(" ".join(["word"] * 1000))
        assert len(summary) <= 800
        assert summary.endswith("…")
        assert "wor…" not in summary  # cut on a boundary, not mid-word

    def test_keeps_short_text_untouched(self) -> None:
        assert sanitize.clean_summary("  You have 3 open todos.  ") == (
            "You have 3 open todos."
        )

    def test_collapses_blank_lines(self) -> None:
        assert sanitize.clean_summary("One.\n\n\n  Two.") == "One.\nTwo."

    def test_non_string_becomes_empty(self) -> None:
        assert sanitize.clean_summary(None) == ""


# --------------------------------------------------------------------------- #
# ``POST /ai/edit-todo`` (iteration 5, spec §3.1 A3)
#
# The editing helpers exist because the drafting ones have the wrong *default*:
# a draft may be coerced into shape, an edit may not, because every value that
# survives here is presented to the user as a change they asked for.
# --------------------------------------------------------------------------- #


class TestCleanOptionalPriority:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [("low", "low"), ("MEDIUM", "medium"), ("  high  ", "high")],
    )
    def test_accepts_the_three_real_priorities(
        self, value: str, expected: str
    ) -> None:
        assert sanitize.clean_optional_priority(value) == expected

    @pytest.mark.parametrize(
        "value", ["URGENT", "", "   ", "highest", "1", None, 3, ["high"], True]
    )
    def test_anything_else_is_no_change(self, value: object) -> None:
        assert sanitize.clean_optional_priority(value) is None

    def test_it_differs_from_clean_priority_exactly_where_it_matters(self) -> None:
        """The trap the spec calls out by name.

        ``clean_priority`` is right for a draft — the todo needs *a* priority.
        On an edit the same coercion invents a change out of model garbage.
        """
        assert sanitize.clean_priority("URGENT") == "medium"
        assert sanitize.clean_optional_priority("URGENT") is None


class TestCleanFlag:
    @pytest.mark.parametrize("value", [True, False])
    def test_a_real_boolean_survives(self, value: bool) -> None:
        assert sanitize.clean_flag(value) is value

    @pytest.mark.parametrize(
        "value", ["true", "yes", "no", "", 0, 1, None, [], {"completed": True}]
    )
    def test_anything_else_is_no_change(self, value: object) -> None:
        """Not truthiness: every one of these would flip a todo's state."""
        assert sanitize.clean_flag(value) is None


class TestCleanClearFields:
    def test_keeps_only_the_two_clearable_fields(self) -> None:
        assert sanitize.clean_clear_fields(
            ["description", "due_date", "title", "tags", "completed"]
        ) == {"description", "due_date"}

    def test_normalizes_case_and_whitespace(self) -> None:
        assert sanitize.clean_clear_fields([" Description ", "DUE_DATE"]) == {
            "description",
            "due_date",
        }

    @pytest.mark.parametrize("value", [None, "description", 7, {}])
    def test_a_non_list_clears_nothing(self, value: object) -> None:
        assert sanitize.clean_clear_fields(value) == set()

    def test_non_string_entries_are_ignored(self) -> None:
        assert sanitize.clean_clear_fields([7, None, "due_date"]) == {"due_date"}


class TestCleanTagEdits:
    def test_normalizes_both_lists(self) -> None:
        added, removed = sanitize.clean_tag_edits(["Health", "not a tag!"], ["  HOME "])

        assert added == ["health"]
        assert removed == ["home"]

    def test_a_name_in_both_lists_ends_up_in_neither(self) -> None:
        """A model that adds and removes the same tag is contradicting itself.

        Guessing which half it meant would be inventing intent; dropping both
        leaves the user's tags exactly as they were.
        """
        added, removed = sanitize.clean_tag_edits(["health", "work"], ["Health"])

        assert added == ["work"]
        assert removed == []

    def test_each_list_is_capped_at_ten(self) -> None:
        added, _ = sanitize.clean_tag_edits([f"tag{n}" for n in range(20)], [])

        assert len(added) == 10

    def test_non_lists_are_empty(self) -> None:
        assert sanitize.clean_tag_edits("health", None) == ([], [])


class TestCleanIndex:
    @pytest.mark.parametrize("value", [1, 2, 3, "2", " 3 "])
    def test_accepts_a_position_inside_the_snapshot(self, value: object) -> None:
        assert sanitize.clean_index(value, subtask_count=3) is not None

    @pytest.mark.parametrize("value", [0, 4, -1, "two", "", 1.5, None, [1], {}])
    def test_rejects_everything_outside_it(self, value: object) -> None:
        assert sanitize.clean_index(value, subtask_count=3) is None

    @pytest.mark.parametrize("value", [True, False])
    def test_a_boolean_is_not_a_position(self, value: bool) -> None:
        """``bool`` is an ``int`` subclass, so ``True`` would be position 1."""
        assert sanitize.clean_index(value, subtask_count=3) is None

    def test_no_position_exists_in_a_childless_snapshot(self) -> None:
        assert sanitize.clean_index(1, subtask_count=0) is None


def op(action: object, index: object = None, title: object = None) -> dict:
    return {"action": action, "index": index, "title": title}


class TestCleanEditOps:
    def test_the_five_actions_survive(self) -> None:
        ops = sanitize.clean_edit_ops(
            [
                op("rename", 1, "Book the appointment"),
                op("complete", 2),
                op("reopen", 3),
                op("remove", 4),
                op("add", None, "Find the number"),
            ],
            subtask_count=4,
        )

        assert [item.action for item in ops] == [
            "rename",
            "complete",
            "reopen",
            "remove",
            "add",
        ]

    @pytest.mark.parametrize("action", ["delete_todo", "move", "", "RENAME ALL", None])
    def test_an_unknown_action_is_dropped(self, action: object) -> None:
        assert sanitize.clean_edit_ops([op(action, 1, "x")], subtask_count=2) == []

    def test_an_action_is_matched_case_insensitively(self) -> None:
        ops = sanitize.clean_edit_ops([op(" Remove ", 1)], subtask_count=2)

        assert [item.action for item in ops] == ["remove"]

    def test_an_out_of_range_index_drops_the_operation(self) -> None:
        assert (
            sanitize.clean_edit_ops(
                [op("remove", 3), op("rename", 0, "x")], subtask_count=2
            )
            == []
        )

    def test_a_rename_needs_a_title(self) -> None:
        assert sanitize.clean_edit_ops([op("rename", 1, "   ")], subtask_count=2) == []

    def test_an_add_needs_a_title(self) -> None:
        assert sanitize.clean_edit_ops([op("add", None, None)], subtask_count=2) == []

    def test_an_add_never_keeps_an_index(self) -> None:
        ops = sanitize.clean_edit_ops([op("add", 2, "New step")], subtask_count=2)

        assert ops == [sanitize.EditOperation("add", None, "New step")]

    def test_a_positional_action_never_keeps_a_title(self) -> None:
        ops = sanitize.clean_edit_ops([op("complete", 1, "Book it")], subtask_count=2)

        assert ops == [sanitize.EditOperation("complete", 1, None)]

    def test_operations_after_a_remove_of_the_same_position_are_dropped(self) -> None:
        ops = sanitize.clean_edit_ops(
            [op("remove", 1), op("rename", 1, "Too late"), op("complete", 1)],
            subtask_count=2,
        )

        assert ops == [sanitize.EditOperation("remove", 1, None)]

    def test_a_repeated_action_and_position_is_dropped(self) -> None:
        ops = sanitize.clean_edit_ops(
            [op("complete", 1), op("complete", 1)], subtask_count=2
        )

        assert len(ops) == 1

    def test_two_different_actions_on_one_position_both_survive(self) -> None:
        """The backend enforces one op per subtask id; this layer only bounds.

        Dropping the second here would hide a real pairing (rename *and*
        complete the same step) that the caller is better placed to resolve.
        """
        ops = sanitize.clean_edit_ops(
            [op("rename", 1, "Book the appointment"), op("complete", 1)],
            subtask_count=2,
        )

        assert len(ops) == 2

    def test_several_adds_survive_because_they_have_no_position(self) -> None:
        ops = sanitize.clean_edit_ops(
            [op("add", None, "One"), op("add", None, "Two")], subtask_count=0
        )

        assert [item.title for item in ops] == ["One", "Two"]

    def test_a_repeated_add_title_is_dropped(self) -> None:
        """A 3B model happily repeats the same step with new capitalization."""
        ops = sanitize.clean_edit_ops(
            [op("add", None, "Buy candles"), op("add", None, "buy candles.")],
            subtask_count=0,
        )

        assert len(ops) == 1

    def test_an_add_of_a_subtask_that_already_exists_is_dropped(self) -> None:
        """Measured, not hypothetical: on the live lane ``qwen2.5:3b`` answered a
        bare rename by re-adding both existing subtasks.

        It is the one restatement the caller cannot recognise as a no-op — an
        ``add`` of an existing title is a *duplicate*, not a repeat.
        """
        ops = sanitize.clean_edit_ops(
            [op("add", None, "book it."), op("add", None, "Find the number")],
            subtask_count=2,
            existing_titles=["Book it", "Pay the invoice"],
        )

        assert [item.title for item in ops] == ["Find the number"]

    def test_a_rename_to_an_existing_title_is_untouched(self) -> None:
        """There the position says which row is meant, so it is unambiguous."""
        ops = sanitize.clean_edit_ops(
            [op("rename", 2, "Book it")],
            subtask_count=2,
            existing_titles=["Book it", "Pay the invoice"],
        )

        assert ops == [sanitize.EditOperation("rename", 2, "Book it")]

    def test_twelve_operations_are_capped_at_ten(self) -> None:
        ops = sanitize.clean_edit_ops(
            [op("add", None, f"Step {n}") for n in range(12)], subtask_count=0
        )

        assert len(ops) == sanitize.MAX_EDIT_OPS == 10

    @pytest.mark.parametrize("values", [None, "remove", {"action": "remove"}, 7])
    def test_a_non_list_yields_no_operations(self, values: object) -> None:
        assert sanitize.clean_edit_ops(values, subtask_count=2) == []

    @pytest.mark.parametrize("value", ["remove", None, 7, ["remove", 1]])
    def test_an_entry_that_is_not_an_object_is_dropped(self, value: object) -> None:
        assert sanitize.clean_edit_ops([value], subtask_count=2) == []

    def test_one_bad_operation_does_not_cost_the_good_ones(self) -> None:
        ops = sanitize.clean_edit_ops(
            [op("remove", "two"), op("rename", 1, "Book the appointment")],
            subtask_count=2,
        )

        assert ops == [sanitize.EditOperation("rename", 1, "Book the appointment")]


def test_the_action_vocabulary_matches_the_wire_contract() -> None:
    """``sanitize`` filters against this tuple; the response model types it.

    If they drifted, a value could survive clamping and then fail *response*
    validation — a 500 caused by a model quirk, which is exactly the class of
    failure this layer exists to prevent.
    """
    assert set(sanitize.EDIT_ACTIONS) == set(get_args(EditAction))
