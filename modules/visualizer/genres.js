// Which scenes suit which music. The music page tells the visualizer the song's style (its strongest Discogs style
// from the AI genre index, e.g. "Electro House", "Dubstep", "Soundtrack"); this file sorts it into one of 16 genre
// groups, and each group lists the scenes that suit it (our own AtriHord scenes, reused across groups, and MilkDrop
// presets picked for it by watching them with music). In "By genre" the conductor plays only the current song's group,
// and the set changes when a song of another group starts.
//   VisGenres.family('Tech Trance') -> 'trance'      (null: no style or an unknown one -> every scene)
//   VisGenres.pool('trance', names)  -> the names that suit it (a scene in no group suits every group)
//   VisGenres.FAMILIES[f]            -> { label, slow, scenes }   slow: changes come after twice as many phrases
(function () {
  'use strict';
  const FAMILIES = {
    house: { label: "House", slow: false,   // house, deep / tropical / progressive house, nu-disco, garage
      scenes: [
        "AtriHord - Ring Tunnel",
        "AtriHord - Mandala Bloom",
        "AtriHord - Pulse Field",
        "_Mig_085",
        "Adam Eatit Mashup FX 2 martin - disco mix + Lodus + Geiss + Ludicrous speed + Aderrasi 2_1",
        "Geiss - Desert Rose 4",
        "martin - move this body",
        "martin - another kind of groove",
        "GreatWho - Lasershow"] },
    electro: { label: "Electro", slow: false,   // electro house, electro, big room, bassline, big beat
      scenes: [
        "AtriHord - Ring Tunnel",
        "AtriHord - Pulse Field",
        "$$$ Royal - Mashup (197)",
        "444",
        "fishbrain + flexi - stitchcraft",
        "martin - glass corridor",
        "suksma - water cooled red uranium vs dotes - freeenergynow.net",
        "ORB - Fire and Fumes 2"] },
    dubstep: { label: "Dubstep", slow: false,   // dubstep, halftime, riddim, future bass
      scenes: [
        "AtriHord - Pulse Field",
        "$$$ Royal - Mashup (220)",
        "$$$ Royal - Mashup (253)",
        "_Krash + Eo.S. - Photographic Sentinel",
        "Flexi + Martin - cascading decay swing",
        "Flexi - wild at range",
        "Flexi - mindblob mix",
        "shifter - dark tides bdrv mix",
        "Flexi - alien fish pond"] },
    dnb: { label: "Drum & Bass", slow: false,   // drum and bass, jungle, breaks
      scenes: [
        "AtriHord - Ring Tunnel",
        "AtriHord - Star Gate",
        "Rovastar - Trippy Sperm (Jelly)",
        "flexi - hyperspaceflight (bn cn Jelly 4)",
        "martin - tunnel race",
        "Adam Eatit Mashup FX 2 martin - disco mix + Lodus + Geiss + Ludicrous speed + Baked Ft another AdamFX Mashup 7_1",
        "Flexi + Martin - astral projection",
        "Flexi - reality tunnel"] },
    hiphop: { label: "Hip hop", slow: false,   // trap, cloud rap, grime, phonk, hip hop
      scenes: [
        "AtriHord - Pulse Field",
        "Flexi + fiShbRaiN - operation fatcap II",
        "LuxXx - Subtle HipHopFlake",
        "Milk Artist At our Best - FED - SlowFast Ft AdamFX n Martin - HD CosmoFX",
        "PieturP - triptrap_(ultimate-trip-mix)",
        "Flexi, fishbrain, Geiss + Martin - tokamak witchery"] },
    pop: { label: "Pop", slow: false,   // k-pop, j-pop, dance-pop, synth-pop, reggaeton, ballads
      scenes: [
        "AtriHord - Mandala Bloom",
        "_Mig_COLORFUL9",
        "martin - baby one more time",
        "martin - bombyx mori",
        "suksma - Rovastar - Sunflower Passion (Enlightment Mix)_Phat_edit + flexi und martin shaders - circumflex in character classes in regular expression",
        "Eo.S. + Zylot - skylight (Stained Glass Majesty mix)",
        "ORB - Depth Charge 2",
        "An AdamFX n Martin Infusion 2 flexi - Why The Sky Looks Diffrent Today - AdamFx n Martin Infusion - Tack Tile Disfunction B",
        "_Mig_014_version2",
        "Martin - cool morning"] },
    trance: { label: "Trance", slow: false,   // trance, tech / progressive trance, hands up
      scenes: [
        "AtriHord - Ring Tunnel",
        "AtriHord - Star Gate",
        "A Tribute to Martin - bombyx mori - Ft Flexi - AdamFX - StahlRegen - Krash - Rovastar -  Hd in Milk T",
        "_Mig_028",
        "158",
        "shifter - feathers (angel wings)_phat_remix relief 2",
        "Rovastar + Loadus + Geiss - FractalDrop (Triple Mix)",
        "Martin - journey into space",
        "martin, flexi, fishbrain + sto - enterstate [random mashup]",
        "Flexi + Martin - tunnel of supraschismatika",
        "martin - skywards"] },
    psy: { label: "Psytrance", slow: false,   // psytrance, goa
      scenes: [
        "AtriHord - Mandala Bloom",
        "Cope - Passage (mandala mix)",
        "_Mig_004_version2",
        "Rovastar-altarsofmadness(forgottenrea",
        "Martin N AdamFX Infusion = Phat+Yin+Eo.S_Mandala Chaser Ft AdamFX n Martin - The Beast Mandala Chaser FX H",
        "EVET - RGB Singularity",
        "Aderrasi - Curse of the Mirror Emu",
        "$$$ Royal - Mashup (160)",
        "sawtooth grin roam",
        "orb - toxic goo"] },
    techno: { label: "Techno", slow: false,   // techno, minimal, acid
      scenes: [
        "AtriHord - Ring Tunnel",
        "AtriHord - Pulse Field",
        "Eo.S. + Phat - cubetrace - v2",
        "Geiss - Myriad Mosaics",
        "Martin - acid wiring",
        "Ishan - Anuera",
        "Martin - charisma",
        "ORB - Waaa",
        "ORB - Sandblade",
        "Zylot - True Visionary (Final Mix)",
        "Stahlregen & Boz + Eo.S + Geiss + Phat + Rovastar + Zylot - Machine Code [Jelly]"] },
    hard: { label: "Hard dance", slow: false,   // hardstyle, hardcore, happy hardcore, hard house, gabber
      scenes: [
        "AtriHord - Ring Tunnel",
        "11",
        "Geiss - 3 layers (Tunnel Mix)",
        "Flexi - blame hexcollie twice",
        "martin - bring up the big guns",
        "martin - sparky caleidoscope",
        "yin - 253 - Artificial poles of the continuum (remix #3)",
        "martin + stahlregen - martin in da mash 12",
        "Aderrasi - Storm of the Eye (Thunder) - mash0000 - quasi pseudo meta concentrics"] },
    ambient: { label: "Ambient", slow: true,   // ambient, chillwave, downtempo, lo-fi, trip hop
      scenes: [
        "AtriHord - Star Gate",
        "AtriHord - Mandala Bloom",
        "cope - the drain to heaven",
        "$$$ Royal - Mashup (431)",
        "ORB - Blue Emotion",
        "Geiss - Tokamak Plus 2",
        "martin - castle in the air",
        "martin - infinity (2010 update)",
        "martin - stormy sea (2010 update)",
        "martin + stahlregen - martin in da mash 14",
        "Aderrasi - Songflower (Moss Posy)",
        "cope - soar (v2.0)"] },
    cinematic: { label: "Cinematic", slow: true,   // soundtrack, score, classical, choral
      scenes: [
        "AtriHord - Star Gate",
        "_Rovastar + Geiss - Hurricane Nightmare (Posterize Mix)",
        "_Vovan + Geiss - Bass With Flover (Feedback Mix) 2",
        "martin - angel flight",
        "martin - crystal alley",
        "martin - dark galaxy",
        "martin - The Bridge of Khazad-Dum",
        "Flexi + Rovastar - Fractopia [lovecraft]",
        "suksma - nip tuck",
        "flexi - borderline imagery",
        "martin + stahlregen - martin in da mash 12a",
        "ech0 - liquid firesticks I"] },
    folk: { label: "Folk", slow: true,   // folk, medieval, nordic, neofolk, dungeon synth
      scenes: [
        "AtriHord - Mandala Bloom",
        "_Geiss - untitled",
        "martin - sunset over the river",
        "TonyMilkdrop - Magellan's Nebula [Flexi - fancy + $this shall not retain]",
        "stahlregen & geiss + rovastar - fields of flowers (mashup 9 - space flower rmx) - mash0001 - pack em in, we got a long haul + flashlight",
        "Eo.S. + Geiss - glowsticks v2 02 (Relief Mix)",
        "flexi - inter state"] },
    synthwave: { label: "Synthwave", slow: false,   // synthwave, vaporwave, chiptune, outrun
      scenes: [
        "AtriHord - Ring Tunnel",
        "Fumbling_Foo & Flexi, Martin, Orb, Unchained - Star Nova v7b",
        "Krash - War Machine (Shifting Complexity Mix)",
        "martin - reflections on black tiles",
        "martin [shadow harlequins shape code] - fata morgana",
        "Phat + Zylot + Eo.S. - work with lines",
        "Rovastar - Torrid Tales",
        "yin - 190 - Temporal fluctuations",
        "yin - 191 - Temporal singularities",
        "Unchained - Rewop",
        "Unchained - Fuzzy Sciences",
        "Eo.S. + Phat - Emergent factors",
        "martin - disco mix 4"] },
    rock: { label: "Rock & Metal", slow: false,   // rock, metal, punk
      scenes: [
        "AtriHord - Pulse Field",
        "Stahlregen + martin + others - Psychedelic Metal Flower",
        "Cope - The Neverending Explosion of Red Liquid Fire",
        "Flexi, martin + geiss - painterly rogue wave strike",
        "martin - attack of the beast",
        "Zylot - Star Ornament",
        "ORB - Magma Pool",
        "TonyMilkdrop - Magellan's Nebula [Flexi - you enter first + multiverse]"] },
    industrial: { label: "Industrial", slow: false,   // industrial, experimental, glitch, breakcore, noise
      scenes: [
        "AtriHord - Pulse Field",
        "_Mig_079",
        "$$$ Royal - Mashup (273)",
        "flexi - what is the matrix",
        "Flexi - alien fader",
        "Aderrasi - Veil of Steel (Steel Storm) - mash0000 - bob ross finally loses it",
        "_Geiss - Artifact 01",
        "Rovastar - Explosive Minds",
        "Unchained & Rovastar - Wormhole Pillars (Hall of Shadows mix)",
        "$$$ Royal - Mashup (441)",
        "stahlregen + geiss + shifter - babylon",
        "Flexi - the distant point between derivative"] },
  };

  // Style names (lower case) that the patterns would place wrongly; then the patterns, IN ORDER (the first match
  // wins: "Electro House" is electro, not house; "Dance-pop" is pop; "Dark Ambient" is industrial).
  const STYLES = {
    instrumental: 'hiphop', 'dj battle tool': 'hiphop', 'hands up': 'trance', 'hard trance': 'hard',
    'dark ambient': 'industrial', 'symphonic rock': 'rock', 'disco polo': 'pop', bassline: 'electro',
  };
  const PATTERNS = [
    [/psy|goa/, 'psy'],
    [/hardstyle|hardcore|gabber|hard ?dance|hard house|jumpstyle|speedcore|frenchcore/, 'hard'],
    [/trance|eurodance/, 'trance'],
    [/techno|minimal|acid|tech house/, 'techno'],
    [/drum n bass|drum and bass|jungle|breakbeat|breaks|neurofunk|liquid funk/, 'dnb'],
    [/dubstep|halftime|riddim|brostep|future bass|glitch hop|bass music/, 'dubstep'],
    [/synthwave|vaporwave|chiptune|outrun|retrowave|italo|future funk/, 'synthwave'],
    [/industrial|experimental|glitch|breakcore|noise|ebm|darkwave|idm|witch house/, 'industrial'],
    [/hip ?hop|rap|trap|grime|drill|phonk|gangsta|horrorcore|boom bap|turntablism/, 'hiphop'],
    [/rock|metal|punk|grunge|emo|shoegaze/, 'rock'],
    [/soundtrack|score|classical|baroque|romantic|opera|choral|orchestral|contemporary|musical|video game/, 'cinematic'],
    [/folk|medieval|nordic|celtic|dungeon synth|country|bluegrass|world|acoustic|renaissance|pagan|tribal/, 'folk'],
    [/ambient|chill|downtempo|lounge|lo-?fi|trip hop|new age|easy listening|jazz|bossa|piano/, 'ambient'],
    [/electro|big room|big beat|fidget|complextro|moombah/, 'electro'],
    [/pop|ballad|reggaeton|latin|dancehall|schlager|funk|soul|r&b|eurobeat/, 'pop'],
    [/house|disco|garage|dance|edm/, 'house'],
  ];

  function family(style) {
    if (!style) return null;
    const s = String(style).split('---').pop().trim().toLowerCase();
    if (STYLES[s]) return STYLES[s];
    for (const [re, f] of PATTERNS) if (re.test(s)) return f;
    return null;
  }

  const LISTED = new Set();
  for (const f of Object.values(FAMILIES)) for (const n of f.scenes) LISTED.add(n);

  // The scenes of a group, in the order given; a scene in no group (a new preset) suits every group.
  // Fewer than `min` (default 3) -> all of them.
  function pool(fam, names, min) {
    if (!fam || !FAMILIES[fam]) return names.slice();
    const want = FAMILIES[fam].scenes;
    const out = names.filter((n) => !LISTED.has(n) || want.includes(n));
    return out.length >= (min == null ? 3 : min) ? out : names.slice();
  }

  window.VisGenres = { FAMILIES, family, pool };
})();
