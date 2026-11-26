# Changelog

## v1.2.0 (2026-11-26)

### Features

- **Legacy SHOUTcast (ICY) streams play again:** ffmpeg 9.1 made its HTTP status-line parser strict, so servers that answer with the legacy `ICY 200 OK` line (Radio Caprice and friends) failed with *"Malformed HTTP status line"*. radio-tui now detects that failure and retries through `curl --http0.9`, which accepts the old response; the player bar shows `(curl)`.
- **YouTube support via yt-dlp (optional):** manually entered YouTube links are resolved with `yt-dlp` and the best AAC audio (`bestaudio[ext=m4a]`, highest bitrate first) is streamed to the player. The player bar shows `(yt-dlp)`.

### Fixes

- The stderr of the player pipeline is drained continuously, so a chatty stream can no longer fill the pipe buffer and stall ffmpeg.
- Errors from `yt-dlp` (for example *"Video unavailable"*) are shown instead of ffmpeg's generic message.

## v1.1.1 (2026-11-26)

### Fixes

- **Search prompt closed on the first keystroke:** `curses.KEY_TAB` does not exist in Python's curses module, so typing the first character raised `AttributeError` and the prompt was dismissed (the character was lost and the next keystroke was handled as a command). The tab key is now matched by its numeric code.

### Features

- **Recent stations tab (`7`):** stations you have played are remembered in `~/.config/radio-tui/recent.json` (most recent first, up to 50 entries) and listed in a new `Recent` tab. The list survives restarts and re-playing a station moves it back to the top.

## v1.1.0 (2026-11-26)

### Fixes

- **Playback now always stops with the app:** `ffmpeg`/`paplay` are spawned with `PR_SET_PDEATHSIG`, so the audio pipeline dies with radio-tui even if it is `SIGKILL`ed; `SIGHUP`/`SIGTERM`/`SIGINT` are handled so closing a terminal tab no longer leaves an orphaned stream playing, and an `atexit` hook plus an explicit kill of the child processes back up the normal quit path.
- **`s` (stop) can no longer miss:** the player now tracks the `ffmpeg`/`paplay` PIDs and terminates them directly, and the parent performs the `setpgid` itself, so a stop issued in the milliseconds right after playback starts no longer kills an empty process group and leaves the radio playing.
- **Escape sequences no longer leak into keybindings:** arrow/function keys pressed while an input prompt is open used to cancel the prompt and then trigger random commands (e.g. `[A` -> `a` -> "add station" prompt). Pending input is now drained after `Esc`, and special keys are ignored inside prompts.
- **More robust prompts:** `curs_set` failures are tolerated, `KEY_ENTER`/backspace/navigation keys behave consistently, and `Esc` reliably cancels a prompt.
- **`Esc` in the main view** clears an active filter or leaves a country/genre listing instead of doing nothing.

## v1.0.0 (2026-11-22)

### Features

- **Initial Release (`radio-tui`):** Single-file Python curses internet radio player with zero third-party Python dependencies.
- **Favorites & Custom URLs:** Bookmark stations (`f`) or add custom stream URLs (`a`), persisted to `~/.config/radio-tui/favorites.json`.
- **SomaFM Feed:** Fetches all 51 SomaFM channels directly from `somafm.com/channels.json` and resolves `.pls`/`.m3u` streams.
- **Radio Browser Integration:** Browse **Top World**, **Countries**, **Genres**, and **Search (`/`)** via `radio-browser.info` with automatic 100-item lazy-loading pagination when scrolling near the end of a list.
- **Client-Side List Filtering & Input Controls:** Press `l` on any tab (including Genres and Countries) to filter loaded items by substring (`Esc` clears the filter or cancels prompts).
- **Audio Output & Volume Control:** Decodes streams with `ffmpeg` (`-reconnect`) to PipeWire/PulseAudio (`paplay`) or ALSA `default` (`aplay`, compatible with `dmix` / BlueALSA), with `+`/`-` live volume adjustments via `pactl`, `bluealsa-cli`, or `amixer`.
