# pgtriage launch animation

The supplied v5 source is `pgtriage_demo.html` (1280 × 720). Its MP4 and GIF exports are included unchanged. `pgtriage_demo_square.html` recomposes the same animation at 1080 × 1080 with responsive playback controls. Serve assets over HTTP to preview; for example, run `python3 -m http.server 8765` from the repository root and open `/assets/pgtriage_demo_square.html`.

## Consistency contract

The canonical first script block and all three scene markup blocks are identical in both HTML files. The square adds CSS and a separate adapter after the canonical script. That adapter changes connector orientation, particle coordinates and expanded-panel height only; all data, IDs, caption text, easing and timing remain unchanged. Both expose `window.seek(t)` for capture and review. The square also has Play, Restart, Safety, SQL fix, Close and scrub controls.

- Total loop: 16 seconds.
- Findings arrive: 6.4–7.0 seconds.
- Rows enter at 8.2, 8.6, 9.0 and 9.35 seconds.
- Fix expands: 9.6–10.2 seconds.
- SQL types: 10.2–11.3 seconds; full text remains until fading starts at 12.6 seconds.
- Endcard enters at 13.0 seconds; loop fade starts at 15.3 seconds.

V5 uses illustrative SAMPLE AUDIT data: 283 tables, 147 issues, including 1 high, 19 medium, 64 low and 63 more across seven categories. These are not the README's real-audit totals. The orders DROP statement is an illustrative advisory suggestion, not a live output or executed command. HIGH connection pressure, MEDIUM duplicate indexes and LOW unused indexes match current detector severities. Current duplicate-index detector output provides index definitions and review comments rather than this exact DROP statement.

## Fonts and scope

The six referenced Fontsource 5.3.0 WOFF2 files are bundled locally. Each package's license is beside its font files. No external font request is required. Square changes do not modify detector behavior. Square MP4/GIF exports are not included yet.
