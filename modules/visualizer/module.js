// Manifest of the optional "visualizer" module. The music page loads this file first: if it is missing (the module is
// not installed) the page simply shows no Visualizer entry. Otherwise the page loads `files` in order (relative to this
// folder, each with ?v=<version>) and calls window[entry].open(host).
// Everything the module needs lives in this folder, so installing or removing it is copying or deleting the folder.
window.AtriModules = window.AtriModules || {};
window.AtriModules.visualizer = {
  id: 'visualizer',
  name: 'Visualizer',
  version: '1.3.2',
  description: 'Full-screen music visualizer: the AtriHord scene set and curated MilkDrop presets (Butterchurn), chosen by genre.',
  files: [
    'vendor/butterchurn.min.js',   // MilkDrop in WebGL 2, MIT (THIRD_PARTY.md)
    'design.js',                   // the colour roles and schemes ("AtriHord Spectrum")
    'scenes.js',                   // our own presets
    'conductor.js',                // when scenes change
    'strands.js',                  // audio features and cover palette (and the P0 strand renderer)
    'presets-curated.js',          // the picked MilkDrop presets
    'presets-genres.js',           // more presets, picked per genre group
    'preset-tweaks.js',            // our edits to some of those presets
    'genres.js',                   // which scenes suit which genre
    'visualizer.js',               // the full-screen view
  ],
  entry: 'AtriVisualizer',
};
