// -------------------------------------------------------------------
// Videos filmed sideways. Some channels (Morio CH, Matsurou Ch.) upload phone
// footage as landscape files whose picture lies on its side, with no rotation
// flag a browser could read. The server's orientation pass finds them from
// where upright faces are (media_indexer "orient"), and every listing says
// how far to turn each video: f.rotate (degrees clockwise, what is shown),
// f.rotate_auto (what the pass would do) and f.rotate_set (a person chose).
// player.js turns and sizes the video (sizeTurnedVideo, video._turn); this
// file is what a person sees and clicks about it:
//   - a chip in the preview header whenever a video is shown turned or a turn
//     was set by hand: an automatic turn always comes with a button to undo
//     it (user, 2026-11-08). The chip opens the video settings menu;
//   - "Turn the picture" in that menu (the gear), for every video, so a
//     sideways one the pass missed can be turned by hand;
//   - why it is turned, in the info panel (setPreviewEnhancement 'turn').
// A turn set here is one shared answer for everybody, kept on the server
// (POST /api/media/rotation), and the thumbnails follow it.
// -------------------------------------------------------------------

const rotateRoutes = { set: '/api/media/rotation' };

const TURN_LABELS = { 0: 'as filmed', 90: '90° right', 180: 'upside down', 270: '90° left' };
const TURN_CHOICES = [['As filmed', 0], ['90° right', 90], ['90° left', 270], ['180°', 180]];
const TURN_AUTO = 'auto';

function turnLabel(deg) {
  return TURN_LABELS[((deg || 0) % 360 + 360) % 360] || `${deg}°`;
}

// The choice that is in force: TURN_AUTO while nobody has set one and the
// pass has an answer, otherwise the turn itself.
function currentTurnChoice(f) {
  if (!f.rotate_set && f.rotate_auto) return TURN_AUTO;
  return f.rotate || 0;
}

function turnChoices(f) {
  return (f.rotate_auto || f.rotate_set)
    ? [[`Automatic${f.rotate_auto ? ` (${turnLabel(f.rotate_auto)})` : ''}`, TURN_AUTO], ...TURN_CHOICES]
    : TURN_CHOICES;
}

function turnReason(f) {
  if (f.rotate_set) {
    const auto = f.rotate_auto ? `; automatic would be ${turnLabel(f.rotate_auto)}` : '';
    return `Set by hand to ${turnLabel(f.rotate)}${auto}.`;
  }
  if (f.rotate_auto) {
    return `Turned ${turnLabel(f.rotate_auto)} automatically: the picture lies on its side `
      + '(faces in it are upright only when turned).';
  }
  return 'For a video filmed sideways. The turn is kept for everybody, thumbnails too.';
}

// Called by media.js for every video it opens in the preview.
function startPreviewTurn(video, f) {
  video._turnFile = f;
  video._turn = f.rotate || 0;
  paintTurnChip(video, f);
  describeTurn(video, f);
}

function paintTurnChip(video, f) {
  const old = document.getElementById('mediaTurnChip');
  const wanted = rotateRoutes.set && (f.rotate || f.rotate_set);
  if (!wanted) {
    if (old) old.remove();
    return;
  }
  const actions = document.querySelector('#mediaPreviewModal .modal-header-actions');
  if (!actions) return;
  let chip = old;
  if (!chip) {
    chip = document.createElement('button');
    chip.type = 'button';
    chip.id = 'mediaTurnChip';
    chip.className = 'upscale-chip turn-chip';
    actions.prepend(chip);
  }
  chip.textContent = f.rotate ? `↻ ${turnLabel(f.rotate)}` : 'As filmed';
  chip.title = `${turnReason(f)}\nClick to change it.`;
  chip.onclick = (e) => {
    e.stopPropagation();
    openTurnMenu(video);
  };
}

// The chip's target: the gear menu of this video's player, where the turn is
// the first setting. Without a gear (an old cached page) the info panel,
// which carries the same buttons.
function openTurnMenu(video) {
  const wrap = video.closest('.media-player-wrap');
  const gear = wrap && wrap.querySelector('.player-settings-btn');
  if (gear) {
    if (!wrap.querySelector('.player-settings-menu')) gear.click();
    return;
  }
  if (typeof toggleInfoPanel === 'function' && !infoPanelOpen) toggleInfoPanel();
}

function describeTurn(video, f) {
  if (typeof setPreviewEnhancement !== 'function') return;
  if (!f.rotate && !f.rotate_set) {
    setPreviewEnhancement('turn', null);
    return;
  }
  const current = currentTurnChoice(f);
  setPreviewEnhancement('turn', {
    title: f.rotate ? `Shown turned ${turnLabel(f.rotate)}` : 'Shown as filmed',
    rows: [['Why', turnReason(f)]],
    actions: rotateRoutes.set ? turnChoices(f).map(([label, value]) => ({
      label,
      active: value === current,
      disabled: value === current,
      title: value === TURN_AUTO ? 'Go back to the automatic turn' : `Show this video ${turnLabel(value)}`,
      onClick: () => setTurn(video, f, value),
    })) : [],
  });
}

// "Turn the picture" in the gear menu (interp.js renderPlayerSettings).
function renderTurnSettings(menu, video, heading, note, rerender) {
  const f = video._turnFile;
  if (!f || !rotateRoutes.set || typeof segmentedChoice !== 'function') return;
  heading('Turn the picture');
  const choice = segmentedChoice(turnChoices(f), currentTurnChoice(f), (value) => {
    setTurn(video, f, value).then(rerender);
  });
  choice.classList.add('turn-choice');
  menu.appendChild(choice);
  note(turnReason(f));
}

async function setTurn(video, f, value) {
  if (value === currentTurnChoice(f)) return;
  let res;
  try {
    res = await fetch(rotateRoutes.set, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        root: f.root || (typeof currentMediaRoot === 'string' ? currentMediaRoot : ''),
        path: f.path,
        deg: value === TURN_AUTO ? null : value,
      }),
    });
  } catch (e) {
    return;
  }
  if (!res.ok) return;
  const data = await res.json();
  const before = f.rotate || 0;
  f.rotate = data.deg || 0;
  f.rotate_auto = data.auto || 0;
  f.rotate_set = data.set !== null && data.set !== undefined;
  // The listing's width and height are the shape as shown.
  if ((before % 180) !== (f.rotate % 180) && f.width && f.height) {
    [f.width, f.height] = [f.height, f.width];
  }
  refreshTurnedTiles(f);
  if (!video.isConnected) return;
  video._turn = f.rotate;
  if (typeof currentPreviewEl !== 'undefined' && currentPreviewEl === video) {
    applyMediaSizing(video, video.videoWidth || currentPreviewNativeW, video.videoHeight || currentPreviewNativeH);
  }
  paintTurnChip(video, f);
  describeTurn(video, f);
}

// The grid behind the preview: its tile gets the thumbnail made with the new
// turn, and takes that thumbnail's shape once it loads.
function refreshTurnedTiles(f) {
  if (typeof thumbnailUrlFor !== 'function') return;
  const root = f.root || (typeof currentMediaRoot === 'string' ? currentMediaRoot : '');
  for (const tile of document.querySelectorAll('.media-tile[data-kind="file"]')) {
    if (tile.dataset.path !== f.path || (tile.dataset.root || '') !== root) continue;
    const img = tile.querySelector('.media-tile-thumb img');
    if (!img) continue;
    if (typeof fitTileToThumb === 'function') {
      img.addEventListener('load', () => fitTileToThumb(tile, img), { once: true });
    }
    img.src = thumbnailUrlFor(f);
  }
}
