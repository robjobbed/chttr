"""Command mode's local edits: no model needed."""

from __future__ import annotations

import pytest

from chttr import commands as CM


@pytest.mark.parametrize(("instruction", "selection", "result"), [
    ("sort lines", "b\na", "a\nb"),
    ("Sort the lines.", "pear\nApple\nfig", "Apple\nfig\npear"),
    ("sort lines in reverse", "a\nc\nb", "c\nb\na"),
    ("sort descending", "a\nc\nb", "c\nb\na"),
    ("upper case", "hello there", "HELLO THERE"),
    ("Um, can you make it all caps, please?", "hello", "HELLO"),
    ("upper cases", "hi", "HI"),                                  # a small mis-hearing
    ("lower case", "LOUD Words", "loud words"),
    ("title case", "the lord of the rings", "The Lord of the Rings"),
    ("sentence case", "THIS IS LOUD. so is this", "This is loud. So is this"),
    ("join lines", "one\n  two\n\nthree", "one two three"),
    ("make it one line", "a\nb", "a b"),
    ("remove blank lines", "a\n\n\nb", "a\nb"),
    ("remove duplicates", "a\nb\na\nB", "a\nb"),
    ("trim whitespace", "  a  \n  b ", "a\nb"),
    ("wrap in quotes", "hello", "\"hello\""),
    ("put it in single quotes", "hello", "'hello'"),
    ("wrap in backticks", "npm test", "`npm test`"),
    ("wrap in parentheses", "aside", "(aside)"),
    ("wrap in brackets", "x", "[x]"),
    ("bullet list", "milk\neggs", "- milk\n- eggs"),
    ("make it a bullet list", "apples, pears and plums", "- apples\n- pears\n- plums"),
    ("turn this into a numbered list", "First. Second. Third.", "1. First.\n2. Second.\n3. Third."),
    ("numbered list", "- milk\n- eggs", "1. milk\n2. eggs"),
    ("remove bullets", "- milk\n* eggs\n3. jam", "milk\neggs\njam"),
    ("delete that", "some words", ""),
    ("clear", "some words", ""),
])
def test_local_commands(instruction, selection, result):
    assert CM.local_command(instruction, selection) == result


@pytest.mark.parametrize("instruction", [
    "make it shorter", "make it short",           # "short" is not "sort"
    "translate this into French", "fix the grammar", "make it more formal", "remove the commas", "summarize",
    "", "um",
])
def test_anything_else_needs_a_model(instruction):
    assert CM.local_command(instruction, "some selected text") is None


@pytest.mark.parametrize("instruction", ["remove spaces", "remove empty spaces", "remove the spaces"])
def test_a_different_edit_is_not_a_mis_hearing(instruction):
    assert CM.local_command(instruction, "a  b\n\nc") is None


def test_with_a_model_only_exact_phrases_are_done_locally():
    assert CM.local_command("upper case", "hi", fuzzy=False) == "HI"
    assert CM.local_command("Please make it upper case.", "hi", fuzzy=False) == "HI"
    assert CM.local_command("upper cases", "hi", fuzzy=False) is None       # the model reads it


def test_writing_new_text_needs_a_model():
    assert CM.local_command("upper case", "") is None


def test_the_whitespace_around_a_selection_stays():
    assert CM.local_command("sort lines", "b\na\n") == "a\nb\n"
    assert CM.local_command("wrap in quotes", "  hi ") == "  \"hi\" "
