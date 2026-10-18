"""Personal Assistant — deterministic command grammar (pure: no database, no network, no LLM)."""
import os

os.environ.setdefault("JWT_SECRET", "dummy")
os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")

import pytest

from personal_assistant.parser import parse_message, split_names


def one(text):
    segs = parse_message(text)
    assert len(segs) == 1, [s.text for s in segs]
    return segs[0]


@pytest.mark.parametrize("text,lookup,names", [
    ("Show me Angela Kumar's profile.", "profile", ["Angela Kumar"]),
    ("Show me Angela Kumar's media link.", "media_link", ["Angela Kumar"]),
    ("Find Angela Kumar.", "find", ["Angela Kumar"]),
    ("Show Angela's photos and introduction video.", "media", ["Angela"]),
    ("Has Angela tested for any previous projects?", "tested", ["Angela"]),
    ("Which projects has Angela been added to?", "projects", ["Angela"]),
    ("Show me Angela's submission history.", "submissions", ["Angela"]),
    ("show me priya sharma", "profile", ["priya sharma"]),
])
def test_talent_lookups(text, lookup, names):
    s = one(text)
    assert (s.intent, s.lookup, s.talents) == ("lookup", lookup, names)


def test_add_variants():
    s = one("Add Angela Kumar to Amazon Now.")
    assert (s.intent, s.talents, s.project_text) == ("add", ["Angela Kumar"], "Amazon Now")
    s = one("Add Angela, Riya and Neha to Amazon Now.")
    assert s.talents == ["Angela", "Riya", "Neha"] and s.project_text == "Amazon Now"
    s = one("Add these five talents to Amazon and Pepsi.")
    assert (s.talent_ref, s.talents, s.project_text) == ("selected", [], "Amazon and Pepsi")
    s = one("Add these selected talents to the current project.")
    assert (s.talent_ref, s.project_ref) == ("selected", "current")


def test_send_variants():
    s = one("Send the casting call for Amazon Now to Angela and Riya.")
    assert (s.intent, s.template, s.talents, s.project_text) == ("send", "casting call", ["Angela", "Riya"], "Amazon Now")
    s = one("Send the Follow Up template to these talents for Amazon Now.")
    assert (s.template, s.talent_ref, s.project_text) == ("Follow Up", "selected", "Amazon Now")
    s = one("Send the Shortlisted template to these five talents for Pepsi.")
    assert (s.template, s.talent_ref, s.project_text) == ("Shortlisted", "selected", "Pepsi")
    s = one("Send the casting call to the selected talents in Ask to Test.")
    assert (s.template, s.talent_ref, s.stage_filter) == ("casting call", "selected", "ask_to_test")
    s = one("Send the casting call to Angela via worker 2")
    assert s.worker_hint == "worker 2" and s.talents == ["Angela"]


def test_pipeline_variants():
    s = one("Move Angela to Follow-up in Amazon.")
    assert (s.intent, s.talents, s.stage, s.project_text) == ("move", ["Angela"], "follow_up", "Amazon")
    s = one("Move these three talents to Shortlisted in Pepsi.")
    assert (s.talent_ref, s.stage, s.project_text) == ("selected", "shortlisted", "Pepsi")
    s = one("Move Angela to Ask to Test in Amazon.")                       # "to" inside the stage name
    assert (s.stage, s.project_text) == ("ask_to_test", "Amazon")
    s = one("Show me everyone in Ask to Test.")
    assert (s.intent, s.stage) == ("list", "ask_to_test")
    s = one("Which talents are awaiting a test?")
    assert (s.intent, s.stage) == ("list", "ask_to_test")
    s = one("show everyone in shortlisted for pepsi")
    assert (s.stage, s.project_text) == ("shortlisted", "pepsi")
    s = one("Mark these talents as reached out.")
    assert (s.intent, s.stage, s.talent_ref) == ("move", "follow_up", "selected")


def test_share_variants():
    s = one("Share Angela's media link")
    assert (s.intent, s.lookup, s.talents) == ("share", "media_link", ["Angela"])
    s = one("Send Angela's audition to the casting director.")
    assert (s.intent, s.lookup, s.recipient["kind"], s.recipient["value"]) == ("share", "audition", "role", "casting_director")
    s = one("Send this profile to this project group.")
    assert (s.intent, s.talent_ref, s.recipient["kind"]) == ("share", "selected", "project_group")
    s = one("Share the profile with this WhatsApp number +91 98765 43210")
    assert s.recipient == {"kind": "number", "value": "+919876543210"}


def test_multi_command_message_is_split_with_dependencies_in_mind():
    segs = parse_message("Add Angela and Riya to Amazon Now and send them the Casting Call. Also add Neha to Pepsi and send her the Follow Up template.")
    assert [s.intent for s in segs] == ["add", "send", "add", "send"]
    assert segs[0].talents == ["Angela", "Riya"] and segs[0].project_text == "Amazon Now"
    assert segs[1].talent_ref == "previous" and segs[1].template == "Casting Call"      # "them" = the previous task's talents
    assert segs[2].talents == ["Neha"] and segs[2].project_text == "Pepsi"
    assert segs[3].talent_ref == "previous" and segs[3].template == "Follow Up"


def test_add_then_ask_whether_to_send():
    segs = parse_message("Add these talents to Amazon, then ask whether I want to send the casting call.")
    assert [s.intent for s in segs] == ["add", "offer_send"]


def test_a_plain_and_between_names_never_splits_but_a_new_verb_does():
    assert len(parse_message("Add Angela and Riya to Amazon")) == 1
    assert len(parse_message("Add Angela to Amazon and move Riya to Approved in Amazon")) == 2
    assert len(parse_message("Move angela and riya to approved for Pepsi, also send them the follow up template")) == 2


def test_informal_phrasing_and_names_with_possessives():
    s = one("hey can you please add angela kumar to amazon now")
    assert (s.intent, s.talents, s.project_text) == ("add", ["angela kumar"], "amazon now")
    assert split_names("Angela's, Riya & Neha") == ["Angela", "Riya", "Neha"]


def test_unknown_and_empty():
    assert parse_message("") == [] and parse_message("   ") == []
    assert one("what is the weather like").intent == "unknown"


def test_no_external_ai_dependency_in_the_assistant_package():
    import pathlib, re
    banned = re.compile(r"\b(?:anthropic|openai|google\.generativeai|genai|gemini|claude|langchain|litellm|mistralai|cohere)\b", re.I)
    for f in pathlib.Path(__file__).resolve().parent.parent.joinpath("personal_assistant").glob("*.py"):
        for ln, line in enumerate(f.read_text().splitlines(), 1):
            code = line.split("#", 1)[0]
            assert not re.search(r"^\s*(?:from|import)\s+.*" + banned.pattern, code, re.I), f"{f.name}:{ln} {line}"


def test_adversarial_and_oversized_input_is_handled_quickly():
    import time
    nasty = ("add " + "a " * 900 + "to " + "b, " * 300)[:2000]
    t0 = time.perf_counter()
    parse_message(nasty)
    parse_message("show me " + "x'" * 900)
    parse_message(("Add Angela to Amazon. " * 100)[:2000])
    assert time.perf_counter() - t0 < 2.0


def test_the_four_acceptance_examples_are_each_understood_completely():
    a = parse_message("Add Angela Kumar to Amazon Now and send her the Casting Call.")
    assert [x.intent for x in a] == ["add", "send"] and a[1].talent_ref == "previous" and a[1].template == "Casting Call"
    b = parse_message("Send the Follow Up template to these three talents for Pepsi.")
    assert [(x.intent, x.talent_ref, x.project_text, x.template) for x in b] == [("send", "selected", "Pepsi", "Follow Up")]
    c = parse_message("Move these talents to Follow-up, and add these other talents to Amazon.")
    assert [(x.intent, x.talent_ref) for x in c] == [("move", "selected"), ("add", "ask")]            # "other" ≠ the selection
    d = parse_message("Show me Angela Kumar's profile, and tell me which projects she has tested for.")
    assert [(x.intent, x.lookup, x.talent_ref) for x in d] == [("lookup", "profile", None), ("lookup", "tested", "previous")]
    assert d[0].talents == ["Angela Kumar"]


def test_pronoun_followups_refer_to_the_previous_talent():
    for text, kind in [("Has she tested before?", "tested"), ("which projects has he been added to", "projects"), ("show her submission history", "submissions"),
                       ("and her media link", "media_link")]:
        segs = parse_message("Show me Angela's profile. " + text)
        assert segs[-1].intent == "lookup" and segs[-1].lookup == kind and segs[-1].talent_ref == "previous", text


def test_share_recipients_named_role_number_group_and_missing():
    s = one("Send Angela Kumar's media link to Priya Nair")
    assert s.recipient == {"kind": "named", "value": "Priya Nair"}
    s = one("Share Angela's profile with the casting director for Amazon")
    assert s.recipient["kind"] == "role" and s.recipient["value"] == "casting_director" and s.project_text == "Amazon"
    s = one("Share Angela's profile with the WhatsApp group for Amazon Now")
    assert s.recipient["kind"] == "project_group" and s.project_text == "Amazon Now"
    s = one("Send her media link to Priya Nair")
    assert s.talent_ref == "previous" and s.recipient["value"] == "Priya Nair"
    assert one("Share Angela's media link").recipient is None


@pytest.mark.parametrize("text, names, role", [
    ("Share the media link of ZQA-PA Angela with the casting director", ["ZQA-PA Angela"], "casting_director"),
    ("Send the profile of Angela Kumar to the casting director for Amazon", ["Angela Kumar"], "casting_director"),
    ("Send the portfolio of Maya to the casting director", ["Maya"], "casting_director"),
])
def test_a_talent_name_never_swallows_the_recipient_that_follows_it(text, names, role):
    s = one(text)
    assert s.talents == names and s.recipient["kind"] == "role" and s.recipient["value"] == role
