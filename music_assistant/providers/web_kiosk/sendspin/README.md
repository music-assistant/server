# Sendspin browser assets

This directory holds the entry-point source, the build script and the locked npm
dependencies for the bundle committed in `../static/web/sendspin-js/`. It lives
inside the provider so the upstream sync ships the source together with the
generated assets, and the bundle can be rebuilt from the provider tree alone.

Node 22 or newer is required only for development. Music Assistant serves the
committed assets and does not install npm dependencies at runtime.

From this directory:

```bash
npm ci
npm run check   # every committed asset matches the locked build
npm run build   # rebuild after changing index.js or the dependencies
```

To update dependencies, edit the exact versions in `package.json`, regenerate
`package-lock.json` with npm, rebuild and review the generated diff. The Noble
overrides keep the crypto versions unchanged from the previous vendored client.

The classic bundle exposes only `SendspinPlayer` and `loadSendspinClientIdentity`
through `WebKioskSendspin`. It targets Chromium 67, Safari 15 and Firefox 68;
BigInt, Web Audio and crypto support are still runtime requirements. A small
`globalThis` fallback supports older engines without weakening crypto.

The player/builder scripts are separately checked against ES2015 syntax, so
HTML5 does not require the SDK. esbuild bundles their SDK and crypto
dependencies without property mangling.
The entry URL stays `/web/sendspin-js/index.js`; browser storage, player identity
and the existing Sendspin connection behavior remain the same.

Software Opus decoding remains unsupported. The two optional opus-encdec imports
are replaced by modules that reject with an explicit error if reached. Opus uses
WebCodecs; browsers without WebCodecs are still offered FLAC/PCM by the kiosk.
No CDN, unresolved package imports or Node builtins are needed by the browser.

`BUILD.json` records the bundled package versions, esbuild version and JS SHA256,
and names this directory's `index.js` (`source`) and `build.mjs` (`build`), so the
generated bundle can be traced back to the inputs that produced it.
Full upstream licenses and package attribution are embedded in the JS header.
No separate license files are shipped. Generated assets are tracked
so provider releases and the standard upstream sync need no build-system changes
in Music Assistant.
