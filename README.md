# decky-QAM

A [Decky Loader](https://decky.xyz/) plugin that **remaps the Steam Deck Quick
Access Menu (QAM) to a back grip button** (L4 / L5 / R4 / R5).

If the physical QAM ("...") button on your Deck is broken, this lets you open
the Quick Access Menu with any back button instead — including while a game is
running.

## How it works

* A small Python backend reads the built-in controller's **raw HID reports**,
  so it can see the back buttons even during gameplay (Steam normally hides
  them from regular input devices).
* When your bound button is pressed, the backend signals the frontend, which
  calls `Navigation.OpenQuickAccessMenu()`. The frontend lives in Steam's
  always-loaded UI context, so this works in games too.
* Instead of hard-coding which bit in the HID report belongs to each back
  button (it varies between Deck revisions), the plugin **learns** the button:
  you press it once and it records the signature.

## Usage

1. Install the plugin (see below) and open it from the Quick Access Menu's
   Decky tab.
2. Tap **Set trigger button**.
3. Hold the controller still for a second, then **press and hold** the back
   button you want to use, and release.
4. That's it — pressing that button now opens the QAM. Use **Test: open QAM
   now** to confirm the open works, and the **Enabled** toggle to turn the
   remap on/off.

## Installation

This plugin is distributed as a zip you install through Decky's developer
**Install from URL** feature:

1. In Decky settings, enable **Developer mode**.
2. Go to the **Developer** tab → **Install Plugin from URL**.
3. Paste the URL of the latest release zip:
   `https://github.com/nphil/decky-qam/releases/latest/download/decky-QAM.zip`

## Building from source

```bash
pnpm install
pnpm run build      # outputs dist/index.js
```

The CI workflow in `.github/workflows/build.yml` builds and packages the
installable `decky-QAM.zip` automatically when a `v*` tag is pushed.

## Notes & limitations

* Requires a genuine Steam Deck (Valve VID `28DE`). On other handhelds the
  controller won't be detected.
* Opening the QAM uses an undocumented Steam frontend call; if a future
  SteamOS update changes it, the **Test: open QAM now** button is the quickest
  way to check.
* The backend needs raw-HID access, which is why the plugin requests root
  (`_root` flag in `plugin.json`).

## License

BSD-3-Clause. See [LICENSE](./LICENSE).
