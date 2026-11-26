# radio-tui

Lightweight single-file Python curses internet radio player with zero Python third-party dependencies.

## Features

- **Multiple Feeds:**
  - **Favorites:** Bookmark any station or add custom stream URLs (`~/.config/radio-tui/favorites.json`).
  - **SomaFM:** All SomaFM channels via `somafm.com/channels.json`.
  - **Top World, Countries, Genres, Search:** Powered by the Radio Browser API (`radio-browser.info`) with lazy-loading pagination.
  - **Recent:** Stations you have played, remembered in `~/.config/radio-tui/recent.json` (most recent first).
- **Client-Side Filtering:** Filter loaded lists by substring (`l`) and clear with `Esc`.
- **Audio Output & Mixing:** Decodes streams with `ffmpeg` and plays via PulseAudio/PipeWire (`paplay`) or ALSA `default` (`aplay`, compatible with `dmix` and BlueALSA).
- **Legacy SHOUTcast Support:** Stations on old SHOUTcast servers (which answer with `ICY 200 OK` instead of an HTTP status line, rejected by ffmpeg 9.1+) are retried through `curl --http0.9`.
- **YouTube Playback (optional):** YouTube links added as custom URLs are resolved with `yt-dlp` and the best available AAC audio is played.
- **Volume Control:** Adjust volume directly with `+`/`-` (`pactl`, `bluealsa-cli`, or `amixer`).

## Requirements

- Python 3.8+
- `ffmpeg`
- `aplay` (`alsa-utils`) or `paplay` (PulseAudio / PipeWire)
- Optional: `pactl`, `bluealsa-cli`, or `amixer` for volume control
- Optional: `curl` for legacy SHOUTcast streams (fallback for ffmpeg 9.1+)
- Optional: `yt-dlp` for playing YouTube URLs

## Installation

```sh
pip install .
```

Or run directly:

```sh
python3 radio_tui.py
```

## Keybindings

| Key | Action |
| --- | --- |
| `Up` / `Down`, `PgUp` / `PgDn` | Navigate stations / lists |
| `Enter` | Play station or open selected country / genre |
| `s` | Stop playback |
| `+` / `-` | Volume up / down |
| `f` | Toggle favorite |
| `a` | Add custom stream URL to Favorites (YouTube links are played via `yt-dlp`) |
| `l` | Filter current list (`Esc` to clear) |
| `/` | Search stations by name |
| `c` | Jump to country (or browse all if blank) |
| `g` | Jump to genre/tag (or browse all if blank) |
| `1` - `7`, `Tab` | Switch tabs (`6` opens the search prompt, `7` shows recent stations) |
| `Esc` / `Backspace` | Return from country/genre station list |
| `q` | Quit (stops playback) |

Playback is always tied to the app: closing the terminal tab, quitting or
killing radio-tui also stops the stream (no orphaned `ffmpeg`/`paplay`).
