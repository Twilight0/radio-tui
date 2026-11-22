# Changelog

## v1.0.0 (2026-11-22)

### Features

- **Initial Release (`radio-tui`):** Single-file Python curses internet radio player with zero third-party Python dependencies.
- **Favorites & Custom URLs:** Bookmark stations (`f`) or add custom stream URLs (`a`), persisted to `~/.config/radio-tui/favorites.json`.
- **SomaFM Feed:** Fetches all 51 SomaFM channels directly from `somafm.com/channels.json` and resolves `.pls`/`.m3u` streams.
- **Radio Browser Integration:** Browse **Top World**, **Countries**, **Genres**, and **Search (`/`)** via `radio-browser.info` with automatic 100-item lazy-loading pagination when scrolling near the end of a list.
- **Client-Side List Filtering & Input Controls:** Press `l` on any tab (including Genres and Countries) to filter loaded items by substring (`Esc` clears the filter or cancels prompts).
- **Audio Output & Volume Control:** Decodes streams with `ffmpeg` (`-reconnect`) to PipeWire/PulseAudio (`paplay`) or ALSA `default` (`aplay`, compatible with `dmix` / BlueALSA), with `+`/`-` live volume adjustments via `pactl`, `bluealsa-cli`, or `amixer`.
