"""Which downloaded files do not belong to the profile they came from.

The idea the user asked for: a twitter account is one person posting one kind
of thing, so an account marked `cosplayer` should not be full of illustrations
and an account marked `anime` should not be full of photographs. Anything that
disagrees with its own profile is a candidate for deletion.

WHAT THE EVIDENCE ACTUALLY SUPPORTS
-----------------------------------
The classifier is the WD14 tagger's own vocabulary; there is no separate model.
Calibrating it against pixiv - which the user states never contains photographs
or cosplay, and which is therefore ground truth for "drawn" - gives:

    marker             twitter   youtube   pixiv (n=214)
    realistic           32.9%     21.2%     0.0%
    photo_background     9.3%     14.9%     0.0%
    lips                 8.9%      3.3%     0.0%
    cosplay             10.3%      4.0%     0.5%

So a photographic marker at p>=0.5 is a near-zero-false-positive detector of
"this is a photograph": 0 of 214 known illustrations carry one.

The reverse does NOT hold. Across 125 twitter profiles with enough tagged
files, the share of photographic files is a broad hump centred near 0.5, not
two clean peaks - plenty of real cosplay photos never earn a marker. Flagging
"no photographic marker" inside photo-shaped profiles would have queued 222 of
842 files, a quarter of them, which is not a review queue but a second library.

Therefore this module only ever flags on POSITIVE evidence:

  * a photograph inside a drawn-shaped profile        (strong: see above)
  * a drawing inside a photo-shaped profile           (rare but positive)
  * a file with no person in it at all - food, text cards, screenshots
    (positive markers, and 0% of pixiv carries them)

The absence of a marker is never, by itself, a reason to suggest deleting
something. That asymmetry is the whole design.

NOTHING HERE DELETES ANYTHING. The sweep writes rows with state 'pending' and
stops. A human decides in the review queue, and only then does the ordinary
trash-and-ban path run.

The one exception is AUTO_TRASH_REASONS: rules the user has explicitly said to
trust without review ("no subject" and "screenshot in a photo profile", since
2026-09-11). server.py trashes
their pending hits after each scan - still through the trash-and-ban path, so
every file stays restorable, and without writing labels.
"""
from __future__ import annotations

import json
import re
import time

import media_ai

# --------------------------------------------------------------------------
# The vocabulary, and what it means
# --------------------------------------------------------------------------

# A photograph. Every one of these is 0.0% on pixiv.
# NOT cosplay. On danbooru `cosplay` means "character A wearing character B's
# outfit" and is applied to DRAWINGS constantly - 277 of 1054 files carrying it
# have no other photographic marker at all. It cost a false positive on a pixiv
# illustration of Lumine, which the rule announced as a photograph. It is a
# costume marker, not a camera one.
PHOTO_MARKERS = {
    "realistic": 1.0,
    "photo_background": 0.9,
    "lips": 0.7,
    "depth_of_field": 0.5,
}

# A drawing. Weak individually - these are ~1% of anything - so two are wanted
# before this counts as evidence.
DRAWN_MARKERS = {
    "comic": 0.8,
    "monochrome": 0.6,
    "sketch": 1.0,
    "traditional_media": 1.0,
    "greyscale": 0.4,
}

# Not a person at all. `no_humans` alone is not enough: a landscape shot in a
# cosplayer's feed is still theirs. Paired with food or text it is a snack
# photo or a screenshot of an announcement.
NO_SUBJECT_MARKERS = ("food", "english_text", "comic", "text_focus",
                      "still_life", "animal", "scenery")

# Unfinished work. An artist's black-and-white sketch is workbench output, not
# what the feed is followed for - but manga and 4-koma are black and white by
# convention, so they are excluded rather than caught.
SKETCH_MARKERS = ("monochrome", "greyscale", "sketch", "traditional_media")
MANGA_MARKERS = ("comic", "4koma")

# A photograph OF printed photographs: a convention stall, a photobook cover, a
# price list, a poster wall. Advertising rather than a cosplay photo, and wrong
# in an art feed too.
MERCH_MARKERS = ("photo_(object)", "poster_(object)", "poster_(medium)",
                 "magazine_cover", "magazine_(object)", "merchandise",
                 "collage", "album_cover")
# NOT multiple_views: 6 of 322 pixiv files carry it, because artists really do
# post multi-view sheets of their own characters. It supports a verdict, it
# cannot make one.

# Official or promotional art: a company credit line, a game logo, a voice
# actor billing. The tell is the copyright, which is what an artist's own work
# does not have - copyright_name is on 1 pixiv file of 322, and logo on 0.
PROMO_MARKERS = ("copyright_name", "logo")

# A game screenshot. Thin - `gameplay_mechanics` is on 106 files and
# `video_game` on 18 - so this catches the ones with UI furniture and misses a
# clean character render. The training labels are what will fix that.
SCREENSHOT_MARKERS = ("gameplay_mechanics", "video_game", "fake_screenshot",
                      "fake_phone_screenshot")

# A reaction image or macro. `meme` is on 172 twitter files and exactly 1 pixiv
# file, which is the same near-zero false positive rate that makes the photo
# markers usable.
MEME_MARKERS = ("meme",)
# ...but `meme` alone is not enough. It scored 0.29 against the labels: five
# of seven were ordinary cosplay photos of the poster herself that WD14 called
# a meme at up to 0.97, so no threshold separates them. What a reaction image
# actually has is a CAPTION, and none of those five did.
MEME_CAPTION_MARKERS = ("english_text", "text_focus", "subtitled", "speech_bubble",
                        "chinese_text", "korean_text")
# A like-goal / strip-game post ("every 1,000 likes...") is the artist's OWN
# drawing with counters written on it, not a reaction image. WD14 names the
# format, and the user marked both such files as misfires.
STRIP_GAME_MARKERS = ("twitter_strip_game_(meme)", "like_and_retweet")
# What makes a meme a GENERIC reaction image - a photographed person or an
# animal. An account exempt from the meme rule posts game-render and anime
# memes on purpose, but its generic memes are still unwanted (user,
# 2026-09-11, about @anon16509170: "keep the anime and/or game ones and
# discard the rest").
# NOT photo_background or lips: 3D game renders carry both, and with them in
# the list 13 of that account's 17 still-flagged files were game renders. On
# the 79 files classified by eye this list flags 14 of 20 generic memes and 0
# of 59 renders; a CGI film still and a cartoon clip slip through as kept.
PHOTOGRAPHIC_MEME_MARKERS = ("realistic", "photorealistic", "cat", "animal", "animal_focus")

# Positive evidence that a picture was DRAWN. `blush` carries this set: 59.1%
# of 401 pixiv drawings against 0.2% of 2199 photographs, because a pink cheek
# is a thing an artist draws rather than a thing a camera sees. The others are
# the explicit hand-made vocabulary. Nudity tags separate the two corpora far
# more sharply and are deliberately NOT here: they describe subject matter, and
# a clothed illustration would fail them.
DRAWN_PROFILE_MARKERS = ("blush", "sketch", "monochrome", "greyscale",
                         "traditional_media", "comic", "4koma", "chibi")

MIN_MARKER_P = 0.5          # below this the tagger is guessing
STRONG_MARKER_P = 0.6       # what a suggestion needs
MIN_PROFILE_FILES = 8       # a shape needs enough files to be a shape
PHOTO_PROFILE_SHARE = 0.35  # photographic share at or above: a photo profile
ANIME_PROFILE_SHARE = 0.15  # photographic share must be at or below this...
DRAWN_PROFILE_SHARE = 0.40  # ...AND this much of it must look hand-drawn

PROFILE_PATH = re.compile(r"^(?P<source>[^/]+)/(?P<handle>@[^/]+)/")

KIND_PHOTO = "cosplayer"
KIND_ANIME = "anime"
KIND_MIXED = "mixed"


def profile_of(path: str):
    """(source, handle) for a path that belongs to an account, else None.

    Only sources that file by account have profiles - twitter writes
    `twitter/@handle/...`. pixiv files by numeric member id and is handled by
    its own rule, not by this one.
    """
    m = PROFILE_PATH.match(path or "")
    return (m.group("source"), m.group("handle")) if m else None


# --------------------------------------------------------------------------
# Reading the tags of one file
# --------------------------------------------------------------------------

def _tags_of(conn, media_id):
    return {r["tag"]: r["p"] or 0.0 for r in conn.execute(
        "SELECT tag, p FROM media_tags WHERE media_id = ?", (media_id,))}


def photo_evidence(tags: dict):
    """(score, [markers]) - how strongly this looks like a photograph."""
    hits = [(t, tags[t]) for t in PHOTO_MARKERS if tags.get(t, 0) >= MIN_MARKER_P]
    if not hits:
        return 0.0, []
    score = max(p * PHOTO_MARKERS[t] for t, p in hits)
    return score, [t for t, _ in sorted(hits, key=lambda h: -h[1])]


def drawn_evidence(tags: dict):
    """(score, [markers]) - how strongly this looks drawn.

    Deliberately harder to satisfy than photo_evidence: these markers are rare
    and individually unconvincing, so one of them is not enough.
    """
    hits = [(t, tags[t]) for t in DRAWN_MARKERS if tags.get(t, 0) >= MIN_MARKER_P]
    if len(hits) < 2:
        return 0.0, []
    score = max(p * DRAWN_MARKERS[t] for t, p in hits)
    return score, [t for t, _ in sorted(hits, key=lambda h: -h[1])]


def _any_marker(tags: dict, markers, threshold=MIN_MARKER_P):
    """(score, [markers]) for the strongest of a set that is present."""
    hits = [(t, tags[t]) for t in markers if tags.get(t, 0) >= threshold]
    if not hits:
        return 0.0, []
    hits.sort(key=lambda h: -h[1])
    return hits[0][1], [t for t, _ in hits]


# Cues that a picture with a person as its subject was photographed. Weak as
# photo DETECTORS (see PHOTO_MARKERS for why cosplay is not one), but here
# they only have to say "not a drawing" about a picture that already has a
# person in it.
PERSON_PHOTO_CUES = ("realistic", "photorealistic", "photo_background", "lips",
                     "cosplay", "selfie", "asian")


def person_with_decor(tags: dict) -> bool:
    """A photographed person with printed goods only in the background.

    The merch rule's commonest misfire - 14 of its first 19 'wrong' answers:
    a cosplayer in their room or at a shop, posters on the wall, figures on a
    shelf. Unlike a booth (see judge), this one is separable: either the only
    strong merch marker is a poster and there is one strong subject, or it is
    a selfie / mirror selfie. It must also look photographic, so a drawing
    with a poster in it stays flagged - the user called those toss-ups.
    """
    strong = {m for m in MERCH_MARKERS if tags.get(m, 0) >= STRONG_MARKER_P}
    decor = strong <= {"poster_(object)"} and max(
        tags.get("solo", 0), tags.get("1girl", 0), tags.get("1boy", 0)) >= 0.85
    selfie = tags.get("selfie", 0) >= MIN_MARKER_P or (
        tags.get("holding_phone", 0) >= MIN_MARKER_P
        and max(tags.get("mirror", 0), tags.get("reflection", 0)) >= MIN_MARKER_P)
    photo = max(tags.get(t, 0) for t in PERSON_PHOTO_CUES) >= MIN_MARKER_P
    return (decor or selfie) and photo


def drawn_on_photo_background(tags: dict) -> bool:
    """A drawing composited onto a photographed background.

    photo_background is a photo marker because the background IS a photo; when
    it is the only one (no realistic, no lips) and the picture carries drawn
    cues, the subject is drawn. The user marked the example a misfire.
    """
    return (tags.get("photo_background", 0) >= MIN_MARKER_P
            and max(tags.get("realistic", 0), tags.get("lips", 0)) < MIN_MARKER_P
            and max(tags.get(t, 0) for t in ("spot_color", "speech_bubble", "blush",
                                              "english_text")) >= MIN_MARKER_P)


def sketch_evidence(tags: dict):
    """Black and white, and not a comic. See SKETCH_MARKERS."""
    if _any_marker(tags, MANGA_MARKERS)[0]:
        return 0.0, []          # manga is meant to be black and white
    return _any_marker(tags, SKETCH_MARKERS)


# Somebody is in the picture. Used to VETO the no-subject rule rather than to
# support anything, for videos: merge_video unions the tags of four sampled
# frames, so one frame of a chopping board and one of the cosplayer produce
# `no_humans` 0.99 AND `1girl` 1.00 on the same record. A still cannot say
# both, so this only bites where the union happens.
PERSON_MARKERS = ("1girl", "1boy", "2girls", "multiple_girls", "multiple_boys",
                  "solo", "solo_focus")


def no_subject_evidence(tags: dict):
    """(score, [markers]) - nobody is in this picture.

    `no_humans` ALONE is enough, and the labels are what settled that. This
    rule used to demand a second marker - food, text - on the theory that a
    landscape in a cosplayer's feed is still theirs. The user's own answers
    disagree: `no_humans` is on 22.7% of the files they said do not fit and
    on 0.2% of the files they kept, a lift of 120. It was the single largest
    blind spot, 20 of 88 misses.

    The supporting markers still ride along in the detail line, because "no
    humans, food" reads better in a queue than "no humans".
    """
    if tags.get("no_humans", 0) < MIN_MARKER_P:
        return 0.0, []
    if _any_marker(tags, PERSON_MARKERS)[0]:
        return 0.0, []          # some frame of this had a person in it
    hits = [(t, tags[t]) for t in NO_SUBJECT_MARKERS if tags.get(t, 0) >= MIN_MARKER_P]
    return tags["no_humans"], ["no_humans"] + [t for t, _ in hits]


# --------------------------------------------------------------------------
# Profile shapes
# --------------------------------------------------------------------------

def profile_shapes(conn, min_files=MIN_PROFILE_FILES):
    """{(source, handle): {...}} for every account with enough tagged files.

    The shape is measured from the files themselves rather than declared, so
    an unmarked profile still gets a suggestion when it is obviously one kind
    of thing. A mark the user has set overrides what the files say - they know
    which account is which and the tagger does not.
    """
    marks, exempt = {}, {}
    for r in conn.execute("SELECT source, handle, kind, exempt FROM profiles"):
        who = (r["source"], r["handle"])
        marks[who] = r["kind"]
        exempt[who] = parse_exempt(r["exempt"])
    drawn_list = ",".join("'%s'" % t for t in DRAWN_PROFILE_MARKERS)
    seen = {}
    for r in conn.execute(
            "SELECT m.id AS id, m.path AS path,"
            " (SELECT MAX(t.p) FROM media_tags t WHERE t.media_id = m.id"
            "  AND t.tag IN ('realistic','photo_background','lips',"
            "                'depth_of_field')) AS photo,"
            f" (SELECT MAX(t.p) FROM media_tags t WHERE t.media_id = m.id"
            f"  AND t.tag IN ({drawn_list})) AS drawn"
            " FROM media m WHERE m.tags IS NOT NULL AND m.tags != ''"
            "   AND m.trashed IS NULL"):
        who = profile_of(r["path"])
        if not who:
            continue
        entry = seen.setdefault(who, {"files": 0, "photo": 0, "drawn": 0})
        entry["files"] += 1
        if (r["photo"] or 0) >= MIN_MARKER_P:
            entry["photo"] += 1
        if (r["drawn"] or 0) >= MIN_MARKER_P:
            entry["drawn"] += 1

    shapes = {}
    for who, e in seen.items():
        share = e["photo"] / e["files"]
        drawn = e["drawn"] / e["files"]
        if e["files"] >= min_files:
            # BOTH shapes need evidence of their own. "Not photographic" is
            # not evidence of being drawn - that inference is what declared a
            # cosplayer's fifteen selfies an art account.
            if share >= PHOTO_PROFILE_SHARE:
                measured = KIND_PHOTO
            elif share <= ANIME_PROFILE_SHARE and drawn >= DRAWN_PROFILE_SHARE:
                measured = KIND_ANIME
            else:
                measured = KIND_MIXED
        else:
            measured = None
        shapes[who] = {
            "source": who[0], "handle": who[1],
            "files": e["files"], "photo_share": round(share, 3),
            "drawn_share": round(drawn, 3),
            "measured": measured, "mark": marks.get(who),
            "kind": marks.get(who) or measured,
            "exempt": sorted(exempt.get(who) or ()),
        }
    return shapes


# --------------------------------------------------------------------------
# The sweep
# --------------------------------------------------------------------------

# What an account can be excused from. The key is stable; the wording of the
# reason it suppresses is not, so the two are mapped explicitly rather than
# matched on the reason string.
# Rules trusted to act without a person looking, at the user's explicit
# request (2026-09-11: "I am confident in the no subject findings. You may
# auto-trash hits of that rule."). The evidence at that point: 2,803 review
# discards against 2 misfires, and a visual audit that found nobody in 153
# random discards. Every other rule stays propose-only. Auto-trashed files go
# through the ordinary trash + ban path and stay restorable until a trash
# batch is purged by hand.
# Added the same day on the same basis: "screenshot in a photo profile also
# looks ready for prod". Its record: 410 discards, 1 misfire (a cosplay video
# with a game HUD, since excluded by the overlay guard in judge), and the only
# keep outside the artefact batch was a BeReal holding the owner's photo - the
# user discarded 39 of 40 such screenshots.
AUTO_TRASH_REASONS = ("no subject", "screenshot in a photo profile")

# Every rule judge() can name. The settings page offers exactly these as rules
# that may trash without review; the defaults are AUTO_TRASH_REASONS above.
RULES = ("no subject", "screenshot in a photo profile", "reaction meme",
         "official or promotional art", "photo in a drawn profile",
         "sketch in an art profile", "drawing in a photo profile", "photo on pixiv")

EXEMPT_SKETCH = "sketch"
EXEMPT_MEME = "meme"
EXEMPT_SCREENSHOT = "screenshot"
EXEMPT_PROMO = "promo"
EXEMPT_KEYS = (EXEMPT_SKETCH, EXEMPT_MEME, EXEMPT_SCREENSHOT, EXEMPT_PROMO)
EXEMPT_LABELS = {
    EXEMPT_SKETCH: "rough black-and-white work is this artist's output",
    EXEMPT_MEME: "this account posts anime and game-render memes on purpose"
                 " (photo memes are still flagged)",
    EXEMPT_SCREENSHOT: "this account posts game captures on purpose",
    EXEMPT_PROMO: "this account posts official art on purpose",
}


def parse_exempt(raw):
    """'sketch,meme' -> {'sketch', 'meme'}, ignoring anything unrecognised."""
    if not raw:
        return set()
    return {p.strip() for p in str(raw).split(",")
            if p.strip() in EXEMPT_KEYS}


def judge(path: str, tags: dict, kind: str, exempt=()):
    """(reason, detail, score) if this file does not belong, else None.

    `kind` is what the profile is expected to contain. Every branch fires on a
    marker being PRESENT - see the module docstring for why the absence of one
    is never enough.

    `exempt` is the set of rules this account is excused from - see
    EXEMPT_KEYS. Some artists publish rough black-and-white work deliberately;
    some accounts exist to post captioned game-render memes. In each case the
    picture is exactly what the rule fires on and exactly what the account is
    for, and no tag separates "posted on purpose" from "escaped". Only a
    person knows, so it is recorded per account rather than inferred.
    """
    exempt = exempt if isinstance(exempt, (set, frozenset)) else set(exempt or ())
    # Only where an expectation exists. A youtube video with no people and
    # some on-screen text is a video the user chose to download, not a
    # mistake - the first dry run flagged 149 files and most were exactly
    # that. This whole feature is about a file disagreeing with the profile
    # it came from, so no profile means nothing to disagree with.
    if kind or path.startswith("pixiv/"):
        # A reaction image is not what any of these accounts are for, and the
        # marker is as clean as the photographic ones - so it goes first.
        score, markers = _any_marker(tags, MEME_MARKERS)
        caption, cap_markers = _any_marker(tags, MEME_CAPTION_MARKERS)
        photographic, _ = _any_marker(tags, PHOTOGRAPHIC_MEME_MARKERS)
        strip_game, _ = _any_marker(tags, STRIP_GAME_MARKERS)
        if ((EXEMPT_MEME not in exempt or photographic)
                and not strip_game
                and score >= STRONG_MARKER_P and caption >= MIN_MARKER_P):
            return ("reaction meme",
                    "a meme with a caption (%s)" % ", ".join(cap_markers[:2]), score)

        # Official or promotional art - a game announcement card, a character
        # sheet with a voice-actor credit. It IS a drawing, so no drawn/photo
        # rule would ever catch it, and it is not the artist's own work.
        score, markers = _any_marker(tags, PROMO_MARKERS)
        if EXEMPT_PROMO not in exempt and score >= STRONG_MARKER_P:
            return ("official or promotional art",
                    "carries a company credit (%s)" % ", ".join(markers[:2]), score)

        # A photograph OF printed photographs - a stall, a photobook, a price
        # list. Wrong in either kind of profile, so it is not gated on kind.
        #
        # A cosplayer standing at their own booth is wanted, but nothing here
        # can tell that from a table of prints. Three gates were measured on
        # the 282 files with merch markers and somebody in frame:
        #
        #   no_humans < 0.5     kept all 282 - a HAND holding polaroids counts
        #                       as a human, so the commonest unwanted shot
        #                       walked straight through
        #   a person tag        275 of 282 carry one - the printed photos are
        #                       OF people, so `1girl` fires on the print
        #   a detected face     210 of 282 - the detector finds the faces on
        #                       the prints too
        #
        # The subject and the merchandise are the same thing to every signal
        # available. So booths are excluded outright, which is what the user
        # asked for if it proved too hard: the ones where they really are
        # present get Keep-anyway in the review queue, and that now records as
        # a correct catch rather than a false positive.
        score, markers = _any_marker(tags, MERCH_MARKERS)
        if score >= STRONG_MARKER_P and not person_with_decor(tags):
            return ("merchandise, not a photo of a person",
                    "printed goods on display (%s)" % ", ".join(markers[:3]), score)

        score, markers = no_subject_evidence(tags)
        if score >= STRONG_MARKER_P:
            return ("no subject",
                    "nobody in the picture (%s)" % ", ".join(markers[:3]), score)

    if kind == KIND_ANIME:
        score, markers = photo_evidence(tags)
        if score >= STRONG_MARKER_P and not drawn_on_photo_background(tags):
            # Said separately when there is nobody in it: a photograph of the
            # desk or the con hall is the commonest stray in an art feed, and
            # naming it that way makes the queue quicker to work through.
            if tags.get("no_humans", 0) >= MIN_MARKER_P:
                return ("photo of no-one in an art profile",
                        "photographic, nobody in it (%s)" % ", ".join(markers[:2]),
                        score)
            return ("photo in a drawn profile",
                    "photographic (%s)" % ", ".join(markers[:3]), score)

        if EXEMPT_SKETCH not in exempt:
            score, markers = sketch_evidence(tags)
            if score >= STRONG_MARKER_P:
                return ("sketch in an art profile",
                        "black and white, not a comic (%s)" % ", ".join(markers[:3]),
                        score)

    if kind == KIND_PHOTO:
        # One marker, not two: `chibi` is on 12.5% of the missed files and 1.5%
        # of the kept ones, and a chibi drawing in a feed of photographs is not
        # ambiguous the way a lone `monochrome` is.
        # ...unless it is a sticker ON a photo: chibi_inset / reference_inset
        # mean a small drawing pasted onto the picture. The user marked all
        # three such photos as misfires, and the nine discarded ones were the
        # same thing slipping past.
        if (tags.get("chibi", 0) >= STRONG_MARKER_P
                and max(tags.get("chibi_inset", 0),
                        tags.get("reference_inset", 0)) < MIN_MARKER_P):
            return ("drawing in a photo profile", "a chibi drawing",
                    tags["chibi"])

        score, markers = _any_marker(tags, SCREENSHOT_MARKERS)
        # A cosplay video with a game HUD edited over it fires through
        # gameplay_mechanics while the tagger does not think it is a screenshot
        # at all. The one such file was a misfire; nothing discarded matched.
        overlay_on_cosplay = tags.get("cosplay", 0) >= 0.9 and max(
            tags.get("fake_screenshot", 0),
            tags.get("fake_phone_screenshot", 0)) < STRONG_MARKER_P
        if (EXEMPT_SCREENSHOT not in exempt and score >= STRONG_MARKER_P
                and not overlay_on_cosplay):
            return ("screenshot in a photo profile",
                    "game or app capture (%s)" % ", ".join(markers[:3]), score)

        score, markers = drawn_evidence(tags)
        if score >= STRONG_MARKER_P:
            return ("drawing in a photo profile",
                    "drawn (%s)" % ", ".join(markers[:3]), score)

    # pixiv is illustration by definition - the user's own rule - so a
    # photograph there is wrong regardless of any profile shape.
    if path.startswith("pixiv/"):
        score, markers = photo_evidence(tags)
        if score >= STRONG_MARKER_P:
            return ("photo on pixiv",
                    "photographic (%s)" % ", ".join(markers[:3]), score)

    return None


def sweep(conn, limit=None, progress=None):
    """Look at every tagged file and record what does not fit. Returns counts.

    Only ever writes rows with state 'pending'. A file already decided on is
    left alone - re-suggesting something the user kept would make the queue
    impossible to finish.
    """
    shapes = profile_shapes(conn)
    decided = {r["media_id"] for r in conn.execute(
        "SELECT media_id FROM suggestions WHERE state != 'pending'")}

    sql = ("SELECT id, path FROM media"
           " WHERE tags IS NOT NULL AND tags != '' AND trashed IS NULL")
    if limit:
        sql += " LIMIT %d" % int(limit)

    found, looked = 0, 0
    now = time.time()
    for row in conn.execute(sql).fetchall():
        looked += 1
        if progress and looked % 500 == 0:
            progress(looked, found)
        if row["id"] in decided:
            continue
        who = profile_of(row["path"])
        shape = (shapes.get(who) or {}) if who else {}
        kind = shape.get("kind")
        verdict = judge(row["path"], _tags_of(conn, row["id"]), kind,
                        exempt=set(shape.get("exempt") or ()))
        if not verdict:
            continue
        reason, detail, score = verdict
        conn.execute(
            "INSERT INTO suggestions (media_id, reason, detail, score, state,"
            " created_at) VALUES (?, ?, ?, ?, 'pending', ?)"
            " ON CONFLICT(media_id) DO UPDATE SET reason=excluded.reason,"
            " detail=excluded.detail, score=excluded.score,"
            " created_at=excluded.created_at WHERE suggestions.state='pending'",
            (row["id"], reason, detail, score, now))
        found += 1
    conn.commit()
    return {"looked": looked, "suggested": found,
            "profiles": sum(1 for s in shapes.values() if s["kind"])}


# --------------------------------------------------------------------------
# Labels
# --------------------------------------------------------------------------

VERDICT_BELONGS = "belongs"
VERDICT_NOT = "not_belongs"
VERDICT_UNSURE = "unsure"


def record_label(conn, media_id, path, root, queue, verdict, acted=None,
                 by=None, shapes=None):
    """Write down one judgement, with everything it needs to outlive the file.

    The tag vector is snapshotted rather than referenced: a re-sweep can
    change a file's tags, and a training example whose features moved after
    the fact is worse than no example.
    """
    tags = _tags_of(conn, media_id) if media_id else {}
    who = profile_of(path)
    kind = None
    if who and shapes is not None:
        kind = (shapes.get(who) or {}).get("kind")
    row = conn.execute("SELECT reason, score FROM suggestions WHERE media_id = ?",
                       (media_id,)).fetchone() if media_id else None
    conn.execute(
        "INSERT INTO labels (queue, root, path, media_id, profile, profile_kind,"
        " reason, score, flagged, verdict, acted, tags, decided_at, decided_by)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (queue, root, path, media_id, who[1] if who else None, kind,
         row["reason"] if row else None, row["score"] if row else None,
         1 if row else 0, verdict, acted, json.dumps(tags, sort_keys=True),
         time.time(), by))
    conn.commit()


def sample_for_training(conn, limit=24):
    """Random tagged files from the population the rules scan.

    NOT filtered by whether the rules flagged them - filtering would throw
    away the flagged half and make recall unmeasurable, which is the one
    number this queue exists to produce. Only files already labelled are
    excluded, so the same judgement is not asked for twice.
    """
    return conn.execute(
        "SELECT m.id AS id, m.root AS root, m.path AS path"
        " FROM media m"
        " WHERE m.tags IS NOT NULL AND m.tags != '' AND m.trashed IS NULL"
        "   AND m.path LIKE '%/@%/%'"
        "   AND NOT EXISTS (SELECT 1 FROM labels l WHERE l.path = m.path)"
        " ORDER BY RANDOM() LIMIT ?", (limit,)).fetchall()


def label_stats(conn):
    """What the labels say about the rules.

    Two precisions, and they are not the same number:

      review_precision - over the review queue, where every file was flagged,
        so "was the flag correct" is exactly what a verdict answers. This is
        the one that accumulates quickly, because that queue is worked.
      precision / recall - over the blind random sample only. Recall CANNOT
        come from the review queue: it never shows an unflagged file, so a
        recall computed there is always 1.0 and always meaningless.

    A file kept anyway counts as a CORRECT flag in both. The rule spotted a
    mismatch; the user simply wants the file. Scoring that as a miss would
    train the next classifier to stop finding them.
    """
    out = {"review": {}, "training": {}, "precision": None, "recall": None,
           "precision_n": 0, "recall_n": 0}
    for r in conn.execute(
            "SELECT queue, verdict, COUNT(*) AS n FROM labels GROUP BY queue, verdict"):
        out.setdefault(r["queue"], {})[r["verdict"]] = r["n"]

    # Judged with TODAY'S rules on the tag snapshot stored with each label,
    # NOT with the `flagged` column. That column is frozen at the moment the
    # answer was given, so every label written before a rule existed says
    # "not flagged" for ever - which reported precision 1.00 and recall 0.07
    # when the truth was 0.75 and 0.31. The column stays as history; it is no
    # longer what the numbers are built on.
    shapes = profile_shapes(conn)
    t = {}
    for row in conn.execute("SELECT path, verdict, tags FROM labels"
                            " WHERE queue = 'training' AND verdict != 'unsure'"):
        try:
            snapshot = json.loads(row["tags"] or "{}")
        except (ValueError, TypeError):
            snapshot = {}
        who = profile_of(row["path"])
        kind = (shapes.get(who) or {}).get("kind") if who else None
        key = ("f" if judge(row["path"], snapshot, kind) else "u")             + ("n" if row["verdict"] == VERDICT_NOT else "b")
        t[key] = t.get(key, 0) + 1
    # The review queue's own precision: every file there was flagged, so the
    # verdict alone says whether the flag was right.
    r = {row["verdict"]: row["n"] for row in conn.execute(
        "SELECT verdict, COUNT(*) AS n FROM labels"
        " WHERE queue = 'review' AND verdict != 'unsure' GROUP BY verdict")}
    right, wrong = r.get("not_belongs", 0), r.get("belongs", 0)
    if right + wrong:
        out["review_precision"] = round(right / (right + wrong), 3)
        out["review_precision_n"] = right + wrong
    else:
        out["review_precision"] = None
        out["review_precision_n"] = 0
    out["kept_anyway"] = conn.execute(
        "SELECT COUNT(*) AS n FROM labels WHERE queue='review'"
        " AND verdict='not_belongs' AND acted='kept'").fetchone()["n"]

    tp, fp, fn = t.get("fn", 0), t.get("fb", 0), t.get("un", 0)
    if tp + fp:
        out["precision"] = round(tp / (tp + fp), 3)
        out["precision_n"] = tp + fp
    if tp + fn:
        out["recall"] = round(tp / (tp + fn), 3)
        out["recall_n"] = tp + fn
    out["confusion"] = {"flagged_and_wrong": tp, "flagged_but_fine": fp,
                        "missed": fn, "correctly_left_alone": t.get("ub", 0)}
    return out


# --------------------------------------------------------------------------
# Finding likely slips in the labels themselves
# --------------------------------------------------------------------------
#
# There is no ground truth to check labels against - if there were, none of
# this would be needed. What there IS is internal consistency: the same picture
# answered two ways, two near-identical pictures answered two ways, or a single
# odd answer in an account answered consistently otherwise.
#
# Deliberately NOT "the rules disagree with you". The rules are the thing being
# measured; using them to grade the labels would make the measurement circular
# and would quietly train the labels towards the rules' own blind spots.

SUSPECT_SIMILARITY = 0.92     # cosine over the tag vector
SUSPECT_MIN_ACCOUNT = 5       # labels in an account before an odd one out counts


def _vec(row):
    try:
        return json.loads(row["tags"] or "{}")
    except (ValueError, TypeError):
        return {}


def _cosine(a, b):
    keys = set(a) & set(b)
    if not keys:
        return 0.0
    dot = sum(a[k] * b[k] for k in keys)
    na = sum(v * v for v in a.values()) ** 0.5
    nb = sum(v * v for v in b.values()) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def _post_of(path):
    """The tweet a file came from: everything files as <post id>_<n>.<ext>."""
    name = path.rsplit("/", 1)[-1]
    stem = name.rsplit(".", 1)[0]
    return (path.rsplit("/", 1)[0], stem.rsplit("_", 1)[0]) if "_" in stem else None


def suspect_labels(conn, limit=60):
    """Labels worth a second look, most suspicious first.

    Each entry says what makes it suspicious and names the label it disagrees
    with, so the pair can be shown together - one of the two is wrong and only
    a person can say which.
    """
    rows = [dict(r) for r in conn.execute(
        "SELECT id, path, profile, verdict, tags, decided_at FROM labels"
        " WHERE verdict != 'unsure' ORDER BY id")]
    for r in rows:
        r["vec"] = _vec(r)

    out = []
    seen_pairs = set()

    # 1. The same post, answered both ways. Images in one tweet are almost
    #    always the same kind of thing, so this is the strongest signal.
    by_post = {}
    for r in rows:
        key = _post_of(r["path"])
        if key:
            by_post.setdefault(key, []).append(r)
    for key, group in by_post.items():
        verdicts = {g["verdict"] for g in group}
        if len(verdicts) > 1:
            minority = min(verdicts, key=lambda v: sum(1 for g in group if g["verdict"] == v))
            for g in group:
                if g["verdict"] == minority:
                    other = next(x for x in group if x["verdict"] != minority)
                    out.append({"label_id": g["id"], "path": g["path"],
                                "verdict": g["verdict"], "profile": g["profile"],
                                "why": "same post, answered both ways",
                                "against_path": other["path"],
                                "against_verdict": other["verdict"],
                                "weight": 1.0})

    # 2. Near-identical pictures answered differently.
    for i, a in enumerate(rows):
        for b in rows[i + 1:]:
            if a["verdict"] == b["verdict"]:
                continue
            pair = (a["id"], b["id"])
            if pair in seen_pairs:
                continue
            sim = _cosine(a["vec"], b["vec"])
            if sim < SUSPECT_SIMILARITY:
                continue
            seen_pairs.add(pair)
            for one, two in ((a, b), (b, a)):
                out.append({"label_id": one["id"], "path": one["path"],
                            "verdict": one["verdict"], "profile": one["profile"],
                            "why": "nearly identical to a file you answered the other way",
                            "against_path": two["path"],
                            "against_verdict": two["verdict"],
                            "weight": round(sim, 3)})

    # 3. The odd answer out in an account answered consistently otherwise.
    by_profile = {}
    for r in rows:
        if r["profile"]:
            by_profile.setdefault(r["profile"], []).append(r)
    for handle, group in by_profile.items():
        if len(group) < SUSPECT_MIN_ACCOUNT:
            continue
        counts = {}
        for g in group:
            counts[g["verdict"]] = counts.get(g["verdict"], 0) + 1
        if len(counts) < 2:
            continue
        minority = min(counts, key=counts.get)
        if counts[minority] != 1:
            continue                     # two of a kind is a pattern, not a slip
        odd = next(g for g in group if g["verdict"] == minority)
        other = next(g for g in group if g["verdict"] != minority)
        out.append({"label_id": odd["id"], "path": odd["path"],
                    "verdict": odd["verdict"], "profile": odd["profile"],
                    "why": "the only file in %s you answered this way (%d others the other way)"
                           % (handle, len(group) - 1),
                    "against_path": other["path"],
                    "against_verdict": other["verdict"],
                    "weight": 0.5 + min(0.4, len(group) / 50.0)})

    best = {}
    for o in out:
        if o["label_id"] not in best or o["weight"] > best[o["label_id"]]["weight"]:
            best[o["label_id"]] = o
    return sorted(best.values(), key=lambda o: -o["weight"])[:limit]
