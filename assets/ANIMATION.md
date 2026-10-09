# pgtriage launch animation

The supplied v6 source is `pgtriage_demo.html` (1280 × 720). The corresponding MP4 and GIF exports are generated from that source. `pgtriage_demo_square.html` recomposes the same animation at 1080 × 1080 with responsive playback controls. Serve assets over HTTP to preview; for example, run `python3 -m http.server 8765` from the repository root and open `/assets/pgtriage_demo_square.html`.

## Consistency contract

The canonical first script block and all three scene markup blocks are identical in both HTML files. The square adds CSS and a separate adapter after the canonical script. That adapter changes connector orientation, particle coordinates and expanded-panel height only; all data, IDs, caption text, easing and timing remain unchanged. Both expose `window.seek(t)` for capture and review. The square also has Play, Restart, Safety, SQL fix, Close and scrub controls.

- Total loop: 16 seconds.
- Findings arrive: 6.4–7.0 seconds.
- Rows enter at 8.2, 8.6, 9.0 and 9.35 seconds.
- Fix expands: 9.6–10.2 seconds.
- SQL types: 10.2–11.3 seconds; full text remains until fading starts at 12.6 seconds.
- Endcard enters at 13.0 seconds; loop fade starts at 15.3 seconds.

V6 uses a demonstration dataset: 283 tables, 147 issues, including 1 critical, 19 medium, 64 low and 63 more across seven categories. These are separate from the README's real-audit totals. The featured `unused_index` finding and `DROP INDEX CONCURRENTLY` suggestion match current detector output. CRITICAL connection pressure, MEDIUM duplicate indexes and LOW unused indexes match current detector severities.

## Fonts and scope

The six referenced Fontsource 5.3.0 WOFF2 files are bundled locally. Each package's license is beside its font files. No external font request is required. Square changes do not modify detector behavior.
