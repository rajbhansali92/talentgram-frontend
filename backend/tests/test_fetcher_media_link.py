"""Fletcher "Show me <talent>'s media link" — must return the ONE canonical
Talent Media Download Link (routers/talent_media.py), honour its toggle, and
leave the existing form / profile commands untouched. Real local Mongo,
end-to-end through the dispatcher (same harness as test_talentgram_fetcher)."""
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import db  # noqa: E402
from agents.modules import talentgram_fetcher as fetcher  # noqa: E402
from routers import talent_media as tm  # noqa: E402

from tests.test_media_assignment import _restore_config, _seed_project, _use_test_config  # noqa: E402
from tests.test_talentgram_fetcher import (  # noqa: E402
    _cleanup_fetcher, _cleanup_links, _seed_link, _seed_submission_with_form,
    _seed_talent_full, _show_me,
)

pytestmark = pytest.mark.asyncio(loop_scope="module")
AGENT_ID = fetcher.AGENT_ID

BODY = "Click the link to view images & introduction video or Download:"


class _Env:
    """Group config + cleanup in one place so each test stays readable."""
    def __init__(self):
        self.group = f"Test Fetcher {uuid.uuid4().hex[:6]}"
        self.tag = uuid.uuid4().hex[:6]
        self.talents, self.projects = [], []

    async def __aenter__(self):
        self.original = await _use_test_config(self.group, agent_id=AGENT_ID)
        return self

    async def talent(self, name, **kw):
        tid = await _seed_talent_full(name, **kw)
        self.talents.append(tid)
        return tid

    async def ask(self, text):
        return await _show_me(self.group, text)

    async def __aexit__(self, *exc):
        await _cleanup_links(talent_ids=self.talents)
        await _cleanup_fetcher(talent_ids=self.talents, project_ids=self.projects)
        await _restore_config(self.original, agent_id=AGENT_ID)


async def _canonical_url(tid):
    return tm.media_link_url(await tm.ensure_token(tid))


# 1 + 2 + 3 — first-name and full-name requests return the exact canonical URL
async def test_angela_media_link_exact_format_and_canonical_url():
    async with _Env() as e:
        tid = await e.talent(f"Angelaq{e.tag} Kumar")
        url = await _canonical_url(tid)
        r = await e.ask(f"Show me Angelaq{e.tag}'s media link")
        assert r.handled, r
        assert r.reply == f"Talentgram X Angelaq{e.tag} K -\n\n{BODY}\n\n{url}"
        # full name, plus the "media" short form, resolve to the same talent + URL
        for phrase in (f"Show me Angelaq{e.tag} Kumar's media link", f"Show me Angelaq{e.tag} Kumar's media"):
            r2 = await e.ask(phrase)
            assert r2.reply == r.reply, phrase
        # same link the submission notification embeds (character-for-character)
        notif = tm.build_media_link_message(f"Angelaq{e.tag} Kumar", await tm.ensure_token(tid))
        assert notif.splitlines()[-1] == url
        assert r.reply.splitlines()[-1] == url


# 4 — Fanny
async def test_fanny_media_link():
    async with _Env() as e:
        tid = await e.talent(f"Fannyq{e.tag} Gandhi")
        r = await e.ask(f"Show me Fannyq{e.tag} Gandhi's media link")
        assert r.reply.startswith(f"Talentgram X Fannyq{e.tag} G -\n\n{BODY}\n\n")
        assert r.reply.endswith(await _canonical_url(tid))


# 5 + 6 — toggle OFF hides the link (and offers no fallback); ON restores the SAME link
async def test_toggle_off_then_on_same_link():
    async with _Env() as e:
        tid = await e.talent(f"Togglq{e.tag} Singh")
        await _seed_link(tid, title=f"Talentgram x Togglq{e.tag}")  # an old portfolio link exists...
        url = await _canonical_url(tid)
        ask = f"Show me Togglq{e.tag}'s media link"
        assert (await e.ask(ask)).reply.endswith(url)

        await db.talents.update_one({"id": tid}, {"$set": {"media_download_enabled": False}})
        off = (await e.ask(ask)).reply
        assert off == f"Talentgram X Togglq{e.tag} S -\n\nMedia download link is currently unavailable."
        assert "http" not in off and "/l/" not in off  # ...and must NOT be offered as a fallback

        await db.talents.update_one({"id": tid}, {"$set": {"media_download_enabled": True}})
        assert (await e.ask(ask)).reply == f"Talentgram X Togglq{e.tag} S -\n\n{BODY}\n\n{url}"


# 7 — form command unchanged, even when the PROJECT is called "...Media..."
async def test_form_command_unchanged_even_with_media_in_project_name():
    async with _Env() as e:
        tid = await e.talent(f"Formq{e.tag} Sharma")
        pid = await _seed_project(f"Media Campaign {e.tag}")
        e.projects.append(pid)
        await _seed_submission_with_form(pid, tid, original_form_data={"first_name": "Formq", "last_name": "Sharma"})
        r = await e.ask(f"Show me Formq{e.tag} Sharma's form for Media Campaign {e.tag}")
        assert r.handled, r
        assert "Form" in r.reply and "Formq - S" in r.reply
        assert "Click the link" not in r.reply and "Talentgram X" not in r.reply


# 8 — profile command unchanged (old portfolio link + format still served by profile)
async def test_profile_command_unchanged():
    async with _Env() as e:
        tid = await e.talent(f"Profq{e.tag} Shah")
        slug = await _seed_link(tid)
        r = await e.ask(f"Show me the profile of Profq{e.tag} Shah")
        assert r.handled, r
        assert f"Talentgram X Profq{e.tag} Shah\n\nClick to view the portfolio:\n\nhttps://links.talentgramagency.com/l/{slug}" in r.reply
        assert "talent-media" not in r.reply


# 9 — unknown talent -> the existing not-found wording
async def test_unknown_talent():
    async with _Env() as e:
        r = await e.ask("Show me Xyzzyq9 Qwertyuiopq's media link")
        assert r.handled, r
        assert "I couldn't find" in r.reply and "in Talentgram" in r.reply
        assert "http" not in r.reply


# 10 — repeating the command returns the same URL, mints no new token, queues no WhatsApp job
async def test_repeat_is_idempotent_no_new_token_no_job():
    async with _Env() as e:
        tid = await e.talent(f"Repeatq{e.tag} Mehta")
        first_token = await tm.ensure_token(tid)
        jobs_before = await db.whatsapp_jobs.count_documents({})
        r1 = await e.ask(f"Show me Repeatq{e.tag}'s media link")
        r2 = await e.ask(f"Show me Repeatq{e.tag}'s media link")
        assert r1.reply == r2.reply
        assert (await db.talents.find_one({"id": tid}))["media_download_token"] == first_token
        assert r1.reply.endswith(tm.media_link_url(first_token))
        assert await db.whatsapp_jobs.count_documents({}) == jobs_before


# first-ever request for a talent with no token yet uses (mints once) the canonical token — not a second system
async def test_talent_without_token_gets_the_canonical_token_minted_once():
    async with _Env() as e:
        tid = await e.talent(f"Newq{e.tag} Rao")
        assert "media_download_token" not in (await db.talents.find_one({"id": tid}))
        r = await e.ask(f"Show me Newq{e.tag}'s media link")
        token = (await db.talents.find_one({"id": tid}))["media_download_token"]
        assert r.reply.endswith(tm.media_link_url(token))
        await e.ask(f"Show me Newq{e.tag}'s media link")
        assert (await db.talents.find_one({"id": tid}))["media_download_token"] == token


# ambiguity: resumable numbered choice, then the canonical link for the chosen talent
async def test_ambiguous_name_asks_then_returns_link():
    async with _Env() as e:
        t1 = await e.talent(f"Dupq{e.tag} Shah", phone="9111111111")
        await e.talent(f"Dupq{e.tag} Shah", phone="9222222222")
        r = await e.ask(f"Show me Dupq{e.tag} Shah's media link")
        assert "Which" in r.reply and "1 →" in r.reply
        r2 = await e.ask("1")
        assert r2.handled and "Click the link" in r2.reply and "/talent-media/" in r2.reply


# multiple names -> one block each
async def test_multiple_names():
    async with _Env() as e:
        a = await e.talent(f"Multiaq{e.tag} Jain")
        b = await e.talent(f"Multibq{e.tag} Roy")
        r = await e.ask(f"Show me Multiaq{e.tag}, Multibq{e.tag} media links")
        assert r.reply.count("Click the link") == 2
        assert await _canonical_url(a) in r.reply and await _canonical_url(b) in r.reply


# filtered-talents search containing the word "media" is not hijacked
async def test_filtered_talents_search_not_hijacked():
    async with _Env() as e:
        r = await e.ask("Show me all female talents with media experience in Mumbai")
        assert "Click the link" not in r.reply


# live-ness: the link Fletcher returns is the same one whose page reflects later media edits
async def test_link_is_the_live_page_not_a_snapshot():
    async with _Env() as e:
        tid = await e.talent(f"Liveq{e.tag} Kapoor")
        r1 = await e.ask(f"Show me Liveq{e.tag}'s media link")
        token = r1.reply.rsplit("/", 1)[-1]
        await db.talents.update_one({"id": tid}, {"$set": {"media": [
            {"id": "m1", "category": "indian", "url": "https://res.cloudinary.com/demo/image/upload/sample.jpg"}]}})
        talent = await tm._talent_for_token(token)
        assert tm.build_media_view(talent)["total"] == 1
        await db.talents.update_one({"id": tid}, {"$set": {"media": []}})
        assert tm.build_media_view(await tm._talent_for_token(token))["total"] == 0
        assert (await e.ask(f"Show me Liveq{e.tag}'s media link")).reply == r1.reply
