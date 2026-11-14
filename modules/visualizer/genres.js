// Which scenes suit which music. The music page tells the visualizer the song's style (its strongest Discogs style
// from the AI genre index, e.g. "Electro House", "Dubstep", "Soundtrack"); this file sorts it into one of five
// families, and every scene carries the families it suits. In "By genre" the conductor plays only the scenes of the
// current song's family, and the set changes when a song of another family starts.
//   VisGenres.family('Tech Trance') -> 'trance'      (null: no style or an unknown one -> every scene)
//   VisGenres.pool('trance', names)  -> the names that suit it
//   VisGenres.FAMILIES[f]            -> { label, slow }   slow: changes come after twice as many phrases
(function () {
  'use strict';
  const FAMILIES = {
    club: { label: 'Club', slow: false },        // house, dance pop, disco, pop
    bass: { label: 'Bass', slow: false },        // dubstep, drum and bass, trap, hip hop
    trance: { label: 'Trance', slow: false },    // trance, techno, hardstyle, hard dance
    calm: { label: 'Calm', slow: true },         // ambient, soundtrack, classical, folk, chill
    dark: { label: 'Dark', slow: false },        // industrial, metal, rock, experimental, noise
  };

  // Style names (lower case) first; then the patterns, in order, for the hundreds of other Discogs styles.
  const STYLES = {
    bassline: 'bass', 'uk garage': 'club', electro: 'club', 'big beat': 'bass', breaks: 'bass', breakbeat: 'bass',
    synthwave: 'trance', 'hands up': 'trance', 'hard house': 'trance', 'disco polo': 'club', chiptune: 'club',
    vaporwave: 'calm', 'dungeon synth': 'calm', 'modern classical': 'calm', neofolk: 'calm', instrumental: 'bass',
    'dj battle tool': 'bass', glitch: 'dark', breakcore: 'dark', 'rhythmic noise': 'dark', experimental: 'dark',
    ballad: 'calm', 'symphonic rock': 'dark', 'trip hop': 'calm', 'lo-fi': 'calm', 'new age': 'calm',
  };
  const PATTERNS = [
    [/trance|techno|hardstyle|hardcore|hard ?dance|gabber|goa|schranz/, 'trance'],
    [/dubstep|drum n bass|jungle|halftime|trap|rap|grime|hip ?hop|gangsta|horrorcore|footwork|bass music|drill|phonk/, 'bass'],
    [/industrial|metal|rock|punk|noise|ebm|darkwave|goth|grunge|doom|drone/, 'dark'],
    [/ambient|soundtrack|score|chill|downtempo|classical|medieval|baroque|choral|opera|folk|nordic|acoustic|jazz|lounge|piano|contemporary|minimal|easy listening/, 'calm'],
    [/house|disco|dance|pop|garage|funk|soul|reggaeton|latin|dancehall|eurobeat|edm|electro/, 'club'],
  ];

  function family(style) {
    if (!style) return null;
    const s = String(style).split('---').pop().trim().toLowerCase();
    if (STYLES[s]) return STYLES[s];
    for (const [re, f] of PATTERNS) if (re.test(s)) return f;
    return null;
  }

  // Our scenes and the curated presets, by the families they suit (looked at with music, 2026-11-14).
  const SUITS = {
    'AtriHord - Ring Tunnel': ['trance', 'club', 'bass'],
    'AtriHord - Star Gate': ['trance', 'calm'],
    'AtriHord - Mandala Bloom': ['calm', 'club'],
    'AtriHord - Pulse Field': ['club', 'bass', 'dark'],
    '_Mig_004_version2': ['calm', 'trance'],                                   // dotted mandala
    '_Mig_028': ['club', 'trance'],                                            // rainbow streaks
    '_Mig_079': ['dark', 'calm'],                                              // grey smoke
    '_Rovastar + Geiss - Hurricane Nightmare (Posterize Mix)': ['dark', 'calm'],  // dark eye swirl
    '$$$ Royal - Mashup (197)': ['club', 'bass', 'trance'],                   // neon lasers
    '$$$ Royal - Mashup (220)': ['bass', 'club'],                             // fiery wings
    '$$$ Royal - Mashup (253)': ['dark', 'bass'],
    '$$$ Royal - Mashup (273)': ['dark', 'bass', 'trance'],                   // black and white kaleidoscope
    '_Krash + Eo.S. - Photographic Sentinel': ['dark', 'bass'],               // glowing creature
    'Flexi + Martin - cascading decay swing': ['bass', 'dark'],               // glitch cascade
    'A Tribute to Martin - bombyx mori - Ft Flexi - AdamFX - StahlRegen - Krash - Rovastar -  Hd in Milk T': ['trance', 'calm'],  // star tunnel
    'Adam Eatit Mashup FX 2 martin - disco mix + Lodus + Geiss + Ludicrous speed + Aderrasi 2_1': ['club'],  // disco stars
    'Cope - Passage (mandala mix)': ['trance', 'club'],                       // kaleidoscope
    'cope - the drain to heaven': ['calm'],                                   // soft flower
  };

  // The scenes of a family; a scene not in SUITS (a new preset) fits every family. Fewer than `min` -> all of them.
  function pool(fam, names, min) {
    if (!fam) return names.slice();
    const out = names.filter((n) => !SUITS[n] || SUITS[n].includes(fam));
    return out.length >= (min == null ? 3 : min) ? out : names.slice();
  }

  window.VisGenres = { FAMILIES, family, pool, SUITS };
})();
