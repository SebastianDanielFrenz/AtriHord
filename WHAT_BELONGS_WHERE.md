# What belongs where

The rule the whole review pipeline is built on: **a profile is one person
posting one kind of thing.** A file that is a different kind of thing from the
rest of its profile is a candidate for deletion — never a certainty, and never
deleted by the machine.

This document is the specification for three things at once: what a suggestion
rule is allowed to fire on, what the training queue is asking you to judge, and
what a future classifier is being trained towards. If a rule and this document
disagree, this document is right and the rule is a bug.

---

## The two kinds of Twitter profile

### Artist profiles — drawings

**Wanted:** illustrations of characters. Finished colour art, sketches offered
as finished work, comics and 4-koma, animated art. Whatever the artist draws.

**Not wanted:**

| Not wanted | Why | Detectable today |
|---|---|---|
| Working sketches — black and white, greyscale, rough line work | The pack wants finished art, not workbench output. **Manga and 4-koma are exempt** — those are black and white by convention, not by being unfinished. | yes — `monochrome` / `greyscale` / `sketch` / `traditional_media`, minus `comic` / `4koma` |
| Photographs of anything | An artist account posting a photograph is posting something other than their art | yes — `realistic`, `photo_background`, `lips`, `depth_of_field` |
| Photographs of non-people — the desk, the con hall, the printed book, food | The most common kind of stray photo in an art feed | yes — the photo markers together with `no_humans` |
| Screenshots, memes, announcement cards, text posts | Not art | partly — `no_humans` plus `english_text` / `text_focus` catches many |

### Cosplayer profiles — photographs

**Wanted:** photographs of the cosplayer (or of other cosplayers) in costume.
Studio shots, convention shots, selfies. **Whoever is in the picture** — a
cosplayer posting a photo of a friend still belongs.

**Not wanted:**

| Not wanted | Why | Detectable today |
|---|---|---|
| Gacha and game screenshots | Not a photograph of anyone | partly — `gameplay_mechanics`, `video_game`, `fake_screenshot` exist but are thin |
| Food, drinks, scenery with nobody in them | Not the subject the profile is followed for | yes — `no_humans` with `food` |
| Merch tables, photobook covers, price lists, con-stall displays | A photograph *of printed photographs* is advertising, not a cosplay photo. **A cosplayer genuinely present at their own booth is wanted, but see below — nothing available can tell the two apart, so booths are excluded outright and you keep the good ones by hand** | yes — `photo_(object)`, `poster_(object)`, `magazine_cover`, `merchandise`, `collage`, and two or more different `*_text` tags together |
| Announcement cards, schedules, text-only images | Same reason | partly — `no_humans` plus text tags |
| Drawings and fan art | Wrong medium for the feed | yes, but weakly — the drawn markers are rare, so this fires seldom |
| Reaction memes and macros | Someone else's picture, used as a comment | yes — `meme`, which is on 172 Twitter files and exactly **1** pixiv file |

### Both kinds

Never wanted in either:

| Not wanted | Why | Detectable today |
|---|---|---|
| No subject at all — text cards, food, screenshots | Nothing to look at | yes — `no_humans` with `food` / `english_text` / `text_focus` |
| Merch tables, photobooks, posters, price lists | Advertising, not the work | yes — the merch markers, with the booth caveat below |
| **Reaction memes and macros** | Someone else's picture used as a comment. A photo of a TV actor pulling a face is not a cosplay photo and not art | yes — `meme` |
| **Official or promotional art** — game announcement cards, character sheets with a voice-actor credit, key visuals | It IS a drawing, so no drawn-vs-photo rule would ever catch it, but it is not the artist's own work. The tell is the **copyright line**: `copyright_name` appears on 1 pixiv file of 322 and `logo` on 0, while an artist's own post carries neither | yes — `copyright_name`, `logo` |

`multiple_views` is deliberately **not** a trigger: 6 of 322 pixiv files carry
it, because artists really do post multi-view sheets of their own characters.
It can support a verdict; it cannot make one.

### Pixiv

Illustration by definition. A photograph there is wrong regardless of anything
else, and this is the calibration source: 0 of 214 pixiv files carry a
photographic marker, which is what makes those markers trustworthy.

### YouTube and everything without an account

**No rule applies.** These were downloaded deliberately, one at a time, and
there is no profile for them to disagree with. The first dry run flagged 149
files here and every one was a false positive.

---

## The asymmetry that governs every rule

A photographic marker is near-proof of a photograph: 0.0% of 214 known pixiv
illustrations carry one, against 32.9% of Twitter files.

**The reverse is not true.** Across 125 Twitter profiles the share of
photographic files is a broad hump centred near 0.5, not two clean peaks —
plenty of genuine cosplay photos earn no marker at all. So:

> **Suggestions fire on evidence being PRESENT, never on evidence being
> absent.** "This has no photo marker" is not a reason to propose deleting
> anything.

Flagging absence would have queued 222 of 842 files in photo-shaped profiles —
a quarter of them. That is not a review queue, it is a second library.

The same asymmetry governs the People grid: a face is hidden as "drawn" only
when its files are judged drawn, never when they are simply unjudged.

---

## What the training queue is asking

**One question: does this file look like the rest of its account?** The strip
of small pictures under each tile is other files from the same account, so
there is nothing to look up and no need to know in advance whether an account
is an artist or a cosplayer.

It is **not** "is this the same person" — a cosplayer posting a photo of
someone else fits. It is **not** "is this any good". It is **not** "do I want
to keep it" — that is `Keep anyway` in the review queue.

If the strip is too mixed to tell, **?** is the right answer. It is recorded
and left out of the numbers.

---

## Status of each rule

| Rule | Reason string | Built |
|---|---|---|
| Photograph in a drawn-shaped profile | `photo in a drawn profile` | yes |
| Photograph on pixiv | `photo on pixiv` | yes |
| Drawing in a photo-shaped profile | `drawing in a photo profile` | yes |
| Nobody in the picture (food, text) | `no subject` | yes |
| Sketch or greyscale in an art profile | `sketch in an art profile` | yes |
| Photograph of no-one in an art profile | `photo of no-one in an art profile` | yes |
| Merch table, photobook, poster, price list | `merchandise, not a photo of a person` | yes |
| Game or gacha screenshot | `screenshot in a photo profile` | yes, weakly |
| Reaction meme or macro | `reaction meme` | yes |
| Official / promotional art with a credit line | `official or promotional art` | yes |

"Weakly" means the vocabulary barely supports it: `gameplay_mechanics` appears
on 106 files and `video_game` on 18, so a screenshot with characters and no UI
furniture will be missed. This is exactly what the training labels are for —
a rule learned from the tag vector will catch what hand-picked markers cannot.


---

## Measured output of these rules

Dry run over the whole tagged library, 2026-09-08 — **436 suggestions from
10,387 tagged files, 4.2%**:

| Reason | Count |
|---|---|
| merchandise, not a photo of a person | 152 |
| no subject | 120 |
| reaction meme | 88 |
| official or promotional art | 28 |
| screenshot in a photo profile | 23 |
| photo in a drawn profile | 11 |
| sketch in an art profile | 10 |
| photo of no-one in an art profile | 2 |
| photo on pixiv | 1 |
| drawing in a photo profile | 1 |

A queue that can be worked through, not a second library. Every rule was
checked against pixiv before being written: a marker that shows up there is a
marker that lies.


---

## Two corrections, both found by looking at what the rules flagged

### `cosplay` is not a photographic marker

On danbooru `cosplay` means "character A wearing character B's outfit" and is
applied to **drawings** constantly — 277 of the 1,054 files carrying it have no
other photographic marker at all. It was in the photo-marker set, and it
announced a pixiv illustration of Lumine as a photograph. Removed from the
marker set, from the profile-shape query, and from the face realness test.

### An art profile now needs evidence of being drawn

`@RooOOooOOppl` is fifteen mirror selfies of a cosplayer. The tagger gave
fourteen of them no photographic marker — the documented asymmetry — so the
profile measured 0.067 photographic and was declared an **art** account. The
one photo that did earn a marker was then flagged "a photo in a drawn profile".

A mis-shaped profile makes every kind-gated rule wrong for that whole account,
and it happens exactly where the evidence is weakest. The cause was inferring
`anime` from the **absence** of photo markers — the one inference this project
has repeatedly proved invalid.

Both shapes now require their own evidence:

| Shape | Requires |
|---|---|
| `cosplayer` | ≥ 35% of files carry a photographic marker |
| `anime` | ≤ 15% photographic **and** ≥ 40% carry a drawn marker |
| `mixed` | anything else — **no kind-gated rule fires** |

Finding a drawn marker took care. Comparing pixiv (all drawings) with
`realistic` Twitter files (all photographs) puts nudity tags at the top —
`explicit` 51% vs 0.2%, `censored` 46% vs 0.2% — but those separate this NSFW
art from this clothed cosplay by **subject**, not medium, and would misread a
clothed illustration. `blush` is the genuine medium marker: **59.1% of drawings
against 0.2% of photographs**, because a pink cheek is something an artist
draws. The set is `blush`, `sketch`, `monochrome`, `greyscale`,
`traditional_media`, `comic`, `4koma`, `chibi`.

### Result

`@RooOOooOOppl` → **mixed**, its selfie no longer flagged. The pixiv drawing no
longer flagged. Profile shapes across the library: **168 cosplayer, 34 anime,
53 mixed**, 528 with too few tagged files to judge.

| Reason | before | after |
|---|---|---|
| merchandise (now needs `no_humans`) | 152 | 11 |
| photo in a drawn profile | 11 | 3 |
| no subject | 120 | 177 |
| screenshot in a photo profile | 23 | 119 |
| reaction meme | 88 | 101 |
| official or promotional art | 28 | 38 |
| sketch in an art profile | 10 | 9 |
| drawing in a photo profile | 1 | 6 |

Totals moved from 436/10,387 to **464/11,976 (3.9%)** — the rises are the tag
pass covering 1,600 more files, the falls are the corrections.


---

## The booth problem, and why it is not solved

**Wanted:** a cosplayer standing at their own booth, in person.
**Not wanted:** a photograph of the merchandise on that booth.

Three gates were measured against the 282 files that carry merch markers and
have somebody in frame:

| Gate | Result |
|---|---|
| `no_humans` below 0.5 | kept **all 282** — a *hand* holding polaroids counts as a human, so the commonest unwanted shot walked straight through |
| a person tag (`1girl`, `solo`, …) | **275 of 282** carry one — the printed photos are *of people*, so `1girl` fires on the print |
| a face detected by InsightFace | **210 of 282** — the detector finds the faces printed on the prints too |

To every signal available, the subject and the merchandise are the same thing:
a picture of a person. Distinguishing them needs something none of these models
provide — depth, or the knowledge that a rectangle within the frame is itself a
photograph.

So **booths are excluded outright**, per the instruction to do that if it
proved too hard. The cost is that a genuine at-the-booth photo lands in the
queue; the remedy is **Keep anyway**, which records as a *correct catch* rather
than a false positive, so it does not poison the precision figures.

Final dry run: **614 suggestions from 12,056 tagged files (5.1%)**.


---

## Rules mined from the labels (2026-09-08, 672 answered)

With the dataset confirmed, the 88 missed files were compared against the 532
correctly left alone. Three changes came out of it, each measured rather than
guessed:

| Change | Evidence |
|---|---|
| **`no_humans` alone is enough** — the rule used to demand a second marker (food, text) | `no_humans` is on **22.7%** of files you rejected and **0.2%** of files you kept — a lift of 120, and the single largest blind spot at 20 of 88 misses |
| **A meme needs a caption** — `meme` plus `english_text` / `text_focus` / `subtitled` / a speech bubble | `meme` alone scored **0.29**: five of seven were ordinary cosplay photos of the poster that WD14 called a meme at up to 0.97. No threshold separates those; a caption does |
| **`chibi` alone flags a drawing in a photo profile** — one marker, not two | `chibi` is on **12.5%** of misses and 1.5% of keeps. A chibi drawing in a feed of photographs is not ambiguous the way a lone `monochrome` is |

The old "a landscape in a cosplayer's feed is still theirs" reasoning was
wrong, and the labels are what proved it.

### Scored against the labels

| | before | after |
|---|---|---|
| Precision | 0.750 | **0.868** |
| Recall | 0.307 | **0.465** |
| Caught correctly | 39 | 59 |
| Flagged but you kept | 13 | 9 |
| Missed | 88 | 68 |

Per rule after the change:

| Rule | Right | Wrong | Precision |
|---|---|---|---|
| no subject | 30 | 0 | 1.00 |
| screenshot in a photo profile | 9 | 0 | 1.00 |
| drawing in a photo profile | 7 | 0 | 1.00 |
| photo in a drawn profile | 1 | 0 | 1.00 |
| official or promotional art | 3 | 1 | 0.75 |
| merchandise | 8 | 7 | 0.53 |
| reaction meme | 1 | 1 | — |

Excluding the merchandise/booth trade-off, precision is **0.96**.

**These numbers are training-set numbers.** The rules were derived from the
labels they are scored on, so they flatter themselves. The honest test is fresh
answers: label another few dozen files and score again without changing
anything. Until then, treat 0.87 / 0.47 as an upper bound.

Queue over the library: **833 of 13,542 tagged files (6.2%)**.
